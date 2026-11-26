/**
 * Real-engine smoke test.
 *
 * Boots the Host plugin against the engine the other subagent ships
 * (`output/dsh-subtitle-studio/engine/subtitle-run`), then renders the real
 * `client.js` panel headlessly and checks the shipped engine's doctor / plan
 * payloads actually reach the UI. The mock test (`tmp/e2e.mjs`,
 * `tmp/ui-smoke.mjs`) covers the plugin contract; this one covers the
 * integration the contract cannot prove.
 *
 * Usage: node tmp/real-smoke.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
// Ships inside <package>/test/: the parent is the package. The fallback keeps
// the script usable from its historical home under the project's evidence tree.
const OWN = path.resolve(HERE, '..')
const PACKAGE = fs.existsSync(path.join(OWN, 'index.js'))
  ? OWN
  : path.join(path.dirname(HERE), 'output', 'dsh-subtitle-studio')
const ENGINE = path.join(PACKAGE, 'engine', 'subtitle-run')

if (fs.existsSync(ENGINE) === false) {
  console.log('SKIP: engine/subtitle-run does not exist yet')
  process.exit(0)
}

const SANDBOX = path.join(HERE, 'sandbox-real')
fs.rmSync(SANDBOX, { recursive: true, force: true })
const HOME = path.join(SANDBOX, 'home')
const DATA = path.join(SANDBOX, 'data')
const SRC = path.join(SANDBOX, 'src')
const OUT = path.join(SANDBOX, 'out')
for (const dir of [HOME, DATA, SRC, OUT]) fs.mkdirSync(dir, { recursive: true })
fs.writeFileSync(path.join(SRC, 'a.mp4'), 'not a real video')
fs.writeFileSync(path.join(SRC, 'b.mkv'), 'not a real video')

// `schedule.mode: 'dsh'` is deliberate: the host must hand the engine a
// normalized copy, because the engine's schema only knows launchd / none.
fs.writeFileSync(
  path.join(DATA, 'config.json'),
  JSON.stringify(
    {
      version: 1,
      engine: { kind: 'ollama', model: 'qwen2.5:14b', temperature: 0.2 },
      source: { kind: 'local', path: SRC, include: ['*.mp4', '*.mkv'], recursive: false },
      output: { kind: 'local', path: OUT, nameTemplate: '{stem}_zh.mp4', overwrite: false },
      selection: { maxPerRun: 2, minSizeGB: 0.5 },
      prompts: { default: { system: '默认系统提示', context: '' }, profiles: [] },
      burn: { enabled: false, crf: 18 },
      schedule: { enabled: true, mode: 'dsh', time: '02:10', label: 'com.subtitle-studio.run' },
    },
    null,
    2,
  ),
)
fs.chmodSync(path.join(DATA, 'config.json'), 0o600)

process.env.HOME = HOME
process.env.SUBTITLE_STUDIO_DATA_DIR = DATA
process.env.SUBTITLE_STUDIO_ENGINE = ENGINE

const routes = new Map()
const hostCtx = {
  effect(fn) {
    fn()
  },
  get() {
    return undefined
  },
  webServer: {
    register(entry) {
      routes.set(entry.path, entry)
      return () => routes.delete(entry.path)
    },
  },
}
await import(pathToFileURL(path.join(PACKAGE, 'index.js')).href).then((module) => module.apply(hostCtx))

const server = http.createServer((request, response) => {
  const pathname = new URL(request.url ?? '/', 'http://127.0.0.1').pathname
  const entry = routes.get(pathname)
  if (entry === undefined) {
    response.writeHead(404).end('not found')
    return
  }
  entry.handler(request, response)
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port
const api = (name, body) =>
  fetch(`http://127.0.0.1:${port}/api/dsh-subtitle-studio/${name}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'content-type': 'application/json', 'sec-fetch-site': 'same-origin' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(async (response) => ({ status: response.status, body: await response.json() }))

const results = []
let failures = 0
function check(name, ok, detail = '') {
  if (ok !== true) failures += 1
  results.push(ok === true)
  console.log(`${ok === true ? 'PASS' : 'FAIL'}  ${name}${detail === '' ? '' : `  ${detail}`}`)
}

/* --------------------------------- host level ------------------------------ */

const describe = await api('describe')
check('describe with the real engine', describe.body.ok === true && describe.body.data.engineExists === true)

const doctor = await api('doctor', {})
const checks = doctor.body?.result?.data?.checks
check('doctor returns checks from the real engine', Array.isArray(checks) && checks.length > 10, `${checks ? checks.length : 0} checks`)
check('doctor checks use name/level/message', Array.isArray(checks) && checks[0]?.name !== undefined && checks[0]?.level !== undefined)
check('doctor works with schedule.mode=dsh (adapter)', Array.isArray(checks), JSON.stringify(doctor.body?.result?.error ?? '').slice(0, 160))
const engineConfig = path.join(DATA, 'state', 'engine-config.json')
check(
  'engine view of config normalizes mode to none',
  fs.existsSync(engineConfig) === true && JSON.parse(fs.readFileSync(engineConfig, 'utf8')).schedule.mode === 'none',
)
check(
  'engine view keeps a 0600 mode',
  fs.existsSync(engineConfig) === true && (fs.statSync(engineConfig).mode & 0o777) === 0o600,
)

const plan = await api('plan', {})
const candidates = plan.body?.result?.data?.candidates
check('plan works with the real engine', plan.body?.result?.ok === true, plan.body?.result?.error?.message ?? '')
check('plan returns candidates with selection flags', Array.isArray(candidates) && candidates.every((item) => item.selected !== undefined))

const status = await api('engine-status')
check('status works with the real engine', status.body?.result?.ok === true)
check('status reports the lock holder field', status.body?.result?.data?.lock !== undefined)

const dryRun = await api('run', { dryRun: true, max: 1 })
check('run --dry-run is accepted', dryRun.body?.ok === true, JSON.stringify(dryRun.body?.data ?? dryRun.body).slice(0, 160))

const refused = await api('run', { dryRun: true })
check('second run is refused while one is active or accepted as a new job', refused.status === 200 || refused.status === 409)

/* -------------------------------- client level ----------------------------- */

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const FRAGMENT = Symbol.for('fragment')
const fibers = new Map()
let current = null
let dirty = false
let pending = []

function slot(init) {
  const { fiber, index } = current
  if (fiber.hooks[index] === undefined) fiber.hooks[index] = init()
  const value = fiber.hooks[index]
  current.index += 1
  return value
}

const React = {
  Fragment: FRAGMENT,
  createElement(type, props, ...children) {
    return { __el: true, type, props: { ...(props || {}), children: children.flat(Infinity) } }
  },
  useState(initial) {
    const box = slot(() => ({ value: typeof initial === 'function' ? initial() : initial }))
    const set = (next) => {
      const value = typeof next === 'function' ? next(box.value) : next
      if (value !== box.value) {
        box.value = value
        dirty = true
      }
    }
    return [box.value, set]
  },
  useRef(initial) {
    return slot(() => ({ current: initial }))
  },
  useMemo(factory, deps) {
    const box = slot(() => ({ deps: undefined, value: undefined }))
    const next = deps === undefined ? undefined : JSON.stringify(deps)
    if (box.deps !== next) {
      box.value = factory()
      box.deps = next
    }
    return box.value
  },
  useCallback(fn, deps) {
    return React.useMemo(() => fn, deps)
  },
  useEffect(fn, deps) {
    const index = current.index
    const box = slot(() => ({ deps: undefined, ran: false }))
    pending.push({ fiber: current.fiber, index, box, fn, key: deps === undefined ? undefined : JSON.stringify(deps) })
  },
}

function renderElement(element, pathKey) {
  if (element === null || element === undefined || element === false || element === true) return { text: '' }
  if (typeof element === 'string' || typeof element === 'number') return { text: String(element) }
  if (Array.isArray(element)) {
    const parts = element.map((child) => renderElement(child, pathKey))
    return { text: parts.map((part) => part.text).join(''), children: parts.flatMap((part) => (part.tag ? [part] : part.children || [])) }
  }
  if (element.__el !== true) return { text: '' }
  const { type, props } = element
  if (type === FRAGMENT) {
    const parts = (props.children || []).map((child) => renderElement(child, pathKey))
    return { text: parts.map((part) => part.text).join(''), children: parts.flatMap((part) => (part.tag ? [part] : part.children || [])) }
  }
  if (typeof type === 'function') {
    const key = `${pathKey}>${type.name || 'Anon'}`
    const previous = current
    const fiber = fibers.get(key) || { hooks: [], key }
    fibers.set(key, fiber)
    current = { fiber, index: 0 }
    let output
    try {
      output = type(props)
    } finally {
      current = previous
    }
    return renderElement(output, key)
  }
  const tag = String(type)
  const children = (props.children || []).map((child, index) => renderElement(child, `${pathKey}>${tag}${index}`))
  return { tag, props, text: children.map((child) => child.text).join(''), children: children.flatMap((child) => (child.tag ? [child] : child.children || [])) }
}

function runEffects() {
  const queued = pending
  pending = []
  for (const entry of queued) {
    if (entry.box.ran === true && entry.box.deps === entry.key) continue
    entry.box.ran = true
    entry.box.deps = entry.key
    entry.fn()
  }
}

async function render(root) {
  let tree = null
  let clean = 0
  const started = Date.now()
  for (let pass = 0; pass < 80; pass += 1) {
    dirty = false
    tree = renderElement(root(), 'root')
    runEffects()
    await sleep(40)
    clean = dirty === false ? clean + 1 : 0
    if (clean >= 5 && Date.now() - started > 300) return tree
  }
  return tree
}

function buttons(tree) {
  const all = []
  const walk = (node) => {
    if (node === null || node === undefined) return
    if (node.tag === 'button') all.push(node)
    for (const child of node.children || []) walk(child)
  }
  walk(tree)
  return all
}

/**
 * Tab labels double as content-button labels (「诊断」 is both a tab and the
 * run-doctor button), and an action that finishes later can yank the tab with
 * it — so tab clicks must never resolve to a content button. The tab bar
 * renders before any card, so the *first* exact match is always the tab.
 */
function clickTab(tree, label) {
  const target = buttons(tree).find((node) => String(node.text).trim() === label)
  if (target === undefined) throw new Error(`tab not found: ${label}`)
  target.props.onClick()
}

function click(tree, label) {
  const all = buttons(tree)
  const exact = all.filter((node) => String(node.text).trim() === label)
  const target = exact.length > 0 ? exact[exact.length - 1] : all.find((node) => String(node.text).includes(label))
  if (target === undefined) {
    const have = all.map((node) => String(node.text).trim()).filter(Boolean).slice(0, 40).join(' | ')
    throw new Error(`button not found: ${label}  [have: ${have}]`)
  }
  target.props.onClick()
}

const dict = {}
globalThis.window = globalThis
globalThis.document = { baseURI: `http://127.0.0.1:${port}/` }
globalThis.setInterval = () => 0
globalThis.clearInterval = () => {}
globalThis.window.confirm = () => true
let moduleExports = null
globalThis.window.__ModuleLoader__ = {
  load(definition) {
    moduleExports = definition.factory((specifier) => {
      if (specifier === 'react') return { default: React, ...React }
      throw new Error(`unexpected require(${specifier})`)
    })
  },
}
await import(pathToFileURL(path.join(PACKAGE, 'client.js')).href)
moduleExports.apply({
  effect(fn) {
    fn()
  },
  locale: {
    register(namespace, strings) {
      dict.zh = strings.zh
      dict.en = strings.en
    },
    bind() {
      return (key) => dict.zh[key] ?? key
    },
  },
  slots: {
    inject(name, callback) {
      callback()
    },
    register(definition, component) {
      moduleExports.Panel = component
      return () => {}
    },
  },
})

const panel = () => React.createElement(moduleExports.Panel)

/**
 * The panel loads asynchronously and several actions jump to another tab when
 * they finish, so "click once, render once" races the tab state. `settle`
 * re-renders until the condition holds; the click itself is issued once, by
 * the caller, before waiting. A timeout comes back as a failed check with the
 * label it was waiting for, not as an exception that hides every later check.
 */
async function settle(root, condition, label) {
  let tree = null
  const deadline = Date.now() + 20000
  while (Date.now() < deadline) {
    tree = await render(root)
    if (condition(tree)) return { tree, ok: true, label }
    await sleep(120)
  }
  return { tree, ok: false, label }
}

const has = (needle) => (tree) => tree.text.includes(needle)
const tabButton = (label) => (tree) => buttons(tree).some((node) => String(node.text).trim() === label)

const opened = await settle(panel, has('字幕工坊'), 'panel renders')
check('panel renders against the real engine', opened.ok, opened.label)
const overviewReady = await settle(panel, tabButton('只选片'), 'overview run actions')
check('the overview offers the run actions', overviewReady.ok, overviewReady.label)

clickTab(overviewReady.tree, '诊断')
const onDiagnose = await settle(panel, tabButton('刷新'), 'the diagnose tab')
check('the diagnose tab is reachable by its own tab button', onDiagnose.ok, onDiagnose.label)

click(onDiagnose.tree, '诊断')
const diagnosed = await settle(panel, (tree) => has('ffmpeg')(tree) && has('python')(tree), 'doctor table with real check names')
check('doctor table renders the real check names', diagnosed.ok, diagnosed.label)
check('doctor shows a real verdict', /通过|警告|失败|error|warn/.test(diagnosed.tree.text))
check('doctor did not fall back to a failure banner', diagnosed.tree.text.includes('doctor failed') === false)
check('doctor renders the engine message text',
      diagnosed.tree.text.includes('配置已加载') || diagnosed.tree.text.includes('ffmpeg'))

clickTab(diagnosed.tree, '概览')
const backOnOverview = await settle(panel, tabButton('只选片'), 'back on the overview')
check('the overview is reachable again after the doctor finished', backOnOverview.ok, backOnOverview.label)

click(backOnOverview.tree, '只选片')
const planned = await settle(panel, (tree) => has('a.mp4')(tree) || has('b.mkv')(tree), 'plan table with real candidates')
check('plan table renders the real candidates', planned.ok, planned.label)
check('plan shows skip reasons for undersized files',
      planned.tree.text.includes('体积小于下限') || planned.tree.text.includes('too-small'))

server.close()
const passed = results.filter(Boolean).length
console.log(`\n${passed}/${results.length} checks passed, ${failures} failed`)
process.exit(failures === 0 ? 0 : 1)

/**
 * Headless smoke test for client.js.
 *
 * Loads the real client bundle through a fake `window.__ModuleLoader__`, renders
 * the settings panel with a tiny React-compatible hook runtime (no DOM, no
 * browser), points `document.baseURI` at the real host bridge running in this
 * process, then clicks through all eight tabs and the main actions. It proves
 * the panel wires up, renders, and talks to the bridge — it does NOT prove the
 * host's React renders identically, nor that the styling looks right.
 *
 * Usage: node output/evidence/selftests/ui-smoke.mjs
 * (the mock engine it spawns lives next to this file in ./mock/subtitle-run)
 */
import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/**
 * Locate the package root. This script ships inside `<package>/test/`, so the
 * parent directory is normally the answer; the upward search is only a
 * fallback for the historical location under the project's `evidence/` tree.
 */
function findPackage(from) {
  const own = path.resolve(from, '..')
  if (fs.existsSync(path.join(own, 'index.js'))) return own
  let dir = from
  for (let hop = 0; hop < 6; hop += 1) {
    if (fs.existsSync(path.join(dir, 'output', 'dsh-subtitle-studio', 'index.js'))) {
      return path.join(dir, 'output', 'dsh-subtitle-studio')
    }
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  throw new Error(`cannot locate the package root from ${from}`)
}
const PACKAGE = findPackage(HERE)

/* ------------------------------- sandbox + host ---------------------------- */

const SANDBOX = path.join(HERE, 'sandbox-ui')
fs.rmSync(SANDBOX, { recursive: true, force: true })
const HOME = path.join(SANDBOX, 'home')
const DATA = path.join(SANDBOX, 'data')
fs.mkdirSync(HOME, { recursive: true })
fs.mkdirSync(DATA, { recursive: true })
fs.mkdirSync(path.join(HOME, 'Movies', 'subtitled'), { recursive: true })
fs.mkdirSync(path.join(SANDBOX, 'launchd-state'), { recursive: true })

/* --------------------------- fake local ollama ----------------------------- */
/* 固定住 Ollama 探测：不依赖测试机上有没有真的装 ollama。 */
const ollamaHits = []
const fakeOllama = http.createServer((request, response) => {
  ollamaHits.push(new URL(request.url ?? '/', 'http://127.0.0.1').pathname)
  if (request.url === '/api/tags') {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ models: [{ name: 'qwen2.5:14b' }, { name: 'llama3.2:3b' }] }))
    return
  }
  if (request.url === '/api/generate') {
    let raw = ''
    request.on('data', (chunk) => {
      raw += chunk
    })
    request.on('end', () => {
      let asked = null
      try {
        asked = JSON.parse(raw)
      } catch {
        asked = null
      }
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ response: `pong 本地 ${asked?.model ?? '?'}` }))
    })
    return
  }
  response.writeHead(404).end('{}')
})
await new Promise((resolve) => fakeOllama.listen(0, '127.0.0.1', resolve))
const OLLAMA_BASE = `http://127.0.0.1:${fakeOllama.address().port}`

// A config that exercises every editor branch (profiles, ssh, burn disabled).
fs.writeFileSync(
  path.join(DATA, 'config.json'),
  JSON.stringify(
    {
      version: 1,
      engine: { kind: 'ollama', model: 'qwen2.5:14b', ollamaHost: OLLAMA_BASE, temperature: 0.2, ollamaModel: 'legacy-model:stale' },
      source: { kind: 'ssh', path: '/mnt/media', ssh: { host: 'media.example', port: 22, user: 'media', keyPath: '~/.ssh/id_ed25519' } },
      output: { kind: 'local', path: '~/Movies/subtitled', nameTemplate: '{stem}_zh.mp4' },
      selection: { maxPerRun: 2, minSizeGB: 0.5 },
      prompts: {
        default: { system: '默认系统提示', context: '' },
        profiles: [{ name: '第01话 专用', match: ['第01话*'], system: '系统 A', context: '上下文 A', series: 'example' }],
      },
      burn: { enabled: false, crf: 18 },
      schedule: { enabled: true, mode: 'dsh', time: '02:10', guardTimes: ['09:05'], label: 'com.subtitle-studio.run' },
      legacyBookmark: { keep: true },
    },
    null,
    2,
  ),
)

process.env.HOME = HOME
process.env.SUBTITLE_STUDIO_DATA_DIR = DATA
const LOCAL_MOCK = path.join(HERE, 'mock', 'subtitle-run')
if (fs.existsSync(LOCAL_MOCK) === false) {
  console.error(`missing mock engine: ${LOCAL_MOCK}`)
  process.exit(1)
}
process.env.SUBTITLE_STUDIO_ENGINE = LOCAL_MOCK
process.env.FAKE_LAUNCHD_STATE = path.join(SANDBOX, 'launchd-state')
process.env.FAKE_LAUNCHCTL_LOG = path.join(SANDBOX, 'launchctl.log')
process.env.PATH = `${path.join(HERE, 'bin')}:${process.env.PATH ?? ''}`

const llmCalls = []
const fakeLlm = {
  listProviders: () => [{ id: 'provider-a', name: 'Provider A' }, { id: 'provider-b', name: 'Provider B' }],
  async listModels(provider) {
    const table = {
      'provider-a': [{ provider: 'provider-a', id: 'model-alpha', name: 'Model Alpha' }],
      'provider-b': [{ provider: 'provider-b', id: 'model-beta', name: 'Model Beta' }],
    }
    return table[provider] ?? []
  },
  async *stream(options) {
    llmCalls.push(options)
    yield { type: 'text-delta', index: 0, text: 'pong' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  },
}

const routes = new Map()
const ctx = {
  effect(fn) {
    const disposer = fn()
    return () => {
      if (typeof disposer === 'function') disposer()
    }
  },
  get(name) {
    return name === 'llm' ? fakeLlm : undefined
  },
  webServer: {
    register(entry) {
      routes.set(entry.path, entry)
      return () => routes.delete(entry.path)
    },
  },
}
await import(pathToFileURL(path.join(PACKAGE, 'index.js')).href).then((module) => module.apply(ctx))

/* ------------------- QQ Bot notify bridge (mocked, no keychain) ------------------- */

// 真实路由会读 macOS 钥匙串、并让引擎真发 QQ 消息；这里全部拦掉，只验证 UI 契约。
const notifyMock = {
  secretConfigured: false,
  secretCalls: [],
  testCalls: 0,
  sent: true,
  reason: 'mock-send-ok',
}

function sendJson(response, status, value) {
  const body = JSON.stringify(value)
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  response.end(body)
}

const readRequestBody = (request) =>
  new Promise((resolve) => {
    let raw = ''
    request.on('data', (chunk) => {
      raw += chunk
    })
    request.on('end', () => {
      try {
        resolve(raw === '' ? {} : JSON.parse(raw))
      } catch {
        resolve({})
      }
    })
  })

/** Re-run a real handler into a shadow response so its JSON can be rewritten. */
function interceptRoute(pathname, transform) {
  const entry = routes.get(pathname)
  if (entry === undefined) throw new Error(`route not registered: ${pathname}`)
  routes.set(pathname, {
    ...entry,
    handler(request, response) {
      let status = 200
      let raw = ''
      const shadow = {
        writableEnded: false,
        setHeader() {},
        writeHead(nextStatus) {
          status = typeof nextStatus === 'number' ? nextStatus : 200
          shadow.writableEnded = true
          return shadow
        },
        end(chunk) {
          if (chunk !== undefined) raw += String(chunk)
          shadow.writableEnded = true
        },
      }
      Promise.resolve(entry.handler(request, shadow)).then((handled) => {
        let parsed = raw === '' ? null : JSON.parse(raw)
        if (handled === false && parsed === null) {
          status = 500
          parsed = { ok: false, error: 'unhandled request' }
        }
        const transformed = transform(parsed, status)
        sendJson(response, status, transformed === undefined ? parsed : transformed)
      })
    },
  })
}

interceptRoute('/api/dsh-subtitle-studio/describe', (payload) => {
  const data = payload.data
  data.notifyChannels = ['qqbot', 'command', 'webhook', 'none']
  data.notifyQqSendAvailable = true
  data.notifySecret = {
    configured: notifyMock.secretConfigured,
    service: 'subtitle-studio-qqbot',
    source: notifyMock.secretConfigured === true ? 'keychain' : '',
  }
  data.config.notify = {
    ...data.config.notify,
    qqAppId: '',
    qqOpenId: '',
    qqSecretService: 'subtitle-studio-qqbot',
    qqSendPath: '',
  }
  return payload
})

routes.set('/api/dsh-subtitle-studio/notify-secret', {
  path: '/api/dsh-subtitle-studio/notify-secret',
  async handler(request, response) {
    const body = await readRequestBody(request)
    notifyMock.secretCalls.push(String(body.secret ?? ''))
    notifyMock.secretConfigured = true
    sendJson(response, 200, { ok: true, data: { configured: true, service: 'subtitle-studio-qqbot' } })
  },
})

routes.set('/api/dsh-subtitle-studio/notify-test', {
  path: '/api/dsh-subtitle-studio/notify-test',
  async handler(request, response) {
    await readRequestBody(request)
    notifyMock.testCalls += 1
    if (notifyMock.sent === true) {
      sendJson(response, 200, { ok: true, data: { sent: true, reason: notifyMock.reason, channel: 'qqbot', target: 'mock-openid' } })
      return
    }
    sendJson(response, 500, {
      ok: false,
      code: 'notify-failed',
      error: notifyMock.reason,
      data: { sent: false, reason: notifyMock.reason },
    })
  },
})

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

/* ------------------------------- mini React -------------------------------- */

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
    pending.push({
      fiber: current.fiber,
      index,
      box,
      fn,
      key: deps === undefined ? undefined : JSON.stringify(deps),
    })
  },
}

const reactModule = { default: React, ...React }

/** Render one element tree, executing function components and collecting text. */
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
    const name = type.name || 'Anon'
    const key = `${pathKey}>${name}`
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
  return {
    tag,
    props,
    path: pathKey,
    text: children.map((child) => child.text).join(''),
    children: children.flatMap((child) => (child.tag ? [child] : child.children || [])),
  }
}

/** Run the queued effects whose dependencies changed. */
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

/**
 * Render until the tree settles. Handlers are async (they await the bridge), so
 * "settled" means several consecutive passes without a state change AND at
 * least 250 ms elapsed — otherwise a render would return mid-flight.
 */
async function render(root) {
  let tree = null
  let clean = 0
  const started = Date.now()
  for (let pass = 0; pass < 60; pass += 1) {
    dirty = false
    tree = renderElement(root(), 'root')
    runEffects()
    await sleep(40)
    clean = dirty === false ? clean + 1 : 0
    if (clean >= 4 && Date.now() - started > 250) return tree
  }
  return tree
}

/** Depth-first walk of a rendered tree. */
function walk(node, visit) {
  if (node === null || node === undefined) return
  visit(node)
  for (const child of node.children || []) walk(child, visit)
}

/** All text in a subtree, plus every button found. */
function inspect(tree) {
  const texts = []
  const buttons = []
  walk(tree, (node) => {
    if (node.tag === 'button') buttons.push(node)
    if (typeof node.text === 'string' && node.text !== '') texts.push(node.text)
  })
  return { text: tree.text, buttons }
}

/** Find a button whose label matches. */
function button(tree, label) {
  const { buttons } = inspect(tree)
  // Tab buttons come before the panel's own actions in DOM order, so an exact
  // duplicate like the 诊断 tab and the 诊断 action must resolve to the latter.
  const exact = buttons.filter((node) => String(node.text).trim() === label)
  const found = exact.length > 0 ? exact[exact.length - 1] : buttons.find((node) => String(node.text).includes(label))
  if (found === undefined) throw new Error(`button not found: ${label} in [${buttons.map((node) => String(node.text).trim()).join(' | ')}]`)
  return found
}

/* -------------------------------- load module ------------------------------ */

const registered = []
const dict = { zh: null, en: null }
let activeLang = 'zh'

globalThis.window = globalThis
globalThis.document = { baseURI: `http://127.0.0.1:${port}/` }
globalThis.setInterval = () => 0
globalThis.clearInterval = () => {}
globalThis.window.confirm = () => true

let moduleExports = null
globalThis.window.__ModuleLoader__ = {
  load(definition) {
    if (definition.id !== 'dsh-subtitle-studio') throw new Error(`unexpected module id ${definition.id}`)
    moduleExports = definition.factory((specifier) => {
      if (specifier === 'react') return reactModule
      throw new Error(`unexpected require(${specifier})`)
    })
  },
}

await import(pathToFileURL(path.join(PACKAGE, 'client.js')).href)

const localeSlots = new Map()
const localeDisposers = []

/** Mirrors LocaleRuntime.register: a namespace may hold each locale only once. */
function fakeRegister(namespace, strings) {
  let locales = localeSlots.get(namespace)
  if (locales === undefined) {
    locales = new Map()
    localeSlots.set(namespace, locales)
  }
  for (const locale of Object.keys(strings)) {
    if (locales.has(locale)) {
      throw new Error(`locale namespace "${namespace}" already has locale "${locale}"`)
    }
  }
  for (const [locale, entries] of Object.entries(strings)) locales.set(locale, entries)
  dict.zh = strings.zh
  dict.en = strings.en
  registered.push(namespace)
  return () => {
    for (const [locale, entries] of Object.entries(strings)) {
      if (locales.get(locale) === entries) locales.delete(locale)
    }
  }
}

const clientCtx = {
  effect(fn) {
    const release = fn()
    if (typeof release === 'function') localeDisposers.push(release)
    return release
  },
  locale: {
    register: fakeRegister,
    bind(namespace) {
      return (key) => dict[activeLang][key] ?? key
    },
  },
  slots: {
    inject(name, callback) {
      return callback()
    },
    register(definition, component) {
      moduleExports.Panel = component
      moduleExports.section = definition
      return () => {}
    },
  },
}
moduleExports.apply(clientCtx)

const results = []
let failures = 0
function check(name, condition, detail = '') {
  const ok = condition === true
  if (!ok) failures += 1
  results.push({ ok, name })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail === '' ? '' : `  ${detail}`}`)
}

check('module registers two locales', registered.includes('dsh-subtitle-studio') === true && dict.zh !== null && dict.en !== null)

/* Re-activation used to drop the whole plugin: the client registered its
 * namespace without handing the cleanup back, so a second apply inside the
 * same page met a namespace that still held "zh". */
function tryApply() {
  try {
    moduleExports.apply(clientCtx)
    return null
  } catch (error) {
    return String(error?.message ?? error)
  }
}

check(
  'locale registration hands its cleanup back to the host',
  localeDisposers.length === 1 && typeof localeDisposers[0] === 'function',
  `${localeDisposers.length} disposers`,
)
for (const release of localeDisposers.splice(0)) release()
const afterDispose = tryApply()
check('re-activating after the host disposed the first one is clean', afterDispose === null, String(afterDispose))
check('the second activation re-registers both locales', registered.filter((id) => id === 'dsh-subtitle-studio').length === 2, `${registered.length} registrations`)
const stale = tryApply()
check('a namespace left behind by a undisposed generation degrades instead of throwing', stale === null, String(stale))
check('a degraded re-registration still resolves panel text', (dict[activeLang]['nav'] ?? '') === '字幕工坊', String(dict[activeLang]?.nav))
check('locale keys are in parity', Object.keys(dict.zh).length === Object.keys(dict.en).length, `${Object.keys(dict.zh).length}/${Object.keys(dict.en).length}`)
check('section registered at settings.section', moduleExports.section?.name === 'settings.section' && moduleExports.section?.order >= 40)
check('section label is localized', typeof moduleExports.section?.label === 'function' && moduleExports.section.label() === '字幕工坊')

let tree = await render(() => React.createElement(moduleExports.Panel))
check('panel renders without throwing', tree !== null && typeof tree.text === 'string')
check('panel shows the title', tree.text.includes('字幕工坊'))
check('panel shows the description', tree.text.includes('config.json'))
check('overview lists the config path', tree.text.includes(path.join(DATA, 'config.json')))
check('overview shows the engine path', tree.text.includes(process.env.SUBTITLE_STUDIO_ENGINE))
check('overview shows schedule state', tree.text.includes('com.subtitle-studio.run'))

/* ---------------------------------- tabs ---------------------------------- */

const expectTab = async (label, markers) => {
  const next = await render(() => React.createElement(moduleExports.Panel))
  button(next, label).props.onClick()
  const after = await render(() => React.createElement(moduleExports.Panel))
  for (const marker of markers) {
    check(`tab ${label}: shows ${marker}`, after.text.includes(marker))
  }
  return after
}

await expectTab('引擎', ['模型', 'Ollama', '温度'])


await expectTab('来源与输出', ['来源目录', '输出目录', 'SSH', '文件名模板'])
const selectionTree = await expectTab('选片', ['每次最多处理', '排序', '跳过已翻译'])
await expectTab('提示词', ['新增提示词配置', '默认提示词', '系统提示词', '匹配模式'])
const qualityTree = await expectTab('画质音频', ['双语字幕', '通知渠道'])
/* A control the engine ignores is worse than no control at all: the panel has
   to hide it, and reveal it again the moment the setting that enables it flips. */
const switchByLabel = (tree, label) => {
  let found = null
  walk(tree, (node) => {
    if (found === null && node.props && node.props.role === 'switch' && node.props['aria-label'] === label) found = node
  })
  if (found === null) throw new Error(`switch not found: ${label}`)
  return found
}
check('quality tab hides the encoder knobs while burning is off', !qualityTree.text.includes('CRF 画质'))
check('quality tab hides the notify toggles when the channel is none', !qualityTree.text.includes('开始时通知'))
/* 输出方式（压制进视频 / 只生成字幕文件）是个下拉，不再是开关；
   压制相关的旋钮要跟着它显隐。 */
const modeSelect = (tree) => {
  let found = null
  walk(tree, (node) => {
    if (found !== null || node.tag !== 'select') return
    const values = []
    walk(node, (child) => {
      if (child.tag === 'option' && child.props && typeof child.props.value === 'string') values.push(child.props.value)
    })
    if (values.includes('burn') && values.includes('srt')) found = node
  })
  if (found === null) throw new Error('output mode select not found')
  return found
}
const pathsTree = await expectTab('来源与输出', ['输出方式', '文件名模板'])
check('the output mode lists both ways to deliver the subtitles',
  pathsTree.text.includes('压制进视频') && pathsTree.text.includes('只生成字幕文件'))
/* 老配置只有 burn.enabled，没有 output.mode —— 面板必须按它推导，
   不能因为默认模板里带了 mode 就把老用户的选择翻过来。 */
check('an old config without output.mode follows burn.enabled',
  modeSelect(pathsTree).props.value === 'srt')
modeSelect(pathsTree).props.onChange({ target: { value: 'burn' } })
const burningTree = await expectTab('画质音频', ['双语字幕'])
check('burning on reveals CRF, preset and audio codec', burningTree.text.includes('CRF 画质') && burningTree.text.includes('编码预设') && burningTree.text.includes('音频'))
check('burning on reveals the style presets', burningTree.text.includes('字幕样式'))
check('burning on reveals the ffmpeg path field', burningTree.text.includes('ffmpeg 路径'))
check('the quality tab carries the anti-hallucination card',
  burningTree.text.includes('转写质量与反幻觉') && burningTree.text.includes('温度回退上限'))
check('single-language burning shows its own font size hint', burningTree.text.includes('0 表示按视频高度自动'))
switchByLabel(burningTree, '双语字幕').props.onClick()
const bilingualTree = await render(() => React.createElement(moduleExports.Panel))
check('bilingual burning switches to the ASS font size', bilingualTree.text.includes('双语中文字号'))
check('bilingual burning hides the SRT font size it no longer reads', !bilingualTree.text.includes('0 表示按视频高度自动'))
const pathsTree2 = await expectTab('来源与输出', ['输出方式'])
modeSelect(pathsTree2).props.onChange({ target: { value: 'srt' } })
const subtitleOnlyTree = await expectTab('画质音频', ['双语字幕'])
check('switching back to subtitle-file mode hides the encoder knobs again',
  !subtitleOnlyTree.text.includes('CRF 画质') && !subtitleOnlyTree.text.includes('字幕样式'))
/* 但 ffmpeg 不能跟着藏：抽音频、分段、ffprobe 在两种模式下都要用，
   藏起来会让「PATH 里没有 ffmpeg」的报错指向一个面板上看不见的字段。 */
check('subtitle-file mode still shows the ffmpeg path field',
  subtitleOnlyTree.text.includes('ffmpeg 路径'))
const channelSelect = (tree) => {
  let found = null
  walk(tree, (node) => {
    if (found !== null || node.tag !== 'select') return
    const values = []
    walk(node, (child) => {
      if (child.tag === 'option' && child.props && typeof child.props.value === 'string') values.push(child.props.value)
    })
    if (values.includes('qqbot')) found = node
  })
  if (found === null) throw new Error('notify channel select not found')
  return found
}
channelSelect(bilingualTree).props.onChange({ target: { value: 'qqbot' } })
const notifiedTree = await render(() => React.createElement(moduleExports.Panel))
check('choosing a notify channel reveals the toggles', notifiedTree.text.includes('开始时通知'))
check('the notify branch follows the channel', notifiedTree.text.includes('AppID') || notifiedTree.text.includes('app id') || notifiedTree.text.includes('密钥'))
const scheduleTree = await expectTab('调度', ['应用调度', '卸载调度', '可粘贴的提示词'])
check('schedule tab shows the dsh guard prompt text', scheduleTree.text.includes('guard --config'))
check('schedule tab labels the primary trigger point', scheduleTree.text.includes('02:10 · 主触发'))
check('schedule tab lists the extra guard trigger point', scheduleTree.text.includes('09:05 · 兜底'))
const diagnoseBefore = await expectTab('诊断', ['环境自检', '日志'])

/* ------------------------- notify: QQ Bot credentials ------------------------- */

const notifySelectNode = (tree) => {
  let found = null
  walk(tree, (node) => {
    if (node.tag !== 'select') return
    const options = (node.children || []).map((child) => child.props && child.props.value)
    if (options.includes('qqbot') && options.includes('none')) found = node
  })
  return found
}
const notifyCardOf = (tree) => {
  let found = null
  walk(tree, (node) => {
    if (node.tag === 'section' && typeof node.text === 'string' && node.text.includes('通知渠道') && node.text.includes('开始时通知')) {
      found = node
    }
  })
  return found
}
const tagNodes = (node, tag) => {
  const list = []
  walk(node, (entry) => {
    if (entry.tag === tag) list.push(entry)
  })
  return list
}
const hasButton = (tree, label) => inspect(tree).buttons.some((node) => String(node.text).trim() === label)

button(await render(() => React.createElement(moduleExports.Panel)), '画质音频').props.onClick()
let notifyTree = await render(() => React.createElement(moduleExports.Panel))
notifySelectNode(notifyTree).props.onChange({ target: { value: 'qqbot' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))

let notifyCard = notifyCardOf(notifyTree)
const qqInputs = tagNodes(notifyCard, 'input')
check('notify qqbot: renders appId, openId and secret inputs', qqInputs.length === 3, `inputs=${qqInputs.length}`)
check('notify qqbot: renders save-secret and send-test buttons', hasButton(notifyTree, '保存密钥') && hasButton(notifyTree, '发送测试消息'))
check('notify qqbot: secret chip starts unconfigured', notifyTree.text.includes('未配置'))

// 空 secret 不应该打桥
button(notifyTree, '保存密钥').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check('notify qqbot: empty secret is refused', notifyTree.text.includes('先填入 AppSecret') && notifyMock.secretCalls.length === 0)

// 填 secret -> 保存 -> chip 翻转、输入框清空、明文不留在 DOM
tagNodes(notifyCardOf(notifyTree), 'input')
  .find((node) => node.props.type === 'password')
  .props.onChange({ target: { value: 'mock-secret-abc' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))
button(notifyTree, '保存密钥').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
const secretInput = tagNodes(notifyCardOf(notifyTree), 'input').find((node) => node.props.type === 'password')
check('notify qqbot: saving a secret posts it to the bridge', notifyMock.secretCalls.includes('mock-secret-abc'), JSON.stringify(notifyMock.secretCalls))
check('notify qqbot: saved chip flips to keychain', notifyTree.text.includes('已保存到钥匙串'))
check(
  'notify qqbot: secret is cleared and never echoed',
  secretInput !== undefined && secretInput.props.value === '' && notifyTree.text.includes('mock-secret-abc') === false,
)

// 测试消息：成功如实显示发动机给的 reason
button(notifyTree, '发送测试消息').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check('notify qqbot: test message reports the real reason', notifyTree.text.includes('测试消息已发送') && notifyTree.text.includes('mock-send-ok'))

// 失败必须显示失败，不许装成功
notifyMock.sent = false
notifyMock.reason = 'mock-test-failure'
button(notifyTree, '发送测试消息').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check(
  'notify qqbot: a failed test is reported as a failure',
  notifyTree.text.includes('测试消息发送失败') && notifyTree.text.includes('mock-test-failure') && notifyTree.text.includes('mock-send-ok') === false,
)
notifyMock.sent = true
notifyMock.reason = 'mock-send-ok'

// openid 辅助：粘贴事件 JSON -> 提取 -> 填进输入框
button(notifyTree, '粘贴消息取 openid').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
tagNodes(notifyCardOf(notifyTree), 'textarea')[0].props.onChange({ target: { value: '{"op":0,"user_openid":"a1b2c3d4e5f60718"}' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))
button(notifyTree, '提取 openid').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
const openIdInput = tagNodes(notifyCardOf(notifyTree), 'input').find((node) => String(node.props.placeholder).includes('openid'))
check(
  'notify qqbot: openid helper fills the field',
  openIdInput !== undefined && openIdInput.props.value === 'a1b2c3d4e5f60718' && notifyTree.text.includes('已填入 openid'),
)

// 提取不到要明确说没找到
tagNodes(notifyCardOf(notifyTree), 'textarea')[0].props.onChange({ target: { value: '{"msg":"nothing here"}' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))
button(notifyTree, '提取 openid').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check('notify qqbot: openid helper reports a miss', notifyTree.text.includes('没找到 openid'))

// command 分支保留原字段，不出现自定义脚本字段
notifySelectNode(notifyTree).props.onChange({ target: { value: 'command' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check('notify command: keeps the command field', notifyTree.text.includes('执行命令'))
check('notify command: drops the custom-script fallback field', notifyTree.text.includes('自定义脚本路径') === false)

// qqbot 的回退脚本只在 notifyQqSendAvailable 时、且折叠着的
notifySelectNode(notifyTree).props.onChange({ target: { value: 'qqbot' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check('notify qqbot: fallback script is collapsed by default', notifyTree.text.includes('自定义脚本路径') === false)
button(notifyTree, '高级：回退到自定义发送脚本').props.onClick()
notifyTree = await render(() => React.createElement(moduleExports.Panel))
check('notify qqbot: advanced section reveals the fallback script', notifyTree.text.includes('自定义脚本路径'))

// none 分支只剩说明文字
notifySelectNode(notifyTree).props.onChange({ target: { value: 'none' } })
notifyTree = await render(() => React.createElement(moduleExports.Panel))
notifyCard = notifyCardOf(notifyTree)
check(
  'notify none: hides every input',
  tagNodes(notifyCard, 'input').length === 0 && tagNodes(notifyCard, 'textarea').length === 0,
  `inputs=${tagNodes(notifyCard, 'input').length} textareas=${tagNodes(notifyCard, 'textarea').length}`,
)
check('notify none: explains nothing is sent', notifyTree.text.includes('不发送任何通知'))

/* -------------------------------- actions --------------------------------- */

// plan -> switches to the selection tab and renders candidates
const overview = await render(() => React.createElement(moduleExports.Panel))
button(overview, '概览').props.onClick()
const planTree = await render(() => React.createElement(moduleExports.Panel))
button(planTree, '只选片').props.onClick()
const afterPlan = await render(() => React.createElement(moduleExports.Panel))
check('plan renders candidates', afterPlan.text.includes('第01话 [1080p].mp4'), afterPlan.text.slice(0, 200))
check('plan renders skip reasons', afterPlan.text.includes('跳过'))

// doctor -> diagnostics table with the failing/warn rows
button(await render(() => React.createElement(moduleExports.Panel)), '概览').props.onClick()
const doctorTree = await render(() => React.createElement(moduleExports.Panel))
button(doctorTree, '诊断').props.onClick()
const afterDoctor = await render(() => React.createElement(moduleExports.Panel))
check('doctor renders the check table', afterDoctor.text.includes('ffmpeg') && afterDoctor.text.includes('磁盘空间'))
check('doctor renders a summary', afterDoctor.text.includes('通过'))

// save -> banner + reloaded truth
button(await render(() => React.createElement(moduleExports.Panel)), '概览').props.onClick()
const editTree = await render(() => React.createElement(moduleExports.Panel))
button(editTree, '保存').props.onClick()
const afterSave = await render(() => React.createElement(moduleExports.Panel))
check('save shows a success banner', afterSave.text.includes('已保存'))
const savedFile = JSON.parse(fs.readFileSync(path.join(DATA, 'config.json'), 'utf8'))
check('save persisted to disk', savedFile.engine?.model === 'qwen2.5:14b')
check('save kept the unknown key', savedFile.legacyBookmark?.keep === true)

// the run button starts a real background job
button(await render(() => React.createElement(moduleExports.Panel)), '概览').props.onClick()
const runTree = await render(() => React.createElement(moduleExports.Panel))
button(runTree, '立即运行').props.onClick()
await sleep(120)
const afterRun = await render(() => React.createElement(moduleExports.Panel))
check('run starts and reports', afterRun.text.includes('已开始运行') || afterRun.text.includes('已有任务在运行'))

// logs viewer (the background mock run needs a moment to flush run.log)
await sleep(1200)
button(await render(() => React.createElement(moduleExports.Panel)), '概览').props.onClick()
const logTree = await render(() => React.createElement(moduleExports.Panel))
button(logTree, '查看日志').props.onClick()
const afterLogs = await render(() => React.createElement(moduleExports.Panel))
check('log viewer renders content', afterLogs.text.includes('run: done') || afterLogs.text.includes('stage:'))

// language switch: same component tree in English
activeLang = 'en'
const enTree = await render(() => React.createElement(moduleExports.Panel))
check('english locale renders', enTree.text.includes('Subtitle Studio'))
check('english tabs present', enTree.text.includes('Overview') && enTree.text.includes('Diagnostics'))
activeLang = 'zh'

/* ------------------------------- key coverage ------------------------------ */

const describeResponse = await fetch(`http://127.0.0.1:${port}/api/dsh-subtitle-studio/describe`).then((response) => response.json())
const fieldPaths = (describeResponse.data.fields || []).map((field) => field.path)
const source = fs.readFileSync(path.join(PACKAGE, 'client.js'), 'utf8')
const literalKeys = [...source.matchAll(/i18n\.t\('([^']+)'\)/g)].map((match) => match[1])
const missingFields = fieldPaths.filter((fieldPath) => dict.zh[`f.${fieldPath}`] === undefined)
check('every field path has a zh label', missingFields.length === 0, missingFields.join(', '))
check('every field path has an en label', fieldPaths.every((fieldPath) => dict.en[`f.${fieldPath}`] !== undefined))
const missingLiterals = [...new Set(literalKeys)].filter((key) => dict.zh[key] === undefined || dict.en[key] === undefined)
check('every literal i18n key exists in both locales', missingLiterals.length === 0, missingLiterals.join(', '))
const enumOptions = new Set((describeResponse.data.fields || []).filter((field) => field.kind === 'enum').flatMap((field) => field.options))
const missingOptions = [...enumOptions].filter((option) => dict.zh[`o.${option}`] === undefined)
check('every enum option has a label', missingOptions.length === 0, missingOptions.join(', '))
/* Hints are looked up dynamically (`h.${field.path}`) behind a
   `STRINGS[key] !== undefined` guard, so a misspelled key renders nothing and
   the literal-key scan above cannot see it. Hints are optional per field, but
   every field added in this release must actually have one. */
const newPaths = ['output.mode', 'subtitles.styleTemplate',
  'transcribe.conditionOnPreviousText', 'transcribe.temperature',
  'transcribe.hallucinationSilenceThreshold', 'transcribe.minAvgLogprob',
  'transcribe.maxNoSpeechProb', 'transcribe.keepThanksPhrases', 'transcribe.keepRepeats']
const missingHints = newPaths.filter((path) =>
  dict.zh[`h.${path}`] === undefined || dict.en[`h.${path}`] === undefined ||
  dict.zh[`f.${path}`] === undefined || dict.en[`f.${path}`] === undefined)
check('every field added in this release has a bilingual label and hint',
  missingHints.length === 0, missingHints.join(', '))
check('eight tabs are declared', (source.match(/^\s+\['(overview|engine|paths|selection|prompts|quality|schedule|diagnose)',/gm) || []).length === 8)
check('no hardcoded colours', /#[0-9a-fA-F]{3,8}\b|rgba?\(/.test(source) === false)


/* ------------------- engine tab: one model dropdown (issue #1) ------------------- */

{
  const before = await render(() => React.createElement(moduleExports.Panel))
  button(before, '引擎').props.onClick()
  const eng = await render(() => React.createElement(moduleExports.Panel))

  const modelSelect = tagNodes(eng, 'select').find((node) => (node.props.children ?? []).flat(6)
    .some((c) => c?.type === 'optgroup'))
  check('engine tab: one real <select> replaces the datalist + provider + kind triangle',
    modelSelect !== undefined && tagNodes(eng, 'datalist').length === 0,
    `selects=${tagNodes(eng, 'select').length}`)

  const labels = (modelSelect?.props.children ?? []).flat(6).filter((c) => c?.type === 'optgroup').map((g) => g.props.label)
  check('engine tab: DSH providers and local Ollama are peers in that one dropdown',
    labels.some((l) => l.includes('provider-a')) && labels.some((l) => l.includes('provider-b')) && labels.some((l) => l.includes('Ollama')),
    labels.join(' | '))
  check('engine tab: both catalogs actually list their models',
    eng.text.includes('model-alpha') && eng.text.includes('model-beta')
      && eng.text.includes('qwen2.5:14b') && eng.text.includes('llama3.2:3b'))

  const manualOption = (modelSelect?.props.children ?? []).flat(6).find((c) => c?.props?.value === '__manual__')
  check('engine tab: a manual / other-engine escape hatch exists',
    manualOption !== undefined && eng.text.includes('手动填写'))

  check('engine tab: the saved selection reflects the engine it came from',
    modelSelect?.props?.value === 'ollama::qwen2.5:14b', `value=${modelSelect?.props?.value}`)
  check('engine tab: picking Ollama reveals the parameter Ollama needs',
    eng.text.includes('Ollama 地址') && button(eng, '刷新 Ollama 模型') !== undefined)
  check('engine tab: the Ollama list came from the configured host', ollamaHits.includes('/api/tags'), ollamaHits.join(','))
  check('engine tab: no base-url / keychain noise on the happy path',
    eng.text.includes('接口地址（OpenAI 兼容）') === false && eng.text.includes('钥匙串条目名') === false)

  // One pick must set kind + provider + model together.
  modelSelect.props.onChange({ target: { value: 'dsh::provider-b::model-beta' } })
  const dsh = await render(() => React.createElement(moduleExports.Panel))
  const providerBox = tagNodes(dsh, 'input').find((node) => node.props?.value === 'provider-b')
  check('engine tab: choosing a DSH model pins its provider without extra typing',
    providerBox !== undefined && dsh.text.includes('DSH 中转'), `provider=${providerBox?.props?.value}`)
  check('engine tab: switching to DSH drops the Ollama-only fields',
    dsh.text.includes('Ollama 地址') === false)

  const testButton = button(dsh, '测试模型')
  check('engine tab: the test-model action is wired', typeof testButton?.props?.onClick === 'function')
  if (testButton !== undefined) {
    const llmBefore = llmCalls.length
    await testButton.props.onClick()
    const tested = await render(() => React.createElement(moduleExports.Panel))
    const call = llmCalls[llmCalls.length - 1]
    check('engine tab: testing a DSH model calls the host llm with the picked route',
      call !== undefined && call.provider === 'provider-b' && call.model === 'model-beta' && llmCalls.length === llmBefore + 1,
      JSON.stringify({ provider: call?.provider, model: call?.model }))
    check('engine tab: the DSH answer is reported', tested.text.includes('pong') || tested.text.includes('模型可用'))
  }

  // The same button must probe Ollama instead of spending DSH credits.
  {
    const tree = await render(() => React.createElement(moduleExports.Panel))
    const select = tagNodes(tree, 'select').find((node) => (node.props.children ?? []).flat(6)
      .some((c) => c?.type === 'optgroup'))
    select.props.onChange({ target: { value: 'ollama::llama3.2:3b' } })
    const back = await render(() => React.createElement(moduleExports.Panel))
    const llmBefore = llmCalls.length
    const probe = button(back, '测试模型')
    check('engine tab: an Ollama model is testable too', typeof probe?.props?.onClick === 'function')
    if (probe !== undefined) {
      await probe.props.onClick()
      const answered = await render(() => React.createElement(moduleExports.Panel))
      check('engine tab: testing an Ollama model never routes through the DSH relay',
        llmCalls.length === llmBefore, `extra llm calls=${llmCalls.length - llmBefore}`)
      check('engine tab: the Ollama probe posts to /api/generate', ollamaHits.filter((p) => p === '/api/generate').length === 1, ollamaHits.join(','))
      check('engine tab: the Ollama answer is reported', answered.text.includes('pong 本地 llama3.2:3b'), answered.text.slice(-140))
    }
  }

  // Manual entry: a typed name survives, and the advanced row exposes the other engines.
  {
    const tree = await render(() => React.createElement(moduleExports.Panel))
    const select = tagNodes(tree, 'select').find((node) => (node.props.children ?? []).flat(6)
      .some((c) => c?.type === 'optgroup'))
    select.props.onChange({ target: { value: '__manual__' } })
    const manual = await render(() => React.createElement(moduleExports.Panel))
    const free = tagNodes(manual, 'input').find((node) => String(node.props?.placeholder ?? '').includes('填模型名'))
    check('engine tab: manual mode opens a free-text model box', free !== undefined)
    if (free !== undefined) {
      free.props.onChange({ target: { value: 'my-own-model' } })
      const typed = await render(() => React.createElement(moduleExports.Panel))
      check('engine tab: the typed model name is kept',
        tagNodes(typed, 'input').some((node) => node.props?.value === 'my-own-model'))

      const kindSelect = tagNodes(typed, 'select').find((node) => (node.props.children ?? []).flat(6)
        .some((c) => c?.props?.value === 'openai-compatible'))
      check('engine tab: the advanced row still reaches every legacy engine', kindSelect !== undefined)
      if (kindSelect !== undefined) {
        kindSelect.props.onChange({ target: { value: 'openai-compatible' } })
        const advanced = await render(() => React.createElement(moduleExports.Panel))
        check('engine tab: an OpenAI-compatible engine then asks for its own parameters',
          advanced.text.includes('接口地址（OpenAI 兼容）') && advanced.text.includes('钥匙串条目名'))
        const restore = tagNodes(advanced, 'select').find((node) => (node.props.children ?? []).flat(6)
          .some((c) => c?.props?.value === 'openai-compatible'))
        restore.props.onChange({ target: { value: 'dsh' } })
        await render(() => React.createElement(moduleExports.Panel))
      }
    }
  }

  check('engine tab: an Ollama pick does not show the DSH relay block',
    eng.text.includes('DSH 中转') === false)
  check('engine tab: a DSH pick does show it, and degrades honestly without an origin',
    dsh.text.includes('DSH 中转'))
}

/* --------- engine tab: a stale ollamaModel must be visible and clearable --------- */

{
  const tree0 = await render(() => React.createElement(moduleExports.Panel))
  button(tree0, '引擎').props.onClick()
  const tree = await render(() => React.createElement(moduleExports.Panel))
  // Same shape the Ollama-probe block already relies on: the optgroup nodes
  // hold the options, so flat()-ing one level never reaches their values.
  const picker = tagNodes(tree, 'select').find((node) => (node.props.children ?? []).flat(6)
    .some((child) => child?.type === 'optgroup'))
  check('engine tab: an Ollama entry is reachable in the unified picker', picker !== undefined)
  if (picker !== undefined) {
    picker.props.onChange({ target: { value: 'ollama::qwen2.5:14b' } })
    const eng = await render(() => React.createElement(moduleExports.Panel))
    check('engine tab: a legacy Ollama override is surfaced instead of silently winning',
      eng.text.includes('历史覆盖模型') && eng.text.includes('legacy-model:stale'))
    const clear = button(eng, '清除覆盖')
    check('engine tab: the override can be cleared from the panel', clear !== undefined)
    if (clear !== undefined) {
      clear.props.onClick()
      const cleared = await render(() => React.createElement(moduleExports.Panel))
      check('engine tab: clearing removes the warning', cleared.text.includes('历史覆盖模型') === false)
    }
  }
}

/* ---------- paths tab: SSH fields follow the endpoint kind (UX pass) ---------- */

{
  const tree0 = await render(() => React.createElement(moduleExports.Panel))
  button(tree0, '来源与输出').props.onClick()
  const paths = await render(() => React.createElement(moduleExports.Panel))
  const blocks = (paths.text.match(/SSH 连接/g) || []).length

  check('paths tab: the SSH block appears only for the endpoint that is SSH', blocks === 1, `blocks=${blocks}`)
  check('paths tab: an SSH path says type it by hand, while the local one keeps its browser',
    paths.text.includes('目录浏览器只能读本机目录') && paths.text.split('浏览…').length - 1 === 1,
    `browsers=${paths.text.split('浏览…').length - 1}`)

  const kindSelect = tagNodes(paths, 'select').find((node) => {
    const values = (node.props.children ?? []).flat(4).map((child) => child?.props?.value)
    return values.includes('local') && values.includes('ssh')
  })
  check('paths tab: the endpoint kind select is reachable', kindSelect !== undefined)
  if (kindSelect !== undefined) {
    kindSelect.props.onChange({ target: { value: 'local' } })
    const local = await render(() => React.createElement(moduleExports.Panel))
    check('paths tab: flipping the source back to local hides every SSH field',
      local.text.includes('SSH 连接') === false)
    check('paths tab: a local path gets its folder browser back',
      button(local, '浏览…') !== undefined && local.text.includes('目录浏览器只能读本机目录') === false)

    // the per-directory rows must gate the same way
    button(local, '添加目录').props.onClick()
    const withDir = await render(() => React.createElement(moduleExports.Panel))
    check('paths tab: a new local directory row starts without SSH fields',
      withDir.text.includes('SSH 连接') === false && withDir.text.includes('目录名称'))
    check('paths tab: a directory row labels its fields in words, not config keys',
      /\bkeyPath\b/.test(withDir.text) === false && withDir.text.includes('类型'))
    const dirKinds = tagNodes(withDir, 'select').filter((node) => {
      const values = (node.props.children ?? []).flat(4).map((child) => child?.props?.value)
      return values.includes('local') && values.includes('ssh')
    })
    check('paths tab: the directory row carries its own kind selects', dirKinds.length >= 2, `count=${dirKinds.length}`)
    if (dirKinds.length > 0) {
      dirKinds[0].props.onChange({ target: { value: 'ssh' } })
      const dirSsh = await render(() => React.createElement(moduleExports.Panel))
      check('paths tab: switching that directory to SSH reveals its connection fields',
        dirSsh.text.includes('SSH 连接') && dirSsh.text.includes('私钥路径'))
    }
  }
}

/* ------------------- prompts tab: the glossary field (issue #3 decoupling) ---------------- */

{
  const before = await render(() => React.createElement(moduleExports.Panel))
  button(before, '提示词').props.onClick()
  const shown = await render(() => React.createElement(moduleExports.Panel))
  check('prompts tab: the glossary field names the file it loads',
    shown.text.includes('术语表') && shown.text.includes('subtitle-studio/series'),
    shown.text.includes('术语表') ? 'missing path hint' : 'missing label')
  const profileInputs = tagNodes(shown, 'input')
  check('prompts tab: the saved profile keeps its glossary name',
    profileInputs.some((node) => node.props?.value === 'example'))
}

/* mlx_whisper 只能在 Apple Silicon 上跑。Intel 机器必须能在面板里换 faster-whisper，
   否则插件在另一类 Mac 上根本用不了。断言放在文件末尾：tagNodes 在那之前才定义好。 */
{
  const openQuality = async () => {
    const tree = await render(() => React.createElement(moduleExports.Panel))
    button(tree, '画质音频').props.onClick()
    return render(() => React.createElement(moduleExports.Panel))
  }
  // 不能只找 'faster'：编码预设（burn.preset）里也有一个叫 faster 的选项。
  const findEngine = (tree) => tagNodes(tree, 'select').find((node) => {
    const values = (node.children ?? []).flat(4).map((child) => child?.props?.value)
    return values.includes('mlx') && values.includes('faster')
  })
  const quality = await openQuality()
  const engineSelect = findEngine(quality)
  check('the transcription engine is a real panel choice',
    engineSelect !== undefined && String(engineSelect.props.value) === 'mlx',
    quality.text.includes('转写引擎') ? 'label present' : 'label missing')
  if (engineSelect !== undefined) {
    engineSelect.props.onChange({ target: { value: 'faster' } })
    const again = findEngine(await openQuality())
    check('switching the transcription engine to faster-whisper sticks',
      again !== undefined && String(again.props.value) === 'faster',
      again === undefined ? 'select gone after the change' : String(again.props.value))
  }
}

server.close()
const passed = results.filter((entry) => entry.ok).length
console.log(`\n${passed}/${results.length} checks passed, ${failures} failed`)
process.exit(failures === 0 ? 0 : 1)

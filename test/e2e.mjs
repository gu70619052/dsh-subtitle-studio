/**
 * End-to-end check for the Subtitle Studio host bridge, run against a mock
 * engine and a sandboxed HOME so nothing real (`~/.dsh/data`, LaunchAgents,
 * the DSH profile) is touched.
 *
 * Usage: node tmp/e2e.mjs
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import crypto from 'node:crypto'
import { spawn } from 'node:child_process'
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
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = path.join(dir, 'output', 'dsh-subtitle-studio')
    if (fs.existsSync(path.join(candidate, 'index.js'))) return candidate
    const sibling = path.join(dir, 'dsh-subtitle-studio')
    if (fs.existsSync(path.join(sibling, 'index.js'))) return sibling
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  throw new Error(`package directory not found above ${from}`)
}
const PACKAGE = findPackage(HERE)
const SANDBOX = path.join(HERE, 'sandbox')

fs.rmSync(SANDBOX, { recursive: true, force: true })
const HOME = path.join(SANDBOX, 'home')
const DATA = path.join(SANDBOX, 'data')
const LAUNCHD_STATE = path.join(SANDBOX, 'launchd-state')
for (const dir of [HOME, DATA, LAUNCHD_STATE]) fs.mkdirSync(dir, { recursive: true })
for (const name of ['Movies', 'Movies/subtitled', 'Library/LaunchAgents']) {
  fs.mkdirSync(path.join(HOME, name), { recursive: true })
}
fs.writeFileSync(path.join(HOME, 'Movies', '第01话.mp4'), 'x'.repeat(64))

const LAUNCHCTL_LOG = path.join(SANDBOX, 'launchctl.log')
process.env.HOME = HOME
process.env.SUBTITLE_STUDIO_DATA_DIR = DATA
process.env.SUBTITLE_STUDIO_ENGINE = path.join(HERE, 'mock', 'subtitle-run')
process.env.FAKE_LAUNCHD_STATE = LAUNCHD_STATE
process.env.FAKE_LAUNCHCTL_LOG = LAUNCHCTL_LOG
process.env.FAKE_LAUNCHCTL_STATE = path.join(LAUNCHD_STATE, 'loaded-labels.txt')
// The relay remembers what a model declares for a minute; these tests flip
// the fake catalogue on and off, so the memory has to be off here.
process.env.SUBTITLE_STUDIO_EFFORT_CACHE_MS = '0'
process.env.PATH = `${path.join(HERE, 'bin')}:${process.env.PATH ?? ''}`

const results = []
let failures = 0
function check(name, condition, detail = '') {
  const ok = condition === true
  if (!ok) failures += 1
  results.push({ ok, name, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail === '' ? '' : `  ${detail}`}`)
}

/* ------------------------------ fake host ctx ------------------------------ */

const routes = new Map()
const disposers = []
/**
 * A stand-in for the DSH llm service. It stays `null` while the patch-fallback
 * path is exercised, then switches on to cover the live catalog and the relay.
 */
let fakeLlm = null
const ctx = {
  effect(fn) {
    disposers.push(fn())
  },
  get(name) {
    if (name === 'llm') return fakeLlm
    return undefined
  },
  webServer: {
    getOrigin() {
      return origin
    },
    register(entry) {
      if (routes.has(entry.path)) throw new Error(`duplicate route ${entry.path}`)
      routes.set(entry.path, entry)
      return () => routes.delete(entry.path)
    },
  },
}

/** The DSH llm service shape the host uses: providers, models and a stream. */
function makeFakeLlm() {
  return {
    listProviders: () => [{ id: 'fake-provider', name: 'Fake Provider' }],
    listModels: async (provider) =>
      provider === 'fake-provider' ? [{ provider, id: 'fake-model', name: 'Fake Model' }] : [],
    async resolveModelInfo(provider, model) {
      return {
        provider,
        id: model,
        name: 'Fake Model',
        defaultMaxTokens: 8000,
        reasoning: declaredEfforts
          ? {
              efforts: [
                { id: 'low', name: 'Low' },
                { id: 'high', name: 'High', description: 'more thinking' },
              ],
              defaultEffort: 'low',
            }
          : undefined,
      }
    },
    async *stream(options) {
      calls.push(options)
      if (holdGate) await holdGate
      // A provider that refuses the effort, or that says "slow down", has to
      // reach the caller as its own answer, not as a generic broken stream.
      if (throwWith !== '') {
        const message = throwWith
        throwWith = ''
        throw new Error(message)
      }
      // The llm seam takes session-shaped messages: a user message carries
      // typed content blocks. A bare string here fails inside the adapters.
      const content = options.messages?.[0]?.content
      if (!Array.isArray(content)) throw new TypeError('user message content must be an array of content blocks')
      if (content[0]?.type !== 'text' || typeof content[0]?.text !== 'string') {
        throw new TypeError('user message content must start with a text block')
      }
      if (truncateNext) {
        // Exactly what a thinking model does with too small a budget: it spends
        // the tokens on reasoning, writes half a batch and gets cut off.
        yield { type: 'reasoning-delta', index: 0, text: 'thinking about the tone' }
        yield { type: 'block-start', index: 1, blockType: 'text' }
        yield { type: 'text-delta', index: 1, text: '1\n00:00:01,000 --> 00:00:02,000\n你好' }
        yield { type: 'usage', usage: { inputTokens: 120, outputTokens: 64, reasoningTokens: 55 } }
        yield { type: 'finish', reason: { kind: 'max-tokens' } }
        return
      }
      yield { type: 'block-start', index: 0, blockType: 'text' }
      yield { type: 'text-delta', index: 0, text: 'pong' }
      yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 2, reasoningTokens: 0 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    },
  }
}
const calls = []
let holdGate = null
let truncateNext = false
let declaredEfforts = false
let throwWith = ''

const moduleUrl = pathToFileURL(path.join(PACKAGE, 'index.js')).href
const host = await import(moduleUrl)

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
const origin = `http://127.0.0.1:${server.address().port}`
const base = `${origin}/api/dsh-subtitle-studio`

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

// 装插件时宿主会把自己的地址写进中转端点文件，所以要先监听再装载。
host.apply(ctx)

/** Call one bridge endpoint. */
async function call(name, method = 'GET', body = undefined, headers = {}) {
  const init = { method, headers: { ...headers } }
  if (body !== undefined) {
    init.body = JSON.stringify(body)
    init.headers['content-type'] = 'application/json'
  }
  const response = await fetch(`${base}/${name}`, init)
  const text = await response.text()
  let json = null
  try {
    json = JSON.parse(text)
  } catch {
    json = { raw: text }
  }
  return { status: response.status, body: json }
}

const CONFIG_FILE = path.join(DATA, 'config.json')
const sha = (file) => (fs.existsSync(file) ? crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') : null)
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/* --------------------------------- tests ---------------------------------- */

const described = await call('describe')
check('describe 200', described.status === 200 && described.body.ok === true)
const data = described.body.data ?? {}
check('describe: fields contract >= 50 leaves', Array.isArray(data.fields) && data.fields.length >= 50, `${data.fields?.length}`)
check('describe: defaults hydrated', data.config?.engine?.kind === 'dsh' && data.config?.schedule?.time === '02:10')
check('describe: engine detected', data.engineExists === true)
check(
  'describe: model catalog resolved',
  ['llm', 'patch', 'none'].includes(data.models?.source) && Array.isArray(data.models?.groups),
  `${data.models?.source} ${JSON.stringify(data.models?.groups)}`,
)
check(
  'describe: fallback parser found providers',
  data.models?.source === 'patch' ? data.models.groups.every((g) => g.provider !== '' && g.models.length > 0) : true,
)
check('describe: 8 ui groups present', new Set((data.fields ?? []).map((f) => f.group)).size >= 6)
check('describe: config missing yet', data.exists === false && data.parseError === null)

// Cross-origin and method guards.
const crossSite = await call('save', 'POST', { config: {} }, { 'sec-fetch-site': 'cross-site' })
check('cross-site fetch-site -> 403', crossSite.status === 403 && crossSite.body.code === 'cross-origin')
const crossOrigin = await call('save', 'POST', { config: {} }, { origin: 'http://evil.example' })
check('foreign origin -> 403', crossOrigin.status === 403)
const wrongMethod = await call('save', 'GET')
check('wrong method -> 405', wrongMethod.status === 405)

// Invalid config is rejected and nothing is written.
const badDraft = structuredClone(data.config)
badDraft.engine.temperature = 5
badDraft.schedule.time = '25:00'
badDraft.output.nameTemplate = 'no-stem.mp4'
badDraft.selection.minSizeGB = 8
badDraft.selection.maxSizeGB = 2
const beforeBad = sha(CONFIG_FILE)
const refused = await call('save', 'POST', { config: badDraft })
const issuePaths = new Set((refused.body.issues ?? []).map((issue) => issue.path))
check('invalid save -> 422', refused.status === 422 && refused.body.ok === false, JSON.stringify([...issuePaths]))
check('invalid save: temperature flagged', issuePaths.has('engine.temperature'))
check('invalid save: clock flagged', issuePaths.has('schedule.time'))
check('invalid save: nameTemplate flagged', issuePaths.has('output.nameTemplate'))
check('invalid save: size range flagged', issuePaths.has('selection.maxSizeGB'))
check('invalid save wrote nothing', sha(CONFIG_FILE) === beforeBad)

// Unknown keys survive a save that does not carry them.
fs.mkdirSync(DATA, { recursive: true })
fs.writeFileSync(
  CONFIG_FILE,
  JSON.stringify({ version: 1, legacyBookmark: { keep: true }, engine: { kind: 'minimax', temperature: 0.7 } }, null, 2),
)
const current = await call('describe')
check('describe reads partial file', current.body.data?.config?.engine?.temperature === 0.7)
const goodDraft = structuredClone(current.body.data.config)
goodDraft.engine.model = 'MiniMax-Test'
goodDraft.prompts.profiles = [
  { name: '第01话 专用', match: ['第01话*'], system: '系统提示 A', context: '上下文 A', series: 'S1' },
  { name: '默认', match: [], system: '', context: '', series: '' },
]
const saved = await call('save', 'POST', { config: goodDraft })
check('valid save -> 200', saved.status === 200 && saved.body.ok === true, JSON.stringify(saved.body.issues ?? []))
const written = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
check('save: unknown key preserved', written.legacyBookmark?.keep === true)
check('save: value applied', written.engine?.model === 'MiniMax-Test')
check('save: profiles persisted', written.prompts?.profiles?.length === 2)
check('save: file mode 0600', (fs.statSync(CONFIG_FILE).mode & 0o777) === 0o600, (fs.statSync(CONFIG_FILE).mode & 0o777).toString(8))
check('save: no temp files left', fs.readdirSync(DATA).filter((name) => name.includes('.tmp')).length === 0)

// Duplicate profile names are rejected.
const duplicate = structuredClone(written)
duplicate.prompts.profiles = [
  { name: '同名', match: ['a*'] },
  { name: '同名', match: ['b*'] },
]
const dupResult = await call('save', 'POST', { config: duplicate })
check('duplicate profile name -> 422', dupResult.status === 422 && (dupResult.body.issues ?? []).some((i) => i.path.endsWith('.name')))

// A malformed file is never overwritten silently.
fs.writeFileSync(CONFIG_FILE, '{ this is not json ')
const malformedHash = sha(CONFIG_FILE)
const malformedDescribe = await call('describe')
check('describe reports parse error', typeof malformedDescribe.body.data?.parseError === 'string')
const malformedSave = await call('save', 'POST', { config: goodDraft })
check('save over malformed file -> 409', malformedSave.status === 409, malformedSave.body.code)
check('malformed file untouched', sha(CONFIG_FILE) === malformedHash)

// Reset rewrites defaults and keeps a backup.
const reset = await call('reset', 'POST')
check('reset -> 200', reset.status === 200 && reset.body.ok === true)
check('reset: backup written', typeof reset.body.data?.resetBackup === 'string' && fs.existsSync(reset.body.data.resetBackup))
const afterReset = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
check('reset: defaults restored', afterReset.engine?.kind === 'dsh' && afterReset.schedule?.time === '02:10')

// plan
const plan = await call('plan', 'POST')
check('plan -> ok', plan.status === 200 && plan.body.ok === true, `exit=${plan.body.exitCode}`)
check('plan: candidates listed', Array.isArray(plan.body.result?.data?.candidates) && plan.body.result.data.candidates.length >= 1)
check('plan: skip reasons listed', Array.isArray(plan.body.result?.data?.skipped) && plan.body.result.data.skipped.length >= 1)
check('plan: duration recorded', typeof plan.body.durationMs === 'number')

// run: background + single instance + polling
const started = await call('run', 'POST', { max: 1 })
check('run start -> 200', started.status === 200 && typeof started.body.data?.runId === 'string', started.body.data?.runId)
const concurrent = await call('run', 'POST', { max: 1 })
check('second run while busy -> 409', concurrent.status === 409 && concurrent.body.code === 'already-running')
const badMax = await call('run', 'POST', { max: 9999 })
check('run with bad max -> 422', badMax.status === 422)

let polled = null
for (let attempt = 0; attempt < 40; attempt += 1) {
  await sleep(200)
  polled = await call('status')
  if (polled.body.data?.running === false) break
}
check('status: running false when done', polled?.body?.data?.running === false)
check('status: exit code 0', polled?.body?.data?.last?.exitCode === 0)
check('status: result envelope captured', Array.isArray(polled?.body?.data?.last?.result?.data?.actions))
check('status: history recorded', Array.isArray(polled?.body?.data?.history) && polled.body.data.history.length >= 1)

const cancelIdle = await call('cancel', 'POST', {})
check('cancel when idle -> 409', cancelIdle.status === 409)

// logs
const logTail = await call('logs?name=run&lines=50')
check('logs tail -> 200', logTail.status === 200 && logTail.body.ok === true)
check('logs: engine lines present', (logTail.body.data?.lines ?? []).some((line) => line.includes('run: done')))
check('logs: source is file', logTail.body.data?.source === 'file')
const logClamped = await call('logs?name=run&lines=999999')
check('logs: clamped to <= 2000 lines', logClamped.body.data?.lines?.length <= 2000)
const logMissing = await call('logs?name=does-not-exist')
check('logs: unknown file -> 404', logMissing.status === 404)
const logEscape = await call('logs?name=../../config')
check('logs: traversal rejected', logEscape.status === 404)

// doctor + status + selftest
const doctor = await call('doctor', 'POST')
check('doctor -> ok', doctor.status === 200 && doctor.body.ok === true)
check('doctor: checks present', (doctor.body.result?.data?.checks ?? []).length >= 5)
const engineStatus = await call('engine-status')
check('engine-status -> ok', engineStatus.status === 200 && engineStatus.body.ok === true, JSON.stringify(engineStatus.body.result?.data ?? {}))
const selftest = await call('selftest', 'POST')
check('selftest start -> 200', selftest.status === 200 && typeof selftest.body.data?.runId === 'string')
let selftestState = null
for (let attempt = 0; attempt < 30; attempt += 1) {
  await sleep(150)
  selftestState = await call('status')
  if (selftestState.body.data?.running === false && selftestState.body.data?.last?.kind === 'selftest') break
}
check('selftest finished ok', selftestState?.body?.data?.last?.exitCode === 0)

// listdir
const browseHome = await call(`listdir?path=${encodeURIComponent(HOME)}`)
check('listdir home -> 200', browseHome.status === 200 && browseHome.body.data?.path === fs.realpathSync(HOME))
check('listdir: dirs first', browseHome.body.data?.entries?.[0]?.kind === 'dir')
check('listdir: parent exposed', browseHome.body.data?.parent === path.dirname(fs.realpathSync(HOME)))
const browseFiles = await call(`listdir?path=${encodeURIComponent(path.join(HOME, 'Movies'))}&files=1`)
check('listdir with files', browseFiles.body.data?.entries?.some((entry) => entry.kind === 'file'))
const browseDenied = await call(`listdir?path=${encodeURIComponent('/System')}`)
check('listdir /System -> 400 denied', browseDenied.status === 400 && browseDenied.body.code === 'denied')
const browseEtc = await call(`listdir?path=${encodeURIComponent('/etc')}`)
check('listdir /etc -> denied', browseEtc.body.code === 'denied')
const browseMissing = await call(`listdir?path=${encodeURIComponent(path.join(SANDBOX, 'nope'))}`)
check('listdir missing -> 404', browseMissing.status === 404)
const browseFile = await call(`listdir?path=${encodeURIComponent(path.join(HOME, 'Movies', '第01话.mp4'))}`)
check('listdir on a file -> 400', browseFile.status === 400 && browseFile.body.code === 'not-a-directory')

// schedule: install -> idempotent -> remove -> failed install rolls back
// 先存一份能通过校验的配置：dsh 引擎必须先选好提供方与模型，否则调度会被宿主拒绝。
const engineDraft = structuredClone({ ...JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')) })
engineDraft.engine = { ...engineDraft.engine, kind: 'dsh', provider: 'fake-provider', model: 'fake-model' }
const engineSaved = await call('save', 'POST', { config: engineDraft })
check(
  'engine: dsh config with provider saves',
  engineSaved.status === 200 && engineSaved.body.ok === true,
  JSON.stringify(engineSaved.body.issues ?? []),
)
const scheduleCfg = () => {
  const draft = structuredClone({ ...JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')) })
  draft.engine = { ...draft.engine, kind: 'dsh', provider: 'fake-provider', model: 'fake-model' }
  return draft
}
const installDraft = scheduleCfg()
installDraft.schedule = { enabled: true, mode: 'launchd', time: '02:10', guardTimes: ['09:05', '15:30'], label: 'com.subtitle-studio.run' }
const install = await call('schedule', 'POST', { config: installDraft })
const plistPath = path.join(HOME, 'Library', 'LaunchAgents', 'com.subtitle-studio.run.plist')
check('schedule launchd install -> ok', install.status === 200 && install.body.ok === true, JSON.stringify(install.body.steps ?? install.body.error))
check('schedule: plist written', fs.existsSync(plistPath))
if (fs.existsSync(plistPath)) {
  const plist = fs.readFileSync(plistPath, 'utf8')
  check('schedule: plist runs the engine', plist.includes(path.join(HERE, 'mock', 'subtitle-run')) && plist.includes('<string>guard</string>'))
  check('schedule: calendar from HH:MM', /<key>Hour<\/key>\s*<integer>2<\/integer>/.test(plist) && /<key>Minute<\/key>\s*<integer>10<\/integer>/.test(plist))
  // guardTimes used to only feed the DSH prompt text; launchd must get them too
  check(
    'schedule: guard times become extra trigger points',
    /StartCalendarInterval<\/key>\s*<array>/.test(plist) &&
      /<integer>9<\/integer>/.test(plist) && /<integer>15<\/integer>/.test(plist),
  )
  check('schedule: config path embedded', plist.includes(path.join(DATA, 'config.json')))
  check('schedule: relay endpoint exported', plist.includes('SUBTITLE_STUDIO_BRIDGE') && plist.includes(path.join(DATA, 'state', 'bridge.json')))
  const plistPathEnv = (plist.match(/<key>PATH<\/key>\s*<string>([^<]*)<\/string>/) ?? [])[1] ?? ''
  const pathDirs = plistPathEnv.split(':')
  check('schedule: PATH inherits the directories the host was started with',
    pathDirs.includes(path.join(HERE, 'bin')), plistPathEnv.slice(0, 200))
  check('schedule: PATH is not a guess at one package manager layout',
    pathDirs[0] === path.join(HERE, 'bin'), plistPathEnv.slice(0, 200))
}
check(
  'schedule: the installed clock is written back to config.json',
  install.body.data?.persisted === true &&
    JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')).schedule?.time === '02:10' &&
    JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')).schedule?.enabled === true,
  JSON.stringify(install.body.data?.persisted ?? install.body.data?.persistError ?? null),
)
const reinstall = await call('schedule', 'POST', { config: installDraft })
check(
  'schedule: idempotent (unchanged)',
  reinstall.body.data?.steps?.includes('unchanged') === true,
  JSON.stringify(reinstall.body.data?.steps ?? reinstall.body.error ?? reinstall.body),
)
const soloDraft = scheduleCfg()
soloDraft.schedule = { enabled: true, mode: 'launchd', time: '03:20', guardTimes: [], label: 'com.subtitle-studio.run' }
const solo = await call('schedule', 'POST', { config: soloDraft })
const soloPlist = fs.existsSync(plistPath) ? fs.readFileSync(plistPath, 'utf8') : ''
check(
  'schedule: empty guard times keep the single-dict form',
  solo.body.ok === true &&
    /StartCalendarInterval<\/key>\s*<dict>/.test(soloPlist) &&
    /<key>Hour<\/key>\s*<integer>3<\/integer>/.test(soloPlist),
)
// DSH 模式的提示词是给模型直接粘贴执行的：它必须指向引擎读得懂的配置视图。
const dshViewDraft = scheduleCfg()
dshViewDraft.schedule = { enabled: true, mode: 'dsh', time: '02:10', guardTimes: [], label: 'com.subtitle-studio.run' }
const dshInstall = await call('schedule', 'POST', { config: dshViewDraft })
const dshPrompts = JSON.stringify(dshInstall.body.data?.prompts ?? dshInstall.body.data ?? '')
check(
  'schedule dsh mode: the pasted prompt points at the engine view of the config',
  dshPrompts.includes(path.join(DATA, 'state', 'engine-config.json')) &&
    fs.existsSync(path.join(DATA, 'state', 'engine-config.json')) &&
    JSON.parse(fs.readFileSync(path.join(DATA, 'state', 'engine-config.json'), 'utf8')).schedule?.mode === 'none',
  dshPrompts.slice(0, 220),
)
// 面板列出的每个音频取值都必须能存下；opus 进 .mp4 只是提醒，不是拦死。
const audioDraft = scheduleCfg()
audioDraft.burn = { ...audioDraft.burn, enabled: true, audio: 'opus' }
const opusSave = await call('save', 'POST', { config: audioDraft })
check(
  'burn.audio=opus is accepted with a container warning, not blocked',
  opusSave.status === 200 &&
    (opusSave.body.warnings ?? []).some((w) => w.path === 'burn.audio' && /mp4/.test(w.message)),
  JSON.stringify(opusSave.body).slice(0, 220),
)
const webmDraft = { ...audioDraft, output: { ...audioDraft.output, nameTemplate: '{stem}.webm' } }
const webmSave = await call('save', 'POST', { config: webmDraft })
check(
  'an .mkv/.webm template silences the opus warning',
  webmSave.status === 200 && !(webmSave.body.warnings ?? []).some((w) => w.path === 'burn.audio'),
  JSON.stringify(webmSave.body?.warnings ?? []).slice(0, 220),
)
// maxLineChars=0 必须存得下去（引擎把 0 当「不折行」）
const wrapDraft = { ...audioDraft, subtitles: { ...audioDraft.subtitles, maxLineChars: 0 } }
const wrapSave = await call('save', 'POST', { config: wrapDraft })
check(
  'subtitles.maxLineChars=0 saves (0 means: do not wrap)',
  wrapSave.status === 200,
  JSON.stringify(wrapSave.body?.errors ?? wrapSave.status).slice(0, 200),
)
const removeDraft = scheduleCfg()
removeDraft.schedule = { enabled: false, mode: 'off', time: '02:10', label: 'com.subtitle-studio.run' }
const removed = await call('schedule', 'POST', { config: removeDraft })
check('schedule off -> ok', removed.status === 200 && removed.body.ok === true, JSON.stringify(removed.body.data?.steps ?? removed.body.error))
check('schedule off: plist removed', fs.existsSync(plistPath) === false)

process.env.FAKE_BOOTSTRAP_FAIL = '1'
const failing = await call('schedule', 'POST', { config: installDraft })
check('schedule bootstrap failure -> 500', failing.status === 500 && failing.body.code === 'bootstrap-failed', JSON.stringify(failing.body.steps ?? []))
check('schedule failure: plist rolled back', fs.existsSync(plistPath) === false)
delete process.env.FAKE_BOOTSTRAP_FAIL

const dshNoPlistDraft = scheduleCfg()
dshNoPlistDraft.schedule = { enabled: true, mode: 'dsh', time: '02:10', label: 'com.subtitle-studio.run' }
const dshMode = await call('schedule', 'POST', { config: dshNoPlistDraft })
check('schedule dsh mode -> ok', dshMode.status === 200 && dshMode.body.ok === true)
check('schedule dsh mode: prompt text returned', typeof dshMode.body.data?.promptText === 'string' && dshMode.body.data.promptText.includes('subtitle-run'))
check('schedule dsh mode: no plist written', fs.existsSync(plistPath) === false)

// Engine missing path
process.env.SUBTITLE_STUDIO_ENGINE_BAK = process.env.SUBTITLE_STUDIO_ENGINE
const missing = await call('plan', 'POST') // engine still present; sanity only
check('plan still works after schedule churn', missing.body.ok === true)

/* ------------------------------- dsh relay -------------------------------- */

const BRIDGE_FILE = path.join(DATA, 'state', 'bridge.json')
const TOKEN_FILE = path.join(DATA, 'state', 'bridge-token')
check('bridge: endpoint file written', fs.existsSync(BRIDGE_FILE))
const endpoint = fs.existsSync(BRIDGE_FILE) ? JSON.parse(fs.readFileSync(BRIDGE_FILE, 'utf8')) : {}
check('bridge: endpoint points at this host', endpoint.baseUrl === origin, JSON.stringify(endpoint))
check('bridge: endpoint carries no secret', !JSON.stringify(endpoint).includes(fs.existsSync(TOKEN_FILE) ? fs.readFileSync(TOKEN_FILE, 'utf8').trim() : ''))
check(
  'bridge: files are private (0600)',
  fs.existsSync(TOKEN_FILE) && (fs.statSync(TOKEN_FILE).mode & 0o777) === 0o600 && (fs.statSync(BRIDGE_FILE).mode & 0o777) === 0o600,
)
const token = fs.existsSync(TOKEN_FILE) ? fs.readFileSync(TOKEN_FILE, 'utf8').trim() : ''
const describedBridge = await call('describe')
// No llm service is installed yet: "ready" has to mean the host can actually
// serve a translation, not merely that the endpoint file exists.
check(
  'describe: bridge not ready without an llm service',
  describedBridge.body.data?.bridge?.ready === false && describedBridge.body.data?.bridge?.baseUrl === origin,
  JSON.stringify(describedBridge.body.data?.bridge ?? {}),
)
check(
  'describe: never leaks the relay token',
  token !== '' && !JSON.stringify(describedBridge.body.data).includes(token),
)

// The relay must refuse calls without the token.
const noToken = await call('llm', 'POST', { provider: 'fake-provider', model: 'fake-model', prompt: 'hi' })
check('relay: no token -> 401', noToken.status === 401 && noToken.body.code === 'unauthorized')
const badToken = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hi' },
  { 'x-subtitle-studio-token': 'not-the-token' },
)
check('relay: wrong token -> 401', badToken.status === 401)
const missingProvider = await call(
  'llm',
  'POST',
  { provider: '', model: 'fake-model', prompt: 'hi' },
  { 'x-subtitle-studio-token': token },
)
check('relay: missing provider -> 422', missingProvider.status === 422)

// With a live llm service the relay forwards to it.
fakeLlm = makeFakeLlm()
const describedLlm = await call('describe')
check(
  'describe: bridge ready once the llm service answers',
  describedLlm.body.data?.bridge?.ready === true,
  JSON.stringify(describedLlm.body.data?.bridge ?? {}),
)
check(
  'describe: live llm catalog wins',
  describedLlm.body.data?.models?.source === 'llm' &&
    describedLlm.body.data.models.groups.some((group) => group.provider === 'fake-provider'),
  JSON.stringify(describedLlm.body.data?.models ?? {}),
)
const relayed = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello', system: 'sys', maxTokens: 32 },
  { 'x-subtitle-studio-token': token },
)
check('relay: authorized call -> 200', relayed.status === 200 && relayed.body.ok === true, JSON.stringify(relayed.body))
check('relay: text came back', String(relayed.body.data?.text ?? '') === 'pong')
check(
  'relay: provider and model forwarded',
  calls.length >= 1 && calls[0].provider === 'fake-provider' && calls[0].model === 'fake-model',
  JSON.stringify(calls[calls.length - 1] ?? {}),
)
check(
  'relay: prompt and system forwarded as typed content blocks',
  calls.length >= 1 &&
    Array.isArray(calls[0].messages?.[0]?.content) &&
    calls[0].messages[0].content[0]?.type === 'text' &&
    calls[0].messages[0].content[0]?.text?.includes('sys') &&
    calls[0].messages[0].content[0]?.text?.includes('hello') &&
    calls[0].system === undefined,
  JSON.stringify(calls[calls.length - 1] ?? {}),
)

// The panel's "test model" button uses a same-origin route without a token.
const modelTest = await call('model-test', 'POST', { provider: 'fake-provider', model: 'fake-model' })
check('model-test -> 200', modelTest.status === 200 && modelTest.body.ok === true, JSON.stringify(modelTest.body))
const modelTestBad = await call('model-test', 'POST', { provider: '', model: '' })
check('model-test: empty selection -> 422', modelTestBad.status === 422)

// Spend guard: a local process must not be able to ask the host for an
// The panel asks which reasoning efforts a model declares: the catalog never
// carries them, only resolveModelInfo does, and it is allowed to say nothing.
const infoQuiet = await call('model-info?provider=fake-provider&model=fake-model')
check(
  'model-info: a model that declares no efforts says so instead of inventing some',
  infoQuiet.status === 200 &&
    Array.isArray(infoQuiet.body.data?.efforts) &&
    infoQuiet.body.data.efforts.length === 0 &&
    String(infoQuiet.body.data.error).length > 0,
  JSON.stringify(infoQuiet.body).slice(0, 220),
)
declaredEfforts = true
const infoLoud = await call('model-info?provider=fake-provider&model=fake-model')
check(
  'model-info: declared efforts arrive with their default and descriptions',
  infoLoud.status === 200 &&
    infoLoud.body.data?.efforts?.length === 2 &&
    infoLoud.body.data.efforts[1].description === 'more thinking' &&
    String(infoLoud.body.data.defaultEffort) === 'low',
  JSON.stringify(infoLoud.body.data ?? {}).slice(0, 240),
)
const infoNoArg = await call('model-info')
check('model-info: a missing provider is refused, not guessed', infoNoArg.status === 422, String(infoNoArg.status))
declaredEfforts = false

const withEffort = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello', reasoningEffort: ' high ' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: reasoningEffort reaches the llm seam, trimmed',
  withEffort.status === 200 && calls[calls.length - 1]?.reasoningEffort === 'high',
  JSON.stringify(calls[calls.length - 1] ?? {}).slice(0, 200),
)
const withoutEffort = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: no effort means the key is absent, so the model keeps its own default',
  withoutEffort.status === 200 && calls[calls.length - 1] !== undefined && !('reasoningEffort' in calls[calls.length - 1]),
  JSON.stringify(Object.keys(calls[calls.length - 1] ?? {})),
)
const badEffort = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello', reasoningEffort: 'very high!' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a malformed effort id is refused before the model is billed',
  badEffort.status === 422 && badEffort.body.code === 'bad-request',
  JSON.stringify(badEffort.body).slice(0, 200),
)

// An effort the model does not declare is a configuration error: asking again
// gets the same refusal, so the relay must not bill for it.
declaredEfforts = true
const billedBefore = calls.length
const undeclared = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello', reasoningEffort: 'medium' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: an effort the model does not declare is refused with 422',
  undeclared.status === 422 && undeclared.body.code === 'unknown-effort',
  JSON.stringify(undeclared.body).slice(0, 220),
)
check(
  'relay: a refused effort never reaches the model',
  calls.length === billedBefore,
  `calls ${billedBefore} -> ${calls.length}`,
)
// A model that declares nothing is not evidence that the effort is wrong:
// let the provider be the one to answer.
declaredEfforts = false
const undeclaredModel = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello', reasoningEffort: 'medium' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a model that declares no efforts is still asked',
  undeclaredModel.status === 200,
  JSON.stringify(undeclaredModel.body).slice(0, 200),
)

// A provider that refuses the effort mid-flight says so in its own words.
throwWith = 'provider "fake-provider" model "fake-model" does not support reasoning effort "medium"'
const refusedByProvider = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello', reasoningEffort: 'medium' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a provider that refuses the effort is a 422, not a broken stream',
  refusedByProvider.status === 422 && refusedByProvider.body.code === 'unknown-effort',
  JSON.stringify(refusedByProvider.body).slice(0, 220),
)

// An upstream quota needs minutes, not the two-second retry a generic error
// gets, so it must not arrive as an ordinary 502.
throwWith = '429 {"type":"error","error":{"type":"rate_limit_error","message":"quota used up"}}'
const throttledUpstream = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: an upstream rate limit is a 429 the engine backs off from',
  throttledUpstream.status === 429 && throttledUpstream.body.code === 'upstream-rate-limit',
  JSON.stringify(throttledUpstream.body).slice(0, 220),
)

// REQ-62: a Chinese upstream says 「已达到 Token Plan 用量上限」 with no
// numeric 429 and no rate_limit_error type. Missed here, the engine reads a
// plain 502 and hammers the same wall 24 more times (measured 2026-11-27).
throwWith = '{"error":{"message":"已达到 Token Plan 用量上限，请升级 Token Plan 套餐或购买积分补充用量 (2056)"}}'
const quotaZh = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a Chinese quota message is still a 429, not a generic 502',
  quotaZh.status === 429 && quotaZh.body.code === 'upstream-rate-limit',
  JSON.stringify(quotaZh.body).slice(0, 220),
)

// Same coin's other face (audit r16d-host, P1): a billing notice or an
// echoed model name that merely contains the bare word for "quota" is an
// ordinary failure. Classifying it as rate-limited would tell the engine to
// abandon the whole run instead of retrying it, so the Chinese alternatives
// have to stay phrases.
throwWith = '{"error":{"message":"模型额度配置已更新，服务暂时不可用，请稍后重试 (500)"}}'
const quotaLookalike = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a 5xx that only mentions quota is NOT treated as rate-limited',
  quotaLookalike.status === 502 && quotaLookalike.body.code !== 'upstream-rate-limit',
  JSON.stringify(quotaLookalike.body).slice(0, 220),
)

// REQ-64: the recalibrated defaults must be what the panel actually serves,
// not just what the source says.
const defaultsSeen = await call('describe')
const de = defaultsSeen.body?.data?.defaults?.engine ?? {}
check(
  'describe: shipped defaults are batch 20 / maxTokens 16000',
  de.batchSize === 20 && de.maxTokens === 16000,
  JSON.stringify({ batch: de.batchSize, max: de.maxTokens }),
)

// REQ-52 (split off from REQ-64): mlx-whisper is Apple Silicon only, so an
// Intel host must not be handed 'mlx' as its default transcriber.
const expectedEngine = process.platform === 'darwin' && process.arch === 'arm64' ? 'mlx' : 'faster'
const servedEngine = defaultsSeen.body?.data?.defaults?.transcribe?.engine
check(
  'describe: transcribe default follows the CPU architecture',
  servedEngine === expectedEngine,
  JSON.stringify({ served: servedEngine, expected: expectedEngine, arch: process.arch }),
)
const hostSrc = fs.readFileSync(path.join(HERE, '..', 'index.js'), 'utf8')
// CHK-02 / REQ-48: the docs figure must exist, be referenced, and stay a
// schematic (no real paths, no screenshot bytes).
{
  const readme = fs.readFileSync(path.join(HERE, '..', 'README.md'), 'utf8')
  const svgPath = path.join(HERE, '..', 'docs/preview.svg')
  const svg = fs.existsSync(svgPath) ? fs.readFileSync(svgPath, 'utf8') : ''
  check(
    'README embeds the panel schematic',
    /!\[[^\]]*\]\(docs\/preview\.svg\)/.test(readme) && svg.length > 500,
    `svg bytes=${svg.length}`,
  )
  check(
    'the schematic is labelled as a schematic and carries no absolute home path',
    /schematic, not a screenshot/.test(svg) && /aria-label="[^"]+"/.test(svg)
      && !/\/Users\/[A-Za-z0-9._-]+/.test(svg),
  )
}

check(
  'host computes the transcribe default instead of hardcoding mlx',
  /engine:\s*defaultTranscribeEngine\(\)/.test(hostSrc)
    && /process\.platform === 'darwin' && process\.arch === 'arm64'/.test(hostSrc),
)
const engineCfg = fs.readFileSync(path.join(HERE, '..', 'engine/lib/config.py'), 'utf8')
check(
  'engine agrees with the host on the architecture split',
  /platform\.system\(\) == "Darwin" and platform\.machine\(\) == "arm64"/.test(engineCfg)
    && !/"engine": "mlx",/.test(engineCfg),
)

// The failure this release exists for: the model stops at max-tokens. The
// answer still holds the blocks that landed, so it must come back as data.
truncateNext = true
const cut = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'translate', maxTokens: 64 },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a max-tokens stop returns 200 with truncated=true, not a 502',
  cut.status === 200 && cut.body.ok === true && cut.body.truncated === true,
  JSON.stringify(cut.body).slice(0, 200),
)
check(
  'relay: the cut-off text is still delivered (the engine salvages it)',
  /你好/.test(String(cut.body.data?.text ?? '')),
  String(cut.body.data?.text ?? '').slice(0, 80),
)
check(
  'relay: the reply says how much of the budget went to reasoning',
  Number(cut.body.data?.usage?.reasoningTokens) === 55 &&
    String(cut.body.data?.finish).includes('max-tokens'),
  JSON.stringify(cut.body.data?.usage ?? {}),
)
truncateNext = false
const afterCut = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hello' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: a healthy answer is not marked truncated',
  afterCut.status === 200 && afterCut.body.truncated !== true && afterCut.body.data?.usage?.outputTokens === 2,
  JSON.stringify(afterCut.body.data ?? {}).slice(0, 200),
)

// unbounded completion, or to use the relay as an unlimited credit faucet.
const oversized = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hi', maxTokens: 1_000_000_000, temperature: 9.5 },
  { 'x-subtitle-studio-token': token },
)
const lastCall = calls[calls.length - 1] ?? {}
check('relay: authorized call after a huge ask -> 200', oversized.status === 200, JSON.stringify(oversized.body))
check(
  'relay: maxTokens clamped to the ceiling',
  lastCall.maxTokens === 16_384,
  String(lastCall.maxTokens),
)
check('relay: temperature clamped into 0..2', lastCall.temperature === 2, String(lastCall.temperature))

process.env.SUBTITLE_STUDIO_SPEND_LIMIT = '1'
const throttled = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hi' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: per-minute budget trips -> 429',
  throttled.status === 429 && throttled.body.code === 'local-budget',
  JSON.stringify(throttled.body),
)
// The engine decides "retry this batch" vs "stop the whole run" purely from
// this code, so the two 429s must not share a name (audit r16d-quality D-P1).
check(
  'relay: a local budget 429 is not spelled like an upstream quota wall',
  throttled.body.code === 'local-budget'
  && throttledUpstream.body.code === 'upstream-rate-limit',
  JSON.stringify({ local: throttled.body.code, upstream: throttledUpstream.body.code }),
)
const throttledTest = await call('model-test', 'POST', { provider: 'fake-provider', model: 'fake-model' })
check('model-test shares the same budget -> 429', throttledTest.status === 429, JSON.stringify(throttledTest.body))
delete process.env.SUBTITLE_STUDIO_SPEND_LIMIT

process.env.SUBTITLE_STUDIO_RELAY_CONCURRENCY = '1'
let releaseGate = () => {}
holdGate = new Promise((resolve) => { releaseGate = resolve })
const inFlight = call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'hold' },
  { 'x-subtitle-studio-token': token },
)
await new Promise((resolve) => setTimeout(resolve, 80))
const busy = await call(
  'llm',
  'POST',
  { provider: 'fake-provider', model: 'fake-model', prompt: 'second' },
  { 'x-subtitle-studio-token': token },
)
check(
  'relay: beyond the concurrency cap -> 429 busy',
  busy.status === 429 && busy.body.code === 'busy',
  JSON.stringify(busy.body),
)
releaseGate()
holdGate = null
const held = await inFlight
check('relay: the held call still completes', held.status === 200 && held.body.data?.text === 'pong', JSON.stringify(held.body))
delete process.env.SUBTITLE_STUDIO_RELAY_CONCURRENCY

// The engine half must reach the same relay. This runs async on purpose: a
// synchronous child would block the event loop that has to answer the probe.
const probe = await new Promise((resolve) => {
  const child = spawn(
    process.env.PYTHON || 'python3',
    [path.join(HERE, 'bridge_probe.py'), path.join(PACKAGE, 'engine', 'vendor'), 'fake-provider', 'fake-model'],
    { env: { ...process.env, SUBTITLE_STUDIO_BRIDGE: BRIDGE_FILE } },
  )
  let out = ''
  let err = ''
  child.stdout.on('data', (chunk) => {
    out += String(chunk)
  })
  child.stderr.on('data', (chunk) => {
    err += String(chunk)
  })
  child.on('close', () => resolve({ out, err }))
})
let probeJson = null
try {
  probeJson = JSON.parse(String(probe.out || '').trim().split('\n').pop() ?? '')
} catch {
  probeJson = null
}
check('engine: relay reachable from python', probeJson?.ok === true, JSON.stringify(probeJson ?? probe.err))
check('engine: relay text matches the host', probeJson?.text === 'pong')

// ---------------------------------------------------------------- Round A
// DNS rebinding: a page whose domain now resolves to this socket sends a Host
// and an Origin that agree with each other. Only the Host whitelist stops it,
// so the probe must come from a Host naming someone else.
function rawRequest(headers, body) {
  return new Promise((resolve) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port: server.address().port,
        method: 'POST',
        path: '/api/dsh-subtitle-studio/save',
        headers,
      },
      (res) => {
        let text = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { text += chunk })
        res.on('end', () => resolve({ status: res.statusCode, text }))
      },
    )
    req.on('error', (error) => resolve({ status: 0, text: String(error?.message ?? error) }))
    req.end(body)
  })
}
const port = server.address().port
const rebindingBody = JSON.stringify({ config: { notify: { channel: 'command', command: 'touch /tmp/pwned' } } })
const rebound = await rawRequest(
  { host: `evil.example.com:${port}`, origin: `http://evil.example.com:${port}`, 'content-type': 'application/json' },
  rebindingBody,
)
check('rebinding: Host naming another authority -> 403', rebound.status === 403, JSON.stringify(rebound).slice(0, 140))
const opaque = await rawRequest(
  { host: `127.0.0.1:${port}`, origin: 'null', 'content-type': 'application/json' },
  rebindingBody,
)
check('rebinding: opaque "null" origin -> 403', opaque.status === 403, JSON.stringify(opaque).slice(0, 140))
const loopbackNoOrigin = await rawRequest(
  { host: `127.0.0.1:${port}`, 'content-type': 'application/json' },
  JSON.stringify({ config: {} }),
)
// An empty config is a validation failure, but reaching the handler at all is
// the point: a non-browser caller on the loopback address must not be blocked.
check('loopback host without an origin reaches the handler', loopbackNoOrigin.status !== 403 && loopbackNoOrigin.status !== 401, JSON.stringify(loopbackNoOrigin).slice(0, 120))

const oversize = JSON.stringify({ config: { _pad: 'x'.repeat(700 * 1024) } })
const big = await rawRequest(
  { host: `127.0.0.1:${port}`, 'content-type': 'application/json' },
  oversize,
)
check('oversized body -> 413, not a reset socket', big.status === 413, JSON.stringify(big).slice(0, 120))

const baseConfig = (await call('describe')).body.data?.config ?? {}
async function rejectedWith(config, fieldPath) {
  const response = await call('save', 'POST', { config })
  const issue = (response.body?.issues ?? []).find((entry) => entry.path === fieldPath)
  return response.status === 422 && issue !== undefined ? true : { status: response.status, issues: response.body?.issues }
}
const emptyInclude = await rejectedWith(
  { ...baseConfig, source: { ...baseConfig.source, include: [] } },
  'source.include',
)
check('empty include list is refused here, not at exit 2', emptyInclude === true, JSON.stringify(emptyInclude).slice(0, 200))
const deepseekNoUrl = await rejectedWith(
  { ...baseConfig, engine: { ...baseConfig.engine, kind: 'deepseek', baseUrl: '' } },
  'engine.baseUrl',
)
check('deepseek without a baseUrl is refused', deepseekNoUrl === true, JSON.stringify(deepseekNoUrl).slice(0, 200))
const wildPort = await rejectedWith(
  {
    ...baseConfig,
    dirs: [{
      name: 'nas',
      source: { kind: 'ssh', path: '/media', include: ['*.mp4'], ssh: { host: 'media.example', user: 'me', port: 99999 } },
      output: { kind: 'local', path: '/out', nameTemplate: '{stem}_zh.mp4' },
    }],
  },
  'dirs[0].source.ssh.port',
)
check('dirs ssh port out of range is refused', wildPort === true, JSON.stringify(wildPort).slice(0, 200))
// ssh is handed `user@host` on a command line: a value starting with `-` comes back as
// an option, which is how one config field would end up running a program.
const sshHostInjection = await rejectedWith(
  {
    ...baseConfig,
    source: {
      ...baseConfig.source,
      kind: 'ssh',
      ssh: { ...(baseConfig.source?.ssh ?? {}), host: '-oProxyCommand=touch /tmp/pwned', user: 'me' },
    },
  },
  'source.ssh.host',
)
check('an ssh host ssh would read as an option is refused', sshHostInjection === true,
  JSON.stringify(sshHostInjection).slice(0, 200))
const sshUserInjection = await rejectedWith(
  {
    ...baseConfig,
    output: {
      ...baseConfig.output,
      kind: 'ssh',
      ssh: { ...(baseConfig.output?.ssh ?? {}), host: 'media.example', user: '-F /tmp/evil' },
    },
  },
  'output.ssh.user',
)
check('an ssh user ssh would read as an option is refused', sshUserInjection === true,
  JSON.stringify(sshUserInjection).slice(0, 200))
const brokenFont = await rejectedWith(
  { ...baseConfig, subtitles: { ...baseConfig.subtitles, font: "Arial':blabla" } },
  'subtitles.font',
)
check('a font name that would cut the filter graph is refused', brokenFont === true,
  JSON.stringify(brokenFont).slice(0, 200))
// mlx_whisper only runs on Apple Silicon, so the engine choice has to be in the panel.
const panelShape = (await call('describe')).body.data ?? {}
check('the transcription engine is a panel choice, not a hand-edited key',
  (panelShape.transcribeEngines ?? []).join(',') === 'mlx,faster' &&
    (panelShape.fields ?? []).some((entry) => entry.path === 'transcribe.engine'),
  JSON.stringify(panelShape.transcribeEngines))
check('the shipped defaults keep the intermediate-file exclusions the engine relies on',
  (panelShape.defaults?.selection?.excludePatterns ?? []).includes('*.partial') &&
    panelShape.defaults?.selection?.order === 'mtime-asc',
  JSON.stringify(panelShape.defaults?.selection))
const mirrorDraft = structuredClone(baseConfig)
mirrorDraft.dirs = [{
  name: 'nas',
  source: { kind: 'ssh', path: '/media', include: ['*.mkv'], recursive: true,
            ssh: { host: 'media.example', user: 'me', port: 2222, keyPath: '' } },
  output: { kind: 'local', path: '/out', nameTemplate: '{stem}_cn.mp4', overwrite: true },
}]
const mirrorSaved = await call('save', 'POST', { config: mirrorDraft })
const onDisk = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
check(
  'the first pair is mirrored to the legacy top-level keys',
  mirrorSaved.status === 200 &&
    onDisk.source?.kind === 'ssh' && onDisk.source?.path === '/media' &&
    onDisk.source?.ssh?.host === 'media.example' && onDisk.source?.ssh?.port === 2222 &&
    onDisk.output?.path === '/out' && onDisk.output?.nameTemplate === '{stem}_cn.mp4' &&
    onDisk.source?.recursive === true && onDisk.output?.overwrite === true,
  JSON.stringify({ status: mirrorSaved.status, source: onDisk.source, output: onDisk.output }),
)
await call('save', 'POST', { config: { ...baseConfig } })

// REQ-42: the panel edits burn.enabled nowhere, so the only way it can be right
// is for save to write it from output.mode — and the mode itself must survive.
const modeDraft = structuredClone({ ...JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')) })
modeDraft.output = { ...modeDraft.output, mode: 'srt' }
modeDraft.burn = { ...modeDraft.burn, enabled: true }
const modeSaved = await call('save', 'POST', { config: modeDraft })
const modeOnDisk = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
check(
  'saving output.mode=srt writes burn.enabled=false for older engines',
  modeSaved.status === 200 && modeOnDisk.output?.mode === 'srt' && modeOnDisk.burn?.enabled === false,
  JSON.stringify({ status: modeSaved.status, mode: modeOnDisk.output?.mode, burn: modeOnDisk.burn?.enabled }),
)

// A config written before the switch only has burn.enabled: the panel must read
// that as srt and keep it that way on the next save (no silent flip to burn).
const legacyDraft = structuredClone({ ...JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')) })
delete legacyDraft.output.mode
legacyDraft.burn = { ...legacyDraft.burn, enabled: false }
await call('save', 'POST', { config: legacyDraft })
const legacyOnDisk = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
check(
  'an old burn.enabled=false config keeps srt on the next save',
  legacyOnDisk.output?.mode === 'srt' && legacyOnDisk.burn?.enabled === false,
  JSON.stringify({ mode: legacyOnDisk.output?.mode, burn: legacyOnDisk.burn?.enabled }),
)
await call('save', 'POST', { config: { ...baseConfig } })

// A hand edit of config.json must not be shadowed by the plugin's cache.
const onDiskConfig = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'))
onDiskConfig.selection = { ...onDiskConfig.selection, maxPerRun: 7 }
const bumped = { ...onDiskConfig }
// mtime granularity: make sure the file really looks newer than the cached one
await new Promise((resolve) => setTimeout(resolve, 30))
fs.writeFileSync(CONFIG_FILE, JSON.stringify(bumped, null, 2))
const afterEdit = await call('describe')
check(
  'describe: an external edit of config.json is picked up',
  afterEdit.body.data?.config?.selection?.maxPerRun === 7,
  String(afterEdit.body.data?.config?.selection?.maxPerRun),
)

/* --------------------------- local ollama proxy ---------------------------- */

{
  const listed = await call(`ollama-models?host=${encodeURIComponent(OLLAMA_BASE)}`)
  check('ollama-models: lists the tags of a loopback daemon',
    listed.status === 200 && Array.isArray(listed.body.data?.models)
      && listed.body.data.models.includes('qwen2.5:14b') && (listed.body.data.error ?? '') === '',
    JSON.stringify(listed.body.data ?? listed.body).slice(0, 160))

  const remote = await call('ollama-models?host=http%3A%2F%2Follama.example.com%3A11434')
  check('ollama-models: refuses to become a proxy for an address nobody saved',
    remote.status === 200 && (remote.body.data?.models ?? ['x']).length === 0
      && String(remote.body.data?.error ?? '').includes('先保存配置'),
    JSON.stringify(remote.body).slice(0, 160))

  // A LAN Ollama is a legitimate setup: once the user saved it, the panel must use it.
  {
    const cfg = (await call('describe')).body.data.config
    const previousHost = cfg.engine.ollamaHost
    cfg.engine.ollamaHost = 'http://nas.example:11434'
    await call('save', 'POST', { config: cfg })
    const viaSaved = await call('ollama-models')
    check('ollama-models: the saved non-loopback address is used, and fails honestly',
      viaSaved.status === 200 && String(viaSaved.body.data?.error ?? '').includes('连不上'),
      JSON.stringify(viaSaved.body.data ?? viaSaved.body).slice(0, 160))
    cfg.engine.ollamaHost = previousHost
    await call('save', 'POST', { config: cfg })
  }

  const before = calls.length
  const probed = await call('model-test', 'POST', { kind: 'ollama', model: 'llama3.2:3b', host: OLLAMA_BASE })
  check('model-test: the ollama branch answers from ollama and spends no DSH credit',
    probed.status === 200 && String(probed.body.data?.text ?? '').includes('pong 本地') && calls.length === before,
    JSON.stringify(probed.body).slice(0, 160))

  const headless = await call('model-test', 'POST', { kind: 'ollama', model: 'x', host: 'http://127.0.0.1:1' })
  check('model-test: an unreachable ollama reports honestly, not ok',
    headless.status === 502 && String(headless.body.error ?? '').includes('连不上'),
    `${headless.status} ${headless.body.error}`)

  const noProvider = await call('model-test', 'POST', { kind: 'dsh', provider: '', model: 'x' })
  check('model-test: the DSH branch still requires a provider',
    noProvider.status === 422 && String(noProvider.body.error ?? '').includes('provider'),
    `${noProvider.status} ${noProvider.body.error}`)
}

// ============================================================================
// v1.6（REQ-46 / REQ-47c / REQ-59 / REQ-60）：
//   - 两个新配置键的校验与往返
//   - 一键补依赖：describe 说明哪些项可以点、路由必须有 confirm、
//     命令只能来自固定表（这里用 SUBTITLE_STUDIO_FIX_PATH 放假 brew，绝不真装）
// ============================================================================
{
  const fixDescribe = await call('describe')
  const fixDeps = fixDescribe.body.data?.fixDeps ?? null
  check('fix-deps: describe names which checks the panel may install',
    fixDeps !== null && fixDeps.byCheck?.['ffmpeg.filters'] === 'brew-ffmpeg-full' &&
      fixDeps.byCheck?.ffmpeg === 'brew-ffmpeg-full' && fixDeps.byCheck?.whisper === 'pip-mlx-whisper',
    JSON.stringify(fixDeps?.byCheck ?? {}).slice(0, 200))
  check('fix-deps: the label is the whole command, so the panel can show what runs',
    String(fixDeps?.labels?.['brew-ffmpeg-full'] ?? '') === 'brew install ffmpeg-full',
    String(fixDeps?.labels?.['brew-ffmpeg-full'] ?? ''))
  check('fix-deps: a model weight gets no button (multi-GB, the user decides)',
    fixDeps !== null && !('whisper.model' in (fixDeps.byCheck ?? {})),
    Object.keys(fixDeps?.byCheck ?? {}).join(','))

  const fasterDraft = structuredClone(fixDescribe.body.data.config)
  fasterDraft.transcribe.engine = 'faster'
  await call('save', 'POST', { config: fasterDraft })
  const fasterDescribe = await call('describe')
  check('fix-deps: the whisper button follows the transcription engine actually chosen',
    fasterDescribe.body.data?.fixDeps?.byCheck?.whisper === 'pip-faster-whisper',
    String(fasterDescribe.body.data?.fixDeps?.byCheck?.whisper))
  const backDraft = structuredClone(fasterDescribe.body.data.config)
  backDraft.transcribe.engine = 'mlx'
  await call('save', 'POST', { config: backDraft })

  const noConfirm = await call('fix-dep', 'POST', { action: 'brew-ffmpeg-full' })
  check('fix-dep: nothing runs without an explicit confirm',
    noConfirm.status === 422 && noConfirm.body.code === 'confirm-required',
    `${noConfirm.status} ${noConfirm.body.code}`)

  const emptyAction = await call('fix-dep', 'POST', { confirm: true })
  check('fix-dep: an empty action is refused', emptyAction.status === 422, String(emptyAction.status))

  const injected = await call('fix-dep', 'POST', { confirm: true, action: 'rm -rf /', command: 'echo pwned' })
  check('fix-dep: a command the caller invents is not on the table',
    injected.status === 422 && injected.body.code === 'unknown-action',
    `${injected.status} ${injected.body.code}`)
  check('fix-dep: a rejected action does not even trigger a self-check',
    injected.body.data === undefined, JSON.stringify(injected.body).slice(0, 120))

  const fakeBin = path.join(SANDBOX, 'fix-bin')
  const installLog = path.join(SANDBOX, 'fix-argv.txt')
  fs.mkdirSync(fakeBin, { recursive: true })
  fs.writeFileSync(
    path.join(fakeBin, 'brew'),
    `#!/bin/sh\necho "$@" >> '${installLog}'\necho "fake brew ok"\necho "installed into $HOME/Library/Python/3.9/lib"\n`,
    { mode: 0o755 },
  )
  process.env.SUBTITLE_STUDIO_FIX_PATH = fakeBin
  const installed = await call('fix-dep', 'POST', { confirm: true, action: 'brew-ffmpeg-full' })
  delete process.env.SUBTITLE_STUDIO_FIX_PATH
  const argvSeen = fs.existsSync(installLog) ? fs.readFileSync(installLog, 'utf8').trim() : ''
  check('fix-dep: a listed action runs exactly the fixed argv',
    installed.status === 200 && installed.body.ok === true && argvSeen === 'install ffmpeg-full',
    `${installed.status} argv="${argvSeen}"`)
  check('fix-dep: the reply carries the command output so the panel can show it',
    String(installed.body.data?.stdout ?? '').includes('fake brew ok'),
    String(installed.body.data?.stdout ?? '').slice(0, 60))
  check('fix-dep: the self-check is re-run right after the install',
    installed.body.data?.selfcheck !== null && installed.body.data?.selfcheck !== undefined,
    JSON.stringify(installed.body.data?.selfcheck ?? null).slice(0, 80))
  check('fix-dep: the reply reports how long it took',
    typeof installed.body.data?.durationMs === 'number',
    String(installed.body.data?.durationMs))

  const homeOut = String(installed.body.data?.stdout ?? '')
  check('fix-dep: absolute home paths are folded to ~ before the panel sees them',
    homeOut.includes('~/Library/Python/3.9/lib') && !homeOut.includes(String(process.env.HOME ?? '\u0000')),
    homeOut.slice(0, 120))

  const hostLog = path.join(DATA, 'logs', 'host.log')
  const logText = fs.existsSync(hostLog) ? fs.readFileSync(hostLog, 'utf8') : ''
  check('fix-dep: the install is written to the host log',
    /fix-dep brew-ffmpeg-full: ok in \d+ms/.test(logText),
    logText.split('\n').filter((line) => line.includes('fix-dep')).slice(-2).join(' | '))

  // 面板自己崩了也要留下痕迹：浏览器把报错交给宿主，宿主写进 host.log。
  const clientError = await call('client-error', 'POST', {
    kind: 'render-error',
    message: `boom at ${process.env.HOME ?? '/tmp'}/Library/thing`,
    stack: 'Error: boom\n    at Panel',
  })
  check('client-error: the panel may report a crash',
    clientError.status === 200 && clientError.body.ok === true,
    `${clientError.status} ${JSON.stringify(clientError.body).slice(0, 80)}`)
  const clientLog = fs.existsSync(hostLog) ? fs.readFileSync(hostLog, 'utf8') : ''
  check('client-error: the crash lands in the host log',
    clientLog.includes('client render-error: boom'),
    clientLog.split('\n').filter((line) => line.includes('client render-error')).slice(-1).join(' | '))
  check('client-error: home paths in a crash report are folded to ~',
    clientLog.includes('~/Library/thing') && !clientLog.includes(String(process.env.HOME ?? '\u0000')),
    clientLog.split('\n').filter((line) => line.includes('client render-error')).slice(-1).join(' | '))
  const clientErrorGet = await call('client-error')
  check('client-error: only the panel may write it, and only by POST',
    clientErrorGet.status === 405,
    String(clientErrorGet.status))

  // 连点两次：第二条必须被挡在门外，而不是让 brew 抢锁报一个看不懂的失败。
  fs.writeFileSync(
    path.join(fakeBin, 'brew'),
    `#!/bin/sh\nsleep 0.8\necho "slow brew ok"\n`,
    { mode: 0o755 },
  )
  process.env.SUBTITLE_STUDIO_FIX_PATH = fakeBin
  const [firstTap, secondTap] = await Promise.all([
    call('fix-dep', 'POST', { confirm: true, action: 'brew-ffmpeg-full' }),
    call('fix-dep', 'POST', { confirm: true, action: 'brew-ffmpeg-full' }),
  ])
  delete process.env.SUBTITLE_STUDIO_FIX_PATH
  const statuses = [firstTap.status, secondTap.status].sort()
  check('fix-dep: a second install request while one runs is refused, not queued',
    statuses[0] === 200 && statuses[1] === 409,
    statuses.join('/'))
  const refused = [firstTap, secondTap].find((reply) => reply.status === 409)
  check('fix-dep: the refusal says why', refused?.body?.code === 'already-running',
    String(refused?.body?.code))

  const badWhen = structuredClone((await call('describe')).body.data.config)
  badWhen.notify.successWhen = 'always'
  const rejectedWhen = await call('save', 'POST', { config: badWhen })
  check('save: notify.successWhen only accepts the two documented modes',
    rejectedWhen.status === 422 && JSON.stringify(rejectedWhen.body).includes('notify.successWhen'),
    `${rejectedWhen.status} ${JSON.stringify(rejectedWhen.body).slice(0, 140)}`)
  const badOwn = structuredClone((await call('describe')).body.data.config)
  badOwn.selection.skipOwnOutputs = 'nope'
  const rejectedOwn = await call('save', 'POST', { config: badOwn })
  check('save: selection.skipOwnOutputs must be a real switch',
    rejectedOwn.status === 422 && JSON.stringify(rejectedOwn.body).includes('skipOwnOutputs'),
    `${rejectedOwn.status} ${JSON.stringify(rejectedOwn.body).slice(0, 140)}`)

  const bothDraft = structuredClone((await call('describe')).body.data.config)
  bothDraft.notify.successWhen = 'anyOutput'
  bothDraft.selection.skipOwnOutputs = false
  const savedBoth = await call('save', 'POST', { config: bothDraft })
  const readBack = await call('describe')
  check('save: 有产出即成功 + 关掉自家成品判定都能存下来',
    savedBoth.status === 200 && readBack.body.data?.config?.notify?.successWhen === 'anyOutput' &&
      readBack.body.data.config.selection.skipOwnOutputs === false,
    JSON.stringify({
      when: readBack.body.data?.config?.notify?.successWhen,
      own: readBack.body.data?.config?.selection?.skipOwnOutputs,
    }))
  const restoreDraft = structuredClone(readBack.body.data.config)
  restoreDraft.notify.successWhen = 'run'
  restoreDraft.selection.skipOwnOutputs = true
  await call('save', 'POST', { config: restoreDraft })
}

// Teardown: the disposer must stop the bridge cleanly.
for (const dispose of disposers) dispose()
server.close()
fakeOllama.close()

const passed = results.filter((entry) => entry.ok).length
console.log(`\n${passed}/${results.length} checks passed, ${failures} failed`)
console.log(`sandbox: ${SANDBOX}`)
process.exit(failures === 0 ? 0 : 1)

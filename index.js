/**
 * Host half of dsh-subtitle-studio (字幕工坊 / Subtitle Studio).
 *
 * Owns the pipeline's configuration file
 * (`~/.dsh/data/subtitle-studio/config.json`, mode 0600, atomic writes, unknown
 * keys preserved), spawns the bundled engine CLI (`engine/subtitle-run`) with
 * the documented argument contract, keeps one long-running job at a time,
 * tails the engine's log files, browses local directories for the path
 * pickers, and installs/removes the launchd agent.
 *
 * The Web GUI panel talks to it through a same-origin JSON bridge:
 *
 *   GET  /api/dsh-subtitle-studio/describe      -> config + schema + models + run/schedule state
 *   POST /api/dsh-subtitle-studio/save          -> validated atomic write (422 with issues)
 *   POST /api/dsh-subtitle-studio/reset         -> defaults written back (previous file backed up)
 *   POST /api/dsh-subtitle-studio/plan          -> engine `plan` (selection only)
 *   POST /api/dsh-subtitle-studio/run           -> engine `run`, background + runId (409 when busy)
 *   POST /api/dsh-subtitle-studio/selftest      -> engine `selftest`, background + runId
 *   GET  /api/dsh-subtitle-studio/status        -> current/last job state + live line tail
 *   POST /api/dsh-subtitle-studio/cancel        -> stop the background job
 *   GET  /api/dsh-subtitle-studio/logs?name=run -> tail of logs/*.log (default 200, max 2000)
 *   GET  /api/dsh-subtitle-studio/listdir?path= -> one directory level, read-only, denylisted
 *   POST /api/dsh-subtitle-studio/doctor        -> engine `doctor` JSON
 *   GET  /api/dsh-subtitle-studio/engine-status -> engine `status` JSON (last run + today's usage)
 *   POST /api/dsh-subtitle-studio/schedule      -> install/remove the launchd agent
 *
 * Model names are suggestions only: the engine's model namespace is independent
 * of DSH's catalog, so nothing here ever rejects a model id for being unknown.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn, execFile } from 'node:child_process'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url))
const BRIDGE_PREFIX = '/api/dsh-subtitle-studio'
const MAX_JSON_BODY_BYTES = 512 * 1024
const MAX_LOG_LINES = 2000
const DEFAULT_LOG_LINES = 200
const SHORT_TIMEOUT_MS = 180_000
const RING_LINES = 2000
const HOST_LOG_MAX_BYTES = 1024 * 1024
const LISTDIR_ENTRY_LIMIT = 500
const LISTDIR_TIMEOUT_MS = 8000

/* ---------------------------------- paths --------------------------------- */

/** User data root; the sandbox moves it for tests without touching the real one. */
const DATA_DIR = path.resolve(
  process.env.SUBTITLE_STUDIO_DATA_DIR ?? path.join(os.homedir(), '.dsh', 'data', 'subtitle-studio'),
)
const CONFIG_PATH = path.join(DATA_DIR, 'config.json')
const LOGS_DIR = path.join(DATA_DIR, 'logs')
const STATE_DIR = path.join(DATA_DIR, 'state')
const LAST_RUN_PATH = path.join(STATE_DIR, 'plugin-last-run.json')
const LAUNCHD_STATE_PATH = path.join(STATE_DIR, 'launchd.json')
/** Shared secret between this Host half and the engine it spawns. */
/** Wall clock allowed for one relayed model call. */
const LLM_CALL_TIMEOUT_MS = Number(process.env.SUBTITLE_STUDIO_LLM_TIMEOUT_MS) || 240_000

const BRIDGE_TOKEN_PATH = path.join(STATE_DIR, 'bridge-token')
/** Where the engine learns which Host to call and where the token lives. */
const BRIDGE_ENDPOINT_PATH = path.join(STATE_DIR, 'bridge.json')

/** The engine CLI shipped inside this package, overridable for tests. */
const ENGINE_PATH = path.resolve(
  process.env.SUBTITLE_STUDIO_ENGINE ?? path.join(PACKAGE_DIR, 'engine', 'subtitle-run'),
)

/**
 * Engine CLI actually used: `paths.engine` when the user set one, else the CLI
 * bundled with this package. Resolved per call so a shared install never bakes
 * this machine's directory into the config.
 * @param {object} [config] - hydrated config.
 * @returns {string} absolute path to the engine CLI.
 */
function enginePathFor(config) {
  const custom = getAt(config ?? {}, 'paths.engine')
  const text = typeof custom === 'string' ? custom.trim() : ''
  return text ? path.resolve(expandHome(text)) : ENGINE_PATH
}

/** Cached hydrated config so path resolution does not re-read the file. */
let CONFIG_CACHE = null
let CONFIG_CACHE_MTIME_MS = Number.NaN

/**
 * The config the plugin is currently acting on: last saved/loaded value, else
 * the file, else the documented defaults.
 * @returns {object} hydrated config.
 */
function currentConfig() {
  // config.json is the contract between the panel, the engine CLI and launchd,
  // so an edit made outside the panel (or by the engine) has to be visible on
  // the next read: the cache is only valid while the file's mtime stands still.
  let mtimeMs = Number.NaN
  try {
    mtimeMs = fs.statSync(CONFIG_PATH).mtimeMs
  } catch {
    mtimeMs = Number.NaN
  }
  if (CONFIG_CACHE === null || CONFIG_CACHE_MTIME_MS !== mtimeMs) {
    CONFIG_CACHE = hydrate(readConfigFile().parsed)
    CONFIG_CACHE_MTIME_MS = mtimeMs
  }
  return CONFIG_CACHE
}

/** Drop the cached config after a write, so the next read sees the new file. */
function invalidateConfigCache() {
  CONFIG_CACHE = null
  CONFIG_CACHE_MTIME_MS = Number.NaN
}

/** @returns {string} absolute path of the engine CLI in effect. */
function enginePath() {
  return enginePathFor(currentConfig())
}

/**
 * Engine kinds. `dsh` runs the translation through the Host, which calls the
 * running DSH llm service: the model list, the credentials and the endpoint
 * then come from DSH's own configuration instead of a second copy of them.
 */
const ENGINE_KINDS = ['dsh', 'minimax', 'ollama', 'deepseek', 'openai-compatible']
const SELECTION_ORDERS = ['name-asc', 'name-desc', 'mtime-asc', 'mtime-desc', 'size-asc', 'size-desc']
const FFMPEG_PRESETS = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow']
const AUDIO_CODECS = ['copy', 'aac', 'opus']
const NOTIFY_CHANNELS = ['qqbot', 'command', 'webhook', 'none']
// 什么算「成功」：整轮零失败（旧语义）/ 至少有产出一部（REQ-46）
const NOTIFY_SUCCESS_WHEN = ['run', 'anyOutput']
// 能一键补的依赖：面板上每个 error/warn 若命中这里就给一个安装按钮。
// 值必须是固定的命令模板，不接受面板传入的任何自由文本（见 fix-dep 路由）。
const FIX_DEP_ACTIONS = {
  'brew-ffmpeg-full': {
    label: 'brew install ffmpeg-full',
    command: 'brew',
    args: ['install', 'ffmpeg-full'],
  },
  'pip-mlx-whisper': {
    label: 'python3 -m pip install --user mlx-whisper',
    command: 'python3',
    args: ['-m', 'pip', 'install', '--user', 'mlx-whisper'],
  },
  'pip-faster-whisper': {
    label: 'python3 -m pip install --user faster-whisper',
    command: 'python3',
    args: ['-m', 'pip', 'install', '--user', 'faster-whisper'],
  },
  'npm-minimax-mmx': {
    label: 'npm i -g @minimax/mmx',
    command: 'npm',
    args: ['i', '-g', '@minimax/mmx'],
  },
}
// 依赖检查项 → 能一键补它的动作。whisper.model 故意不在表里：模型权重
// 是几百 MB 到几 GB 的下载，与「装个依赖」不是一回事，单独再议。
const FIX_DEP_BY_CHECK = {
  ffmpeg: 'brew-ffmpeg-full',
  'ffmpeg.filters': 'brew-ffmpeg-full',
  'ffmpeg.encoder.libx264': 'brew-ffmpeg-full',
  'ffmpeg.encoder.aac': 'brew-ffmpeg-full',
  whisper: 'pip-mlx-whisper',
  'engine.mmx': 'npm-minimax-mmx',
}
const SCHEDULE_MODES = ['launchd', 'dsh', 'off']
const LINK_KINDS = ['local', 'ssh']
// 输出方式（REQ-42）：压制进视频 / 只生成字幕文件
const OUTPUT_MODES = ['burn', 'srt']
// mlx_whisper 只在 Apple Silicon 上跑得起来，所以转写引擎必须是面板可见的选项，
// 否则 Intel 机器上的用户只能手写 config.json。
const TRANSCRIBE_ENGINES = ['mlx', 'faster']

// mlx-whisper only runs on Apple Silicon. An Intel Mac (or any other host)
// has to start on faster-whisper, otherwise the very first run fails with an
// import error the user cannot act on. Decided once at load; the panel can
// still override it, and the self-check offers the matching pip install.
function defaultTranscribeEngine() {
  return process.platform === 'darwin' && process.arch === 'arm64' ? 'mlx' : 'faster'
}
// 字幕样式模板（REQ-27），与 engine/lib/style_templates.py 的预设一一对应
const STYLE_TEMPLATE_IDS = ['default', 'bold-yellow', 'top-white', 'boxed', 'cinema']

/* ------------------------------ schema + defaults -------------------------- */

/**
 * The documented default configuration (DESIGN §3). Every key the panel can edit
 * exists here, so a missing file still yields a complete, valid draft.
 */
const DEFAULT_CONFIG = Object.freeze({
  version: 1,
  engine: {
    kind: 'dsh',
    provider: '',
    model: '',
    temperature: 0.1,
    seed: 42,
    maxTokens: 16000,
    batchSize: 20,
    reasoningEffort: '',
    ollamaHost: 'http://127.0.0.1:11434',
    ollamaModel: '',
    baseUrl: '',
    apiKeyRef: '',
  },
  source: {
    kind: 'local',
    path: '',
    ssh: { host: '', port: 22, user: '', keyPath: '~/.ssh/id_ed25519' },
    include: ['*.mp4'],
    recursive: false,
  },
  output: {
    kind: 'local',
    path: '',
    ssh: { host: '', port: 22, user: '', keyPath: '~/.ssh/id_ed25519' },
    nameTemplate: '{stem}_zh.mp4',
    overwrite: false,
    // REQ-42: 'burn' 压制进视频 / 'srt' 只生成字幕文件。
    // burn.enabled 保留为兼容字段，保存时按 mode 同步（面板不再单独编辑它）。
    mode: 'burn',
  },
  // 多目录：非空时每一项是一组「来源 + 输出」；为空则沿用顶层 source / output。
  dirs: [],
  selection: {
    maxPerRun: 1,
    skipTranslated: true,
    minSizeGB: 0,
    maxSizeGB: 10,
    maxAgeDays: 36500,
    order: 'mtime-asc',
    excludePatterns: ['*.partial', '*.tmp', '*.part'],
    // 输出目录与来源目录相同时，按命名模板认出自家的成品并排掉（REQ-60）
    skipOwnOutputs: true,
  },
  prompts: {
    default: {
      system:
        '你是专业的字幕翻译。把视频里的口语内容翻译成自然、口语化的简体中文；人名与专有名词保留原文，不音译；只输出译文，不要解释。',
      context: '',
    },
    profiles: [],
  },
  subtitles: {
    bilingual: false,
    trimSilence: true,
    maxLineChars: 12,
    keepOriginalAsSrt: true,
    // 双语时中文字号（0 = 按视频高度自动）；引擎侧同名键默认也是 0
    fontSize: 0,
    // REQ-27: 字幕样式模板（颜色 / 描边 / 位置）
    styleTemplate: 'default',
  },
  burn: {
    enabled: true,
    ffmpeg: '',
    crf: 20,
    preset: 'medium',
    audio: 'copy',
    fontSize: 0,
  },
  transcribe: {
    engine: defaultTranscribeEngine(),
    whisperModel: '',
    language: '',
    keepAudio: false,
    // REQ-02 / REQ-03：反幻觉参数。0 / false = 关闭对应行为。
    conditionOnPreviousText: false,
    temperature: 0,
    hallucinationSilenceThreshold: 0,
    minAvgLogprob: 0,
    maxNoSpeechProb: 0,
    keepThanksPhrases: false,
    keepRepeats: false,
  },
  notify: {
    channel: 'none',
    qqAppId: '',
    qqOpenId: '',
    qqSecretService: 'subtitle-studio-qqbot',
    qqSendPath: '',
    command: '',
    webhookUrl: '',
    webhookBody: '',
    onStart: false,
    onSuccess: true,
    onFailure: true,
    // run = 整轮零失败才算成功（旧语义）；anyOutput = 有产出一部就算成功
    successWhen: 'run',
  },
  // `time` is the primary trigger; `guardTimes` adds extra idempotent guard
  // trigger points (the engine skips a point whose day is already handled).
  schedule: { enabled: false, mode: 'launchd', time: '02:10', guardTimes: [], label: 'com.subtitle-studio.run' },
  paths: { dataDir: '~/.dsh/data/subtitle-studio', engine: '' },
})

/**
 * The editable contract, one row per leaf. `path` is dotted; `kind` drives both
 * validation and the control the panel renders. Defaults live in DEFAULT_CONFIG
 * so this table stays about shape, not values.
 */
const FIELDS = Object.freeze([
  { path: 'engine.kind', kind: 'enum', options: ENGINE_KINDS, group: 'engine', required: true },
  { path: 'engine.provider', kind: 'string', group: 'engine', maxLength: 128 },
  { path: 'engine.model', kind: 'string', group: 'engine', required: true, maxLength: 200 },
  { path: 'engine.ollamaModel', kind: 'string', group: 'engine', maxLength: 200 },
  { path: 'engine.temperature', kind: 'number', group: 'engine', min: 0, max: 2, step: 0.05 },
  { path: 'engine.seed', kind: 'int', group: 'engine', min: 0, max: 2147483647 },
  { path: 'engine.maxTokens', kind: 'int', group: 'engine', min: 1, max: 2000000 },
  { path: 'engine.batchSize', kind: 'int', group: 'engine', min: 1, max: 10000 },
  { path: 'engine.reasoningEffort', kind: 'string', group: 'engine', maxLength: 40 },
  { path: 'engine.ollamaHost', kind: 'string', group: 'engine', url: true },
  { path: 'engine.baseUrl', kind: 'string', group: 'engine', url: true },
  { path: 'engine.apiKeyRef', kind: 'string', group: 'engine', secretRef: true, maxLength: 128 },
  { path: 'paths.engine', kind: 'string', group: 'engine', maxLength: 4096 },

  { path: 'source.kind', kind: 'enum', options: LINK_KINDS, group: 'sourceOutput', required: true },
  { path: 'source.path', kind: 'string', group: 'sourceOutput', required: true },
  { path: 'source.ssh.host', kind: 'string', group: 'sourceOutput' },
  { path: 'source.ssh.port', kind: 'int', group: 'sourceOutput', min: 1, max: 65535 },
  { path: 'source.ssh.user', kind: 'string', group: 'sourceOutput' },
  { path: 'source.ssh.keyPath', kind: 'string', group: 'sourceOutput' },
  { path: 'source.include', kind: 'list', group: 'sourceOutput' },
  { path: 'source.recursive', kind: 'bool', group: 'sourceOutput' },

  { path: 'output.kind', kind: 'enum', options: LINK_KINDS, group: 'sourceOutput', required: true },
  { path: 'output.path', kind: 'string', group: 'sourceOutput', required: true },
  { path: 'output.ssh.host', kind: 'string', group: 'sourceOutput' },
  { path: 'output.ssh.port', kind: 'int', group: 'sourceOutput', min: 1, max: 65535 },
  { path: 'output.ssh.user', kind: 'string', group: 'sourceOutput' },
  { path: 'output.ssh.keyPath', kind: 'string', group: 'sourceOutput' },
  { path: 'output.nameTemplate', kind: 'string', group: 'sourceOutput', required: true, maxLength: 200 },
  { path: 'output.overwrite', kind: 'bool', group: 'sourceOutput' },
  { path: 'output.mode', kind: 'enum', options: OUTPUT_MODES, group: 'sourceOutput' },

  { path: 'dirs', kind: 'dirlist', group: 'sourceOutput' },
  { path: 'selection.maxPerRun', kind: 'int', group: 'selection', min: 0, max: 1000 },
  { path: 'selection.skipTranslated', kind: 'bool', group: 'selection' },
  { path: 'selection.minSizeGB', kind: 'number', group: 'selection', min: 0, max: 100000, step: 0.1 },
  { path: 'selection.maxSizeGB', kind: 'number', group: 'selection', min: 0, max: 100000, step: 0.1 },
  { path: 'selection.maxAgeDays', kind: 'int', group: 'selection', min: 0, max: 100000 },
  { path: 'selection.order', kind: 'enum', options: SELECTION_ORDERS, group: 'selection' },
  { path: 'selection.excludePatterns', kind: 'list', group: 'selection' },
  { path: 'selection.skipOwnOutputs', kind: 'bool', group: 'selection' },

  { path: 'prompts.default.system', kind: 'text', group: 'prompts', maxLength: 8000 },
  { path: 'prompts.default.context', kind: 'text', group: 'prompts', maxLength: 8000 },
  { path: 'prompts.profiles', kind: 'profiles', group: 'prompts' },

  { path: 'subtitles.bilingual', kind: 'bool', group: 'quality' },
  { path: 'subtitles.trimSilence', kind: 'bool', group: 'quality' },
  { path: 'subtitles.maxLineChars', kind: 'int', group: 'quality', min: 0, max: 100 },
  { path: 'subtitles.keepOriginalAsSrt', kind: 'bool', group: 'quality' },
  { path: 'subtitles.fontSize', kind: 'int', group: 'quality', min: 0, max: 200 },
  { path: 'subtitles.styleTemplate', kind: 'enum', options: STYLE_TEMPLATE_IDS, group: 'quality' },

  { path: 'burn.ffmpeg', kind: 'string', group: 'quality' },
  { path: 'burn.crf', kind: 'int', group: 'quality', min: 0, max: 51 },
  { path: 'burn.preset', kind: 'enum', options: FFMPEG_PRESETS, group: 'quality' },
  { path: 'burn.audio', kind: 'enum', options: AUDIO_CODECS, group: 'quality' },
  { path: 'burn.fontSize', kind: 'int', group: 'quality', min: 0, max: 200 },

  { path: 'transcribe.engine', kind: 'enum', options: TRANSCRIBE_ENGINES, group: 'quality' },
  { path: 'transcribe.whisperModel', kind: 'string', group: 'quality' },
  { path: 'transcribe.language', kind: 'string', group: 'quality' },
  { path: 'transcribe.keepAudio', kind: 'bool', group: 'quality' },
  { path: 'transcribe.conditionOnPreviousText', kind: 'bool', group: 'asr' },
  { path: 'transcribe.temperature', kind: 'number', group: 'asr', min: 0, max: 2, step: 0.1 },
  { path: 'transcribe.hallucinationSilenceThreshold', kind: 'number', group: 'asr', min: 0, step: 0.1 },
  { path: 'transcribe.minAvgLogprob', kind: 'number', group: 'asr', max: 0, step: 0.1 },
  { path: 'transcribe.maxNoSpeechProb', kind: 'number', group: 'asr', min: 0, step: 0.05 },
  { path: 'transcribe.keepThanksPhrases', kind: 'bool', group: 'asr' },
  { path: 'transcribe.keepRepeats', kind: 'bool', group: 'asr' },

  { path: 'notify.channel', kind: 'enum', options: NOTIFY_CHANNELS, group: 'quality' },
  { path: 'notify.qqAppId', kind: 'string', group: 'quality', maxLength: 64 },
  { path: 'notify.qqOpenId', kind: 'string', group: 'quality', maxLength: 128 },
  { path: 'notify.qqSecretService', kind: 'string', group: 'quality', maxLength: 128 },
  { path: 'notify.command', kind: 'string', group: 'quality', maxLength: 2048 },
  { path: 'notify.webhookUrl', kind: 'string', group: 'quality', url: true, maxLength: 2048 },
  { path: 'notify.webhookBody', kind: 'string', group: 'quality', maxLength: 4096 },
  { path: 'notify.qqSendPath', kind: 'string', group: 'quality', maxLength: 2048 },
  { path: 'notify.successWhen', kind: 'enum', options: NOTIFY_SUCCESS_WHEN, group: 'quality' },
  { path: 'notify.onStart', kind: 'bool', group: 'quality' },
  { path: 'notify.onSuccess', kind: 'bool', group: 'quality' },
  { path: 'notify.onFailure', kind: 'bool', group: 'quality' },

  { path: 'schedule.enabled', kind: 'bool', group: 'schedule' },
  { path: 'schedule.mode', kind: 'enum', options: SCHEDULE_MODES, group: 'schedule' },
  { path: 'schedule.time', kind: 'time', group: 'schedule' },
  { path: 'schedule.guardTimes', kind: 'list', group: 'schedule' },
  {
    path: 'schedule.label',
    kind: 'string',
    group: 'schedule',
    maxLength: 120,
    pattern: '^[A-Za-z0-9][A-Za-z0-9.-]{2,119}$',
  },
  { path: 'paths.dataDir', kind: 'string', group: 'schedule' },
])

const FIELD_BY_PATH = new Map(FIELDS.map((field) => [field.path, field]))

/* -------------------------------- primitives ------------------------------- */

/** Plain-object test that excludes arrays and null. */
function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Deep clone plain JSON data. */
function clone(value) {
  return structuredClone(value)
}

/** Read a dotted path, returning undefined when any segment is missing. */
function getAt(root, dotted) {
  let cursor = root
  for (const segment of dotted.split('.')) {
    if (!isPlainObject(cursor) && !Array.isArray(cursor)) return undefined
    cursor = cursor[segment]
    if (cursor === undefined) return undefined
  }
  return cursor
}

/** Write a dotted path, creating intermediate objects. */
function setAt(root, dotted, value) {
  const segments = dotted.split('.')
  let cursor = root
  for (let index = 0; index < segments.length - 1; index += 1) {
    const segment = segments[index]
    if (!isPlainObject(cursor[segment])) cursor[segment] = {}
    cursor = cursor[segment]
  }
  cursor[segments[segments.length - 1]] = value
  return root
}

/** Merge `override` onto `base`; arrays and scalars replace, objects recurse. */
function deepMerge(base, override) {
  if (!isPlainObject(base) || !isPlainObject(override)) {
    return override === undefined ? base : override
  }
  const out = {}
  for (const [key, value] of Object.entries(base)) out[key] = value
  for (const [key, value] of Object.entries(override)) {
    out[key] = isPlainObject(value) && isPlainObject(base[key]) ? deepMerge(base[key], value) : value
  }
  return out
}

/** Expand a leading `~`, leaving everything else untouched. */
function expandHome(value) {
  if (typeof value !== 'string') return value
  if (value === '~') return os.homedir()
  if (value.startsWith('~/')) return path.join(os.homedir(), value.slice(2))
  return value
}

/** True when a value looks like `http(s)://host...`. */
function isHttpUrl(value) {
  try {
    const url = new URL(String(value))
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

/** Normalize `true/false/0/1/'true'/'false'` into a boolean. */
function toBool(value, fallback) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') {
    const text = value.trim().toLowerCase()
    if (text === 'true' || text === '1' || text === 'yes' || text === 'on') return true
    if (text === 'false' || text === '0' || text === 'no' || text === 'off') return false
  }
  return fallback
}

/** Normalize a string list: arrays pass through, `a,b` splits, junk is dropped. */
function toStringList(value) {
  const parts = Array.isArray(value)
    ? value
    : typeof value === 'string'
      ? value.split(',')
      : []
  return parts
    .map((part) => (typeof part === 'string' ? part.trim() : ''))
    .filter((part) => part !== '')
}

/**
 * Normalize one prompt profile, preserving keys this version does not know so a
 * round-trip never drops them.
 * @param {object} profile - raw profile.
 * @param {number} index - position, used for a fallback name.
 * @returns {object}
 */
function normalizeProfile(profile, index) {
  const source = isPlainObject(profile) ? profile : {}
  const out = { ...source }
  out.name = typeof source.name === 'string' ? source.name.trim() : ''
  if (out.name === '') out.name = `profile-${index + 1}`
  out.match = toStringList(source.match)
  out.system = typeof source.system === 'string' ? source.system : ''
  out.context = typeof source.context === 'string' ? source.context : ''
  out.series = typeof source.series === 'string' ? source.series : ''
  return out
}

/** Normalize the profile array, tolerating a single object or junk. */
function normalizeProfiles(value) {
  const list = Array.isArray(value) ? value : isPlainObject(value) ? [value] : []
  return list.map((profile, index) => normalizeProfile(profile, index))
}

/**
 * Coerce one stored value into the type its field declares, falling back to the
 * documented default when the stored value cannot be read as that type. Values
 * that are merely out of range stay out of range: validation reports them.
 * @param {object} field - schema row.
 * @param {unknown} value - stored value.
 * @returns {unknown}
 */
/**
 * Normalize the multi-directory list into the shape the engine expects.
 * Each entry carries its own source and output pair; malformed entries are
 * dropped rather than half-kept, so the engine never sees a broken pair.
 * @param {unknown} value - raw submitted value.
 * @returns {Array<object>} normalized directory entries.
 */
/** A port the engine will accept: an integer 1..65535, otherwise the default. */
function toPort(value) {
  const port = Math.trunc(Number(value))
  if (!Number.isFinite(port) || port < 1 || port > 65535) return 22
  return port
}

/**
 * Reject an ssh port the engine would refuse at load time. `report` is the
 * validator's error collector, which only exists inside validateSubmitted.
 * @param {(path: string, message: string) => void} report - error sink.
 * @param {string} pathInConfig - field path to report against.
 * @param {unknown} value - the submitted port.
 * @returns {void}
 */
function checkSshPort(report, pathInConfig, value) {
  if (value === undefined || value === null || value === '') return
  const port = Number(value)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    report(pathInConfig, 'must be an integer between 1 and 65535')
  }
}

/**
 * ssh is handed `user@host` as a single argument, and an scp/ssh option that begins
 * with `-` is read back as a flag (`-oProxyCommand=…` runs a program). A config field
 * must not be able to do that, so both halves are restricted to what a host name or a
 * user name can actually contain.
 * @param {(path: string, message: string) => void} report - error sink.
 * @param {string} pathInConfig - field path to report against.
 * @param {unknown} value - the submitted host or user.
 * @returns {void}
 */
function checkSshToken(report, pathInConfig, value) {
  const text = String(value ?? '').trim()
  if (text === '') return
  if (!/^[A-Za-z0-9._\-[\]:]+$/.test(text)) {
    report(pathInConfig, 'must be a plain host name or user name: no leading dash, spaces or quotes')
  }
}

function normalizeDirs(value) {
  if (!Array.isArray(value)) return []
  const out = []
  value.forEach((item) => {
    if (!isPlainObject(item)) return
    const source = isPlainObject(item.source) ? item.source : {}
    const output = isPlainObject(item.output) ? item.output : {}
    const sourceSsh = isPlainObject(source.ssh) ? source.ssh : {}
    const outputSsh = isPlainObject(output.ssh) ? output.ssh : {}
    out.push({
      name: typeof item.name === 'string' ? item.name.trim() : '',
      source: {
        kind: typeof source.kind === 'string' ? source.kind : 'local',
        path: typeof source.path === 'string' ? source.path : '',
        include: toStringList(source.include),
        recursive: toBool(source.recursive, false),
        ssh: {
          host: typeof sourceSsh.host === 'string' ? sourceSsh.host : '',
          port: toPort(sourceSsh.port),
          user: typeof sourceSsh.user === 'string' ? sourceSsh.user : '',
          keyPath: typeof sourceSsh.keyPath === 'string' ? sourceSsh.keyPath : '',
        },
      },
      output: {
        kind: typeof output.kind === 'string' ? output.kind : 'local',
        path: typeof output.path === 'string' ? output.path : '',
        nameTemplate: typeof output.nameTemplate === 'string' ? output.nameTemplate : '{stem}_zh.mp4',
        overwrite: toBool(output.overwrite, false),
        ssh: {
          host: typeof outputSsh.host === 'string' ? outputSsh.host : '',
          port: toPort(outputSsh.port),
          user: typeof outputSsh.user === 'string' ? outputSsh.user : '',
          keyPath: typeof outputSsh.keyPath === 'string' ? outputSsh.keyPath : '',
        },
      },
    })
  })
  return out
}

function coerceField(field, value) {
  const fallback = clone(getAt(DEFAULT_CONFIG, field.path))
  switch (field.kind) {
    case 'int': {
      const number = Number(value)
      if (value === undefined || value === null || value === '' || !Number.isFinite(number)) return fallback
      return Math.trunc(number)
    }
    case 'number': {
      const number = Number(value)
      if (value === undefined || value === null || value === '' || !Number.isFinite(number)) return fallback
      return number
    }
    case 'bool':
      return toBool(value, fallback === true)
    case 'list':
      return value === undefined || value === null ? fallback : toStringList(value)
    case 'profiles':
      return normalizeProfiles(value)
    case 'dirlist':
      return normalizeDirs(value)
    case 'enum':
    case 'time': {
      if (typeof value === 'string') return value.trim()
      if (typeof value === 'number') return String(value)
      return fallback
    }
    default:
      if (typeof value === 'string') return value
      if (typeof value === 'number' || typeof value === 'boolean') return String(value)
      return fallback
  }
}

/* ------------------------------- configuration ----------------------------- */

/**
 * Read the configuration file, reporting absence, read errors and JSON errors
 * separately so the panel can explain exactly what is wrong.
 * @returns {{exists:boolean,text:string,parsed:object|null,parseError:string|null,error:string|null,mode:string|null,size:number}}
 */
function readConfigFile() {
  try {
    const stat = fs.statSync(CONFIG_PATH)
    const text = fs.readFileSync(CONFIG_PATH, 'utf8')
    const mode = `0${(stat.mode & 0o777).toString(8)}`
    let parsed = null
    let parseError = null
    try {
      const value = JSON.parse(text)
      if (isPlainObject(value)) parsed = value
      else parseError = 'root value is not an object'
    } catch (error) {
      parseError = String(error?.message ?? error)
    }
    return { exists: true, text, parsed, parseError, error: null, mode, size: stat.size }
  } catch (error) {
    if (error?.code === 'ENOENT') {
      return { exists: false, text: '', parsed: null, parseError: null, error: null, mode: null, size: 0 }
    }
    return { exists: false, text: '', parsed: null, parseError: null, error: String(error?.message ?? error), mode: null, size: 0 }
  }
}

/** Hydrate a raw config object into the full, typed shape the panel edits. */
function hydrate(raw) {
  const merged = deepMerge(clone(DEFAULT_CONFIG), isPlainObject(raw) ? raw : {})
  for (const field of FIELDS) {
    setAt(merged, field.path, coerceField(field, getAt(merged, field.path)))
  }
  merged.prompts.profiles = normalizeProfiles(getAt(merged, 'prompts.profiles'))
  if (typeof merged.version !== 'number') merged.version = 1
  // REQ-42：旧配置没有 output.mode，用 burn.enabled 推导，避免老用户一保存
  // 就把「压制 / 只出字幕」翻过来；mode 定下来后 burn.enabled 跟着走。
  const rawMode = isPlainObject(raw) && isPlainObject(raw.output) ? String(raw.output.mode ?? '').trim() : ''
  if (rawMode !== '' && OUTPUT_MODES.includes(rawMode)) {
    merged.output.mode = rawMode
  } else {
    // 没有 mode（或写了不认识的值）：按 burn.enabled 推导
    merged.output.mode = toBool(merged.burn.enabled, true) === false ? 'srt' : 'burn'
  }
  merged.burn.enabled = merged.output.mode !== 'srt'
  return merged
}

/** Write `text` to `target` atomically, creating parent directories. */
function writeFileAtomic(target, text, mode = 0o600) {
  const directory = path.dirname(target)
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
  const temp = path.join(directory, `.${path.basename(target)}.${process.pid}.${Date.now()}.tmp`)
  const handle = fs.openSync(temp, 'wx', mode)
  try {
    fs.writeSync(handle, text, null, 'utf8')
    fs.fsyncSync(handle)
  } finally {
    fs.closeSync(handle)
  }
  try {
    fs.chmodSync(temp, mode)
  } catch {
    // Owned temp file we just created; a failed chmod is not fatal.
  }
  fs.renameSync(temp, target)
  try {
    fs.chmodSync(target, mode)
  } catch {
    // Same reasoning as above.
  }
}

/**
 * Validate the values a client submitted. Types are checked before coercion so a
 * value the user typed is never silently "fixed"; the host stays authoritative.
 * @param {object} submitted - nested config object from the request body.
 * @returns {Array<{level:string,path:string,message:string}>}
 */
function validateSubmitted(submitted) {
  const issues = []
  const error = (issuePath, message) => issues.push({ level: 'error', path: issuePath, message })
  const warn = (issuePath, message) => issues.push({ level: 'warn', path: issuePath, message })

  // Cross-field rules the engine also enforces; catching them here keeps the
  // panel's inline errors in sync with what a run would refuse.
  const notifyChannel = getAt(submitted, 'notify.channel')
  if (notifyChannel === 'qqbot') {
    const appId = String(getAt(submitted, 'notify.qqAppId') ?? '').trim()
    const openId = String(getAt(submitted, 'notify.qqOpenId') ?? '').trim()
    const sendPath = String(getAt(submitted, 'notify.qqSendPath') ?? '').trim()
    if (appId === '' && sendPath === '') {
      error('notify.qqAppId', 'is required when channel is qqbot (or fill in the fallback script path)')
    }
    if (appId !== '' && openId === '') {
      error('notify.qqOpenId', 'qqOpenId of the recipient is required once qqAppId is set')
    }
  }
  if (notifyChannel === 'command' && String(getAt(submitted, 'notify.command') ?? '').trim() === '') {
    error('notify.command', 'is required when channel is command: the command to run')
  }
  if (notifyChannel === 'webhook' && !/^https?:\/\//i.test(String(getAt(submitted, 'notify.webhookUrl') ?? '').trim())) {
    error('notify.webhookUrl', 'must start with http:// or https:// when channel is webhook')
  }

  // Multi-directory mode: every entry carries its own source/output pair.
  // `multiDir` relaxes the top-level source/output requirements, so it must be
  // known before the per-field loop runs.
  const dirEntries = getAt(submitted, 'dirs')
  const multiDir = Array.isArray(dirEntries) && dirEntries.length > 0
  if (dirEntries !== undefined && dirEntries !== null && !Array.isArray(dirEntries)) {
    error('dirs', 'must be a list of directories')
  }

  for (const field of FIELDS) {
    const value = getAt(submitted, field.path)
    if (field.kind === 'profiles') {
      if (value !== undefined && !Array.isArray(value)) error(field.path, 'must be an array')
      continue
    }
    if (value === undefined || value === null) continue
    switch (field.kind) {
      case 'enum':
        if (typeof value !== 'string' || !field.options.includes(value)) {
          error(field.path, `must be one of ${field.options.join(' / ')}`)
        }
        break
      case 'bool':
        if (typeof value !== 'boolean' && toBool(value, undefined) === undefined) {
          error(field.path, 'must be true or false')
        }
        break
      case 'int': {
        const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : NaN
        if (!Number.isFinite(numeric) || !Number.isInteger(numeric)) error(field.path, 'must be an integer')
        else if (field.min !== undefined && numeric < field.min) error(field.path, `must be at least ${field.min}`)
        else if (field.max !== undefined && numeric > field.max) error(field.path, `must be at most ${field.max}`)
        break
      }
      case 'number': {
        const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : NaN
        if (!Number.isFinite(numeric)) error(field.path, 'must be a number')
        else if (field.min !== undefined && numeric < field.min) error(field.path, `must be at least ${field.min}`)
        else if (field.max !== undefined && numeric > field.max) error(field.path, `must be at most ${field.max}`)
        break
      }
      case 'time': {
        const match = typeof value === 'string' ? /^(\d{1,2}):(\d{2})$/.exec(value.trim()) : null
        if (match === null) error(field.path, 'must be HH:MM')
        else if (Number(match[1]) > 23 || Number(match[2]) > 59) error(field.path, 'must be a valid clock time')
        break
      }
      case 'dirlist':
        // Validated entry by entry below; the generic string rules do not apply.
        break
      case 'list':
        if (!Array.isArray(value) && typeof value !== 'string') error(field.path, 'must be a list of strings')
        else if (Array.isArray(value) && value.some((entry) => typeof entry !== 'string')) {
          error(field.path, 'must be a list of strings')
        }
        break
      default: {
        if (typeof value !== 'string') {
          error(field.path, 'must be a string')
          break
        }
        if (field.maxLength !== undefined && value.length > field.maxLength) {
          error(field.path, `must be at most ${field.maxLength} characters`)
        }
        // Paths are saved as-is: an empty path only blocks the run, and the
        // engine reports it with a clearer message than the panel can.
        if (field.required === true && value.trim() === '') {
          if (field.path === 'source.path' || field.path === 'output.path') {
            warn(field.path, 'is empty: the engine refuses to run until a path is set')
          } else {
            error(field.path, 'is required')
          }
        }
        if (field.pattern !== undefined && value !== '' && !new RegExp(field.pattern, 'u').test(value)) {
          error(field.path, 'has an invalid format')
        }
        if (field.secretRef === true && value !== '') {
          // Only a keychain item name may be stored here; anything that looks like a
          // credential is rejected so a key can never land in config.json.
          if (!/^[A-Za-z0-9._:@-]{1,128}$/.test(value)) {
            error(field.path, 'must be a keychain item name, not a key')
          } else if (/^sk-|^tvly-|^Bearer\s/i.test(value)) {
            error(field.path, 'must be a keychain item name, not a key')
          }
        }
        if (field.url === true && value !== '' && !isHttpUrl(value)) {
          error(field.path, 'must be an http(s) URL')
        }
        break
      }
    }
  }

  // Multi-directory mode: every entry carries its own source/output pair.
  if (Array.isArray(dirEntries)) {
    const seenOutputPaths = new Map()
    dirEntries.forEach((item, index) => {
      const prefix = `dirs[${index}]`
      if (!isPlainObject(item)) {
        error(prefix, 'must be an object with source and output')
        return
      }
      const name = typeof item.name === 'string' ? item.name.trim() : ''
      if (name.length > 80) error(`${prefix}.name`, 'must be at most 80 characters')
      const source = isPlainObject(item.source) ? item.source : {}
      const output = isPlainObject(item.output) ? item.output : {}
      if (!LINK_KINDS.includes(source.kind)) error(`${prefix}.source.kind`, 'must be local or ssh')
      if (String(source.path ?? '').trim() === '') error(`${prefix}.source.path`, 'is required')
      if (!Array.isArray(source.include) && typeof source.include !== 'string') {
        error(`${prefix}.source.include`, 'must be a list of patterns')
      } else if (toStringList(source.include).length === 0) {
        // The engine refuses to scan with no pattern (exit 2), so the panel
        // must not let such a directory be saved at all.
        error(`${prefix}.source.include`, 'must contain at least one pattern, e.g. *.mp4')
      }
      if (source.recursive !== undefined && typeof source.recursive !== 'boolean') {
        error(`${prefix}.source.recursive`, 'must be true or false')
      }
      if (source.kind === 'ssh') {
        const ssh = isPlainObject(source.ssh) ? source.ssh : {}
        if (String(ssh.host ?? '').trim() === '') error(`${prefix}.source.ssh.host`, 'is required for ssh')
        else checkSshToken(error, `${prefix}.source.ssh.host`, ssh.host)
        if (String(ssh.user ?? '').trim() === '') error(`${prefix}.source.ssh.user`, 'is required for ssh')
        else checkSshToken(error, `${prefix}.source.ssh.user`, ssh.user)
        checkSshPort(error, `${prefix}.source.ssh.port`, ssh.port)
      }
      if (output.kind === 'ssh') {
        checkSshPort(error, `${prefix}.output.ssh.port`, isPlainObject(output.ssh) ? output.ssh.port : undefined)
      }
      if (!LINK_KINDS.includes(output.kind)) error(`${prefix}.output.kind`, 'must be local or ssh')
      if (String(output.path ?? '').trim() === '') error(`${prefix}.output.path`, 'is required')
      if (!String(output.nameTemplate ?? '').includes('{stem}')) {
        error(`${prefix}.output.nameTemplate`, 'must contain {stem}')
      }
      if (output.overwrite !== undefined && typeof output.overwrite !== 'boolean') {
        error(`${prefix}.output.overwrite`, 'must be true or false')
      }
      if (output.kind === 'ssh') {
        const ssh = isPlainObject(output.ssh) ? output.ssh : {}
        if (String(ssh.host ?? '').trim() === '') error(`${prefix}.output.ssh.host`, 'is required for ssh')
        else checkSshToken(error, `${prefix}.output.ssh.host`, ssh.host)
        if (String(ssh.user ?? '').trim() === '') error(`${prefix}.output.ssh.user`, 'is required for ssh')
        else checkSshToken(error, `${prefix}.output.ssh.user`, ssh.user)
      }
      if (
        source.kind === output.kind
        && String(source.path ?? '').trim() !== ''
        && String(source.path ?? '').trim() === String(output.path ?? '').trim()
      ) {
        warn(`${prefix}.output.path`, 'equals the source path; outputs would land among the inputs')
      }
      // Two directories writing into the same folder can overwrite each other.
      const outputPath = String(output.path ?? '').trim()
      if (outputPath !== '') {
        const previous = seenOutputPaths.get(outputPath)
        if (previous !== undefined) {
          warn(`${prefix}.output.path`, `shares the output folder with ${previous}; same-named files may overwrite each other`)
        } else {
          seenOutputPaths.set(outputPath, prefix)
        }
      }
    })
  }

  // subtitles.font reaches both the ASS writer and ffmpeg's force_style, where a
  // quote or a colon would cut the filter graph apart. Reject it as a setting error.
  const fontName = String(getAt(submitted, 'subtitles.font') ?? '').trim()
  if (fontName !== '' && (/['":;=<>\\]/.test(fontName) || /[\u0000-\u001f]/.test(fontName))) {
    error('subtitles.font', 'must not contain quotes, colons, semicolons, angle brackets or control characters')
  }

  // Single-directory mode keeps its pair at the top level, and the engine validates it
  // unconditionally (`_check_ssh` with required=True), so the panel has to agree: an ssh
  // endpoint needs a host and a user, and neither may be something ssh re-reads as an
  // option.
  for (const side of ['source', 'output']) {
    if (getAt(submitted, `${side}.kind`) !== 'ssh') continue
    const raw = getAt(submitted, `${side}.ssh`)
    const ssh = isPlainObject(raw) ? raw : {}
    const host = String(ssh.host ?? '').trim()
    if (host === '') error(`${side}.ssh.host`, 'is required for ssh')
    else checkSshToken(error, `${side}.ssh.host`, host)
    const user = String(ssh.user ?? '').trim()
    if (user === '') error(`${side}.ssh.user`, 'is required for ssh')
    else checkSshToken(error, `${side}.ssh.user`, user)
    checkSshPort(error, `${side}.ssh.port`, ssh.port)
  }

  // Trigger points: primary `schedule.time` plus optional extras, all HH:MM.
  // Extras are entry points, not load: `guard` is idempotent, so a duplicate or
  // an already-handled day is a silent no-op rather than a second run.
  const guardTimes = getAt(submitted, 'schedule.guardTimes')
  if (guardTimes !== undefined && guardTimes !== null) {
    const entries = toStringList(guardTimes)
    if (entries.length > 11) error('schedule.guardTimes', 'at most 11 extra trigger points')
    const primaryClock = parseClock(getAt(submitted, 'schedule.time'))
    const primary = primaryClock === null ? null : `${String(primaryClock.hour).padStart(2, '0')}:${String(primaryClock.minute).padStart(2, '0')}`
    const seen = new Set()
    entries.forEach((entry, index) => {
      const clock = parseClock(entry)
      if (clock === null) {
        error(`schedule.guardTimes.${index}`, 'must be HH:MM')
        return
      }
      const normalized = `${String(clock.hour).padStart(2, '0')}:${String(clock.minute).padStart(2, '0')}`
      if (seen.has(normalized)) {
        error(`schedule.guardTimes.${index}`, 'must be unique')
      } else if (normalized === primary) {
        warn(`schedule.guardTimes.${index}`, 'duplicates the primary time; it will be ignored')
      }
      seen.add(normalized)
    })
  }

  const profiles = getAt(submitted, 'prompts.profiles')
  if (Array.isArray(profiles)) {
    const seen = new Set()
    profiles.forEach((profile, index) => {
      const prefix = `prompts.profiles.${index}`
      if (!isPlainObject(profile)) {
        error(prefix, 'must be an object')
        return
      }
      const name = typeof profile.name === 'string' ? profile.name.trim() : ''
      if (name === '') {
        error(`${prefix}.name`, 'is required')
      } else if (name.length > 80) {
        error(`${prefix}.name`, 'must be at most 80 characters')
      } else if (seen.has(name.toLowerCase())) {
        error(`${prefix}.name`, 'must be unique')
      } else {
        seen.add(name.toLowerCase())
      }
      if (profile.match !== undefined && !Array.isArray(profile.match) && typeof profile.match !== 'string') {
        error(`${prefix}.match`, 'must be a list of patterns')
      } else if (toStringList(profile.match).length === 0) {
        warn(`${prefix}.match`, 'no pattern: this profile catches every video below it')
      }
      for (const key of ['system', 'context', 'series']) {
        if (profile[key] !== undefined && typeof profile[key] !== 'string') {
          error(`${prefix}.${key}`, 'must be a string')
        }
      }
      const glossary = String(profile.series ?? '').trim()
      if (glossary !== '') {
        // The engine resolves this name into a file under the user's glossary
        // directory, so it must stay a bare name: no separators, no traversal.
        if (glossary.length > 64) {
          error(`${prefix}.series`, 'must be at most 64 characters')
        } else if (/[/\\]|\.\.|\u0000/.test(glossary)) {
          error(`${prefix}.series`, 'must be a glossary file name without slashes or ".."')
        }
      }
    })
  }

  // Cross-field rules that a per-field check cannot express.
  const candidate = hydrate(isPlainObject(submitted) ? submitted : {})
  const engine = candidate.engine
  if (engine.kind === 'openai-compatible' && !isHttpUrl(engine.baseUrl)) {
    error('engine.baseUrl', 'is required for the openai-compatible engine')
  }
  // deepseek is the older alias of the same HTTP path; both need a base URL.
  if (engine.kind === 'deepseek' && !isHttpUrl(engine.baseUrl)) {
    error('engine.baseUrl', 'is required for the deepseek engine')
  }
  // An effort id is handed to the model adapter verbatim, so it is an
  // identifier, not free text. Which ids are legal is per-model and can only be
  // asked of the host (the `model-info` route), so this rejects only the shapes
  // that are certainly wrong.
  const effort = String(engine.reasoningEffort ?? '').trim()
  if (effort !== '' && !/^[A-Za-z0-9._-]{1,40}$/.test(effort)) {
    error('engine.reasoningEffort', 'must be a plain identifier such as low / medium / high, or empty to follow the model default')
  }
  if (toStringList(getAt(submitted, 'source.include')).length === 0) {
    error('source.include', 'must contain at least one pattern, e.g. *.mp4')
  }
  if (engine.kind === 'ollama' && !isHttpUrl(engine.ollamaHost)) {
    error('engine.ollamaHost', 'must be an http(s) URL for the ollama engine')
  }
  if (engine.kind === 'dsh') {
    // The dsh engine borrows DSH's own provider routing, so a provider has to
    // be named; the model id may still be typed by hand for a route the
    // catalog does not advertise.
    if (String(engine.provider ?? '').trim() === '') {
      error('engine.provider', 'is required for the dsh engine')
    }
  }
  if (engine.kind !== 'ollama' && engine.kind !== 'dsh' && String(engine.apiKeyRef ?? '').trim() === '') {
    warn('engine.apiKeyRef', 'no keychain item: the engine will run without a credential')
  }
  if (candidate.selection.maxSizeGB < candidate.selection.minSizeGB) {
    error('selection.maxSizeGB', 'must not be below the minimum size')
  }
  if (!String(candidate.output.nameTemplate).includes('{stem}')) {
    error('output.nameTemplate', 'must contain {stem}')
  }
  const sameTarget = candidate.source.kind === candidate.output.kind
    && String(candidate.source.path ?? '') !== ''
    && candidate.source.path === candidate.output.path
  if (sameTarget) warn('output.path', 'equals the source path; outputs would land among the inputs')
  // ffmpeg 两种输出方式都要用（抽音频、分段、ffprobe），不只是压制
  if (String(candidate.burn.ffmpeg).trim() === '') {
    warn('burn.ffmpeg', 'left empty: ffmpeg is looked up on PATH')
  }
  // The engine does encode Opus (libopus), but an .mp4 container carries it
  // badly on most players — say so instead of letting the run "succeed".
  if (String(candidate.burn.audio ?? '') === 'opus') {
    const templates = [candidate.output.nameTemplate, ...(Array.isArray(dirEntries) ? dirEntries : [])
      .map((entry) => entry?.output?.nameTemplate)]
      .filter((value) => typeof value === 'string' && value !== '')
    if (templates.length > 0 && templates.every((value) => /\.mp4$/i.test(String(value).trim()))) {
      warn('burn.audio', 'Opus inside .mp4 plays badly on many players: pick aac, or an .mkv/.webm template')
    }
  }
  const language = String(candidate.transcribe.language ?? '').trim()
  if (language !== '' && !/^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$/.test(language)) {
    error('transcribe.language', 'must be a language code such as ja or zh-CN')
  }
  return issues
}

/**
 * Mirror the first directory pair onto the top-level source/output fields.
 *
 * The engine reads `dirs` first and falls back to the top-level pair, so this
 * only matters for older engines that ignore `dirs` entirely: without the
 * mirror they would see an empty path and refuse to run. The mirror is
 * applied at write time only, so the panel keeps showing what the user typed.
 * @param {object} config - hydrated configuration object (mutated in place).
 * @returns {object} the same configuration object.
 */
function mirrorFirstDir(config) {
  const dirs = Array.isArray(config?.dirs) ? config.dirs : []
  if (dirs.length === 0) return config
  const first = dirs[0]
  if (!isPlainObject(first)) return config
  const source = isPlainObject(first.source) ? first.source : {}
  const output = isPlainObject(first.output) ? first.output : {}
  if (!isPlainObject(config.source)) config.source = {}
  if (!isPlainObject(config.output)) config.output = {}
  if (String(source.path ?? '').trim() !== '') config.source.path = source.path
  if (String(output.path ?? '').trim() !== '') config.output.path = output.path
  // Kind and ssh endpoint too: an older engine that ignores `dirs` would
  // otherwise treat a remote pair as a local one and find nothing to do.
  for (const [side, values] of [['source', source], ['output', output]]) {
    const kind = String(values.kind ?? '').trim()
    if (kind !== '') config[side].kind = kind
    if (isPlainObject(values.ssh)) config[side].ssh = { ...(config[side].ssh ?? {}), ...values.ssh }
    if (side === 'output' && String(values.nameTemplate ?? '').trim() !== '') {
      config.output.nameTemplate = values.nameTemplate
    }
    if (side === 'source' && Array.isArray(values.include) && values.include.length > 0) {
      config.source.include = values.include
    }
    if (side === 'source' && values.recursive !== undefined) config.source.recursive = values.recursive === true
    if (side === 'output' && values.overwrite !== undefined) config.output.overwrite = values.overwrite === true
  }
  return config
}

/**
 * Persist a submitted configuration. Unknown keys already in the file survive;
 * a malformed existing file is never overwritten silently.
 * @param {object} submitted - nested config object.
 * @returns {{ok:boolean, issues?:Array<object>, code?:string, message?:string}}
 */
function saveConfig(submitted) {
  const issues = validateSubmitted(submitted)
  if (issues.some((issue) => issue.level === 'error')) return { ok: false, issues }
  const current = readConfigFile()
  if (current.exists && current.parseError !== null) {
    return {
      ok: false,
      code: 'config-malformed',
      message: `existing config.json is not valid JSON: ${current.parseError}. Fix it, or use 还原 to rewrite defaults (the old file is backed up).`,
    }
  }
  const candidate = mirrorFirstDir(hydrate(submitted))
  // REQ-42：mode 是唯一的用户语义，burn.enabled 只作为旧引擎的兼容字段跟着走
  candidate.burn.enabled = candidate.output.mode !== 'srt'
  const merged = current.parsed === null ? candidate : deepMerge(clone(current.parsed), candidate)
  const text = `${JSON.stringify(merged, null, 2)}\n`
  try {
    writeFileAtomic(CONFIG_PATH, text, 0o600)
  } catch (error) {
    return { ok: false, code: 'write-failed', message: String(error?.message ?? error) }
  }
  invalidateConfigCache()
  return { ok: true, issues }
}

/**
 * Rewrite the configuration from the documented defaults. The previous file is
 * copied to `config.json.bak` first, so a malformed file is recoverable.
 * @returns {{ok:boolean, backup:string|null, code?:string, message?:string}}
 */
function resetConfig() {
  fs.mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 })
  const current = readConfigFile()
  let backup = null
  if (current.exists) {
    backup = `${CONFIG_PATH}.bak`
    try {
      fs.copyFileSync(CONFIG_PATH, backup)
      fs.chmodSync(backup, 0o600)
    } catch (error) {
      return { ok: false, backup: null, code: 'backup-failed', message: String(error?.message ?? error) }
    }
  }
  const unknown = isPlainObject(current.parsed)
    ? deepMerge(clone(current.parsed), {})
    : {}
  // Start from the defaults, then keep keys the schema does not know about.
  const merged = deepMerge(clone(DEFAULT_CONFIG), {})
  const carryUnknown = (base, existing) => {
    for (const [key, value] of Object.entries(existing)) {
      const known = FIELD_BY_PATH.has(key) || ['version', 'engine', 'source', 'output', 'selection', 'prompts', 'subtitles', 'burn', 'transcribe', 'notify', 'schedule', 'paths'].includes(key)
      if (!known) {
        base[key] = value
        continue
      }
      if (isPlainObject(value) && isPlainObject(base[key])) carryUnknown(base[key], value)
    }
  }
  if (isPlainObject(unknown)) carryUnknown(merged, unknown)
  try {
    writeFileAtomic(CONFIG_PATH, `${JSON.stringify(merged, null, 2)}\n`, 0o600)
  } catch (error) {
    return { ok: false, backup, code: 'write-failed', message: String(error?.message ?? error) }
  }
  invalidateConfigCache()
  return { ok: true, backup }
}

/* ----------------------------------- models -------------------------------- */

/**
 * Model catalog advertised by the running DSH llm service. An empty result means
 * "no catalog" (a provider without credentials answers empty), not an error.
 * @param {object} ctx - host context.
 * @returns {Promise<{source:string,groups:Array<object>,error:string|null}>}
 */
async function modelsFromLlm(ctx) {
  let llm
  try {
    llm = ctx.get('llm')
  } catch {
    return { source: 'llm', groups: [], error: 'llm service unavailable' }
  }
  if (llm === undefined || llm === null || typeof llm.listProviders !== 'function') {
    return { source: 'llm', groups: [], error: 'llm service unavailable' }
  }
  try {
    const providers = await llm.listProviders()
    const groups = []
    for (const provider of providers) {
      const id = String(provider.id)
      let models = []
      try {
        models = await llm.listModels(id)
      } catch {
        models = []
      }
      const ids = models.map((model) => String(model.id)).filter((modelId) => modelId !== '')
      if (ids.length > 0) groups.push({ provider: id, models: ids })
    }
    return { source: 'llm', groups, error: null }
  } catch (error) {
    return { source: 'llm', groups: [], error: String(error?.message ?? error) }
  }
}

/** The profile directory this install runs from. */
function profileDir() {
  const explicit = process.env.DSH_PROFILE_DIR
  if (typeof explicit === 'string' && explicit !== '') return explicit
  return path.join(os.homedir(), '.dsh', 'profiles', process.env.DSH_PROFILE || 'desktop')
}

/**
 * Fallback catalog: the `providers:` mapping of the profile patch layer, read
 * only (never rewritten). Supports block lists and inline `[{id: ...}]` lists.
 * @returns {{source:string,groups:Array<object>,error:string|null}}
 */
function modelsFromPatch() {
  let text
  try {
    text = fs.readFileSync(path.join(profileDir(), 'cordis.patch.yml'), 'utf8')
  } catch (error) {
    return { source: 'none', groups: [], error: String(error?.message ?? error) }
  }
  const groups = []
  let providersIndent = -1
  let current = null
  for (const line of text.split(/\r?\n/)) {
    if (providersIndent < 0) {
      const open = /^(\s*)providers:\s*$/.exec(line)
      if (open !== null) {
        providersIndent = open[1].length
        current = null
      }
      continue
    }
    if (/^\s*$/.test(line) || /^\s*#/.test(line)) continue
    const indent = line.length - line.trimStart().length
    if (indent <= providersIndent) {
      providersIndent = -1
      current = null
      const open = /^(\s*)providers:\s*$/.exec(line)
      if (open !== null) providersIndent = open[1].length
      continue
    }
    const providerKey = /^\s{2,}([A-Za-z0-9_][A-Za-z0-9_.-]*):\s*$/.exec(line)
    if (providerKey !== null && indent === providersIndent + 2) {
      current = { provider: providerKey[1], models: [] }
      groups.push(current)
      continue
    }
    if (current === null) continue
    for (const match of line.matchAll(/\bid:\s*([A-Za-z0-9@._:/+-]+)/g)) {
      if (!current.models.includes(match[1])) current.models.push(match[1])
    }
  }
  const filled = groups.filter((group) => group.models.length > 0)
  return filled.length > 0
    ? { source: 'patch', groups: filled, error: null }
    : { source: 'none', groups: [], error: 'no providers declared in the profile patch' }
}

/** Resolve the model catalog with llm-first, patch-second precedence. */
async function resolveModels(ctx) {
  const live = await modelsFromLlm(ctx)
  if (live.groups.length > 0) return live
  const patched = modelsFromPatch()
  if (patched.groups.length > 0) return patched
  return { source: 'none', groups: [], error: live.error ?? patched.error }
}

/* -------------------------------- llm bridge -------------------------------- */

/**
 * The shared secret between this Host half and the engine it spawns. It lives
 * in the state directory with mode 0600, is created on demand, and never
 * reaches config.json, the panel, a log line or a crash report.
 * @returns {string} the token, or an empty string when it cannot be stored.
 */
function bridgeToken() {
  try {
    const text = fs.readFileSync(BRIDGE_TOKEN_PATH, 'utf8').trim()
    if (text.length >= 32) return text
  } catch {
    // No token yet (or it cannot be read): create one below.
  }
  try {
    const token = randomBytes(32).toString('hex')
    writeFileAtomic(BRIDGE_TOKEN_PATH, `${token}\n`, 0o600)
    return token
  } catch (error) {
    appendHostLog(`bridge token could not be written: ${String(error?.message ?? error)}`)
    return ''
  }
}

/**
 * Describe where the engine can reach this Host: the origin of the running
 * DSH Web GUI plus the path of the shared token. The engine reads this file at
 * call time, so a port change never leaves a stale endpoint behind.
 * @param {string} origin - `ctx.webServer.getOrigin()`.
 * @returns {{baseUrl:string, tokenPath:string, bridgePath:string, updatedAt:string}|null}
 */
/**
 * True only for a URL that points back at this machine. The relay carries
 * subtitle text, so an endpoint file that names some other host is treated as
 * damage (or as a rewritten file) rather than as an instruction to follow.
 * @param {string} value - candidate base URL.
 * @returns {boolean}
 */
/**
 * What a model says it can do, remembered briefly. The relay needs it to reject
 * a reasoning effort the provider will refuse, instead of paying for a stream
 * that dies on the first token; a live check costs one metadata call per model
 * per minute, not per batch.
 * @type {Map<string, {at:number, efforts:string[], error:string}>}
 */
const EFFORT_CACHE = new Map()
/** How long a model's declared efforts stay fresh. Zero turns the cache off. */
const effortCacheMs = () => {
  const value = Number(process.env.SUBTITLE_STUDIO_EFFORT_CACHE_MS)
  return Number.isFinite(value) && value >= 0 ? Math.trunc(value) : 60_000
}

/**
 * @param {object} ctx - the plugin context.
 * @param {string} provider
 * @param {string} model
 * @returns {Promise<{efforts:string[], error:string}>} declared effort ids, and
 *   why there are none. An empty list means "nothing declared", not "no".
 */
async function declaredEfforts(ctx, provider, model) {
  const key = `${provider}\u0000${model}`
  const ttl = effortCacheMs()
  const hit = EFFORT_CACHE.get(key)
  if (ttl > 0 && hit && Date.now() - hit.at < ttl) return hit
  let entry = { at: Date.now(), efforts: [], error: '' }
  try {
    const llm = ctx.get('llm')
    if (typeof llm?.resolveModelInfo !== 'function') {
      entry = { ...entry, error: 'DSH does not expose model capabilities' }
    } else {
      const info = await llm.resolveModelInfo(provider, model)
      entry = {
        at: Date.now(),
        efforts: (info?.reasoning?.efforts ?? []).map((row) => String(row?.id ?? '')).filter((id) => id !== ''),
        error: '',
      }
    }
  } catch (error) {
    // Unknown model or an unreachable catalogue: do not block the call on it.
    entry = { at: Date.now(), efforts: [], error: String(error?.message ?? error) }
  }
  if (EFFORT_CACHE.size > 64) EFFORT_CACHE.clear()
  if (ttl > 0) EFFORT_CACHE.set(key, entry)
  return entry
}

/**
 * The message a refused effort comes back with, phrased as what to do instead.
 * @param {string} provider
 * @param {string} model
 * @param {string} effort
 * @param {string[]} known declared effort ids, possibly empty
 * @returns {string}
 */
function unknownEffortMessage(provider, model, effort, known) {
  const list = known.length > 0
    ? `this model declares ${known.join(', ')}`
    : 'this model declares no reasoning efforts'
  return `reasoning effort "${effort}" is not valid for ${provider}/${model}: ${list}. Leave it empty to follow the model default.`
}

function isLoopbackBaseUrl(value) {
  try {
    const url = new URL(String(value ?? ''))
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return false
    const hostname = url.hostname.replace(/^\[|\]$/g, '').toLowerCase()
    return hostname === 'localhost' || hostname === '::1' || hostname.startsWith('127.')
  } catch {
    return false
  }
}

function writeBridgeEndpoint(origin) {
  const baseUrl = String(origin ?? '').trim().replace(/\/+$/, '')
  if (baseUrl === '') return null
  if (!isLoopbackBaseUrl(baseUrl)) {
    appendHostLog(`refused to publish a non-loopback relay endpoint: ${baseUrl}`)
    return null
  }
  const payload = {
    baseUrl,
    tokenPath: BRIDGE_TOKEN_PATH,
    bridgePath: `${BRIDGE_PREFIX}/llm`,
    updatedAt: new Date().toISOString(),
  }
  try {
    writeFileAtomic(BRIDGE_ENDPOINT_PATH, `${JSON.stringify(payload, null, 2)}\n`, 0o600)
  } catch (error) {
    appendHostLog(`bridge endpoint could not be written: ${String(error?.message ?? error)}`)
    return null
  }
  return payload
}

/** Read the endpoint descriptor, or null when the plugin has not published one. */
function readBridgeEndpoint() {
  try {
    const value = JSON.parse(fs.readFileSync(BRIDGE_ENDPOINT_PATH, 'utf8'))
    if (!isPlainObject(value)) return null
    const baseUrl = String(value.baseUrl ?? '').trim()
    const tokenPath = String(value.tokenPath ?? '').trim()
    if (baseUrl === '' || tokenPath === '') return null
    if (!isLoopbackBaseUrl(baseUrl)) return null
    return { ...value, baseUrl, tokenPath }
  } catch {
    return null
  }
}

/**
 * Environment for a spawned engine: the engine reads the endpoint file at call
 * time instead of baking a URL into the configuration.
 * @returns {object} extra environment variables.
 */
const ENGINE_ENV_BASE = [
  'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'TEMP', 'TMP',
  'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'SSH_AUTH_SOCK', 'DISPLAY',
  'XDG_CACHE_HOME', 'PYTHONPATH', 'NODE_PATH',
]

/**
 * Environment for a spawned engine. The host's own environment is not handed
 * over wholesale: it can carry credentials the host was started with, and the
 * engine has no use for them. Everything this plugin itself understands lives
 * in the SUBTITLE_* namespace, so that passes through by prefix.
 * @returns {object} the environment for the child process.
 */
function engineEnv() {
  const out = {}
  for (const key of ENGINE_ENV_BASE) {
    if (process.env[key] !== undefined) out[key] = process.env[key]
  }
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('SUBTITLE_')) out[key] = process.env[key]
  }
  out.HOME = os.homedir()
  out.SUBTITLE_STUDIO_BRIDGE = BRIDGE_ENDPOINT_PATH
  return out
}

/** Whether the host can serve a relayed call at all (backs bridge.ready). */
function llmServiceAvailable(ctx) {
  try {
    const llm = ctx.get('llm')
    return llm !== undefined && llm !== null && typeof llm.stream === 'function'
  } catch {
    return false
  }
}

/**
 * Spending limits for the two routes that can reach a model.
 *
 * The shared secret proves the caller is the engine, not that it is allowed to
 * buy anything: `~/.dsh` is owned by the same user, and file modes only keep
 * *other* users out. So the route is also clamped (a call cannot ask for an
 * open-ended completion) and metered (a loop cannot spend unnoticed).
 */
const SPEND_WINDOW_MS = 60_000
// Read per request, not once at load: this is a spend guard the user may want
// to retune without restarting the harness, and a guard a test can exercise is
// a guard that stays correct.
function envPositiveInt(name, fallback) {
  const value = Number(process.env[name])
  return Number.isFinite(value) && value > 0 ? Math.trunc(value) : fallback
}
const spendLimit = () => envPositiveInt('SUBTITLE_STUDIO_SPEND_LIMIT', 120)
const relayConcurrency = () => envPositiveInt('SUBTITLE_STUDIO_RELAY_CONCURRENCY', 2)
const relayMaxTokens = () => envPositiveInt('SUBTITLE_STUDIO_RELAY_MAX_TOKENS', 16_384)
const spendStamps = []
let relayInFlight = 0

/** Consume one call from the per-minute budget; false when the window is full. */
function spendAllowed() {
  const now = Date.now()
  while (spendStamps.length > 0 && now - spendStamps[0] > SPEND_WINDOW_MS) spendStamps.shift()
  if (spendStamps.length >= spendLimit()) return false
  spendStamps.push(now)
  return true
}

/** @returns {{ok:boolean, status:number, code:string}|null} null when allowed. */
function spendGate() {
  if (!spendAllowed()) {
    // local-budget，不是 rate-limited：引擎对后者的反应是中止整轮不拆批，
    // 「每分钟调用预算用完」显然不该触发那个动作（审计 r16d-quality D-P1）。
    return { ok: false, status: 429, code: 'local-budget' }
  }
  return null
}

/**
 * Clamp a completion budget so one request cannot ask for an unbounded answer.
 * @param {unknown} value - submitted maxTokens.
 * @param {number} fallback - used when the value is absent or meaningless.
 * @param {number} ceiling - hard upper bound for this route.
 * @returns {number}
 */
function clampTokens(value, fallback, ceiling) {
  const wanted = Number.isFinite(Number(value)) && value !== '' && value !== null ? Number(value) : fallback
  return Math.min(Math.max(Math.trunc(wanted), 1), ceiling)
}

/** Clamp a sampling temperature into the range every provider accepts. */
function clampTemperature(value, fallback) {
  const wanted = Number.isFinite(Number(value)) && value !== '' && value !== null ? Number(value) : fallback
  return Math.min(Math.max(wanted, 0), 2)
}

/**
 * Call one model through the running DSH llm service and collect its text.
 * The provider route and its credentials belong to DSH; this plugin never sees
 * an API key.
 * @param {object} ctx - host context.
 * @param {{provider:string, model:string, prompt:string, system?:string, temperature?:number, maxTokens?:number, reasoningEffort?:string}} input - the call.
 * @returns {Promise<{ok:boolean, text:string, finish:string|null, usage:object, code:string|null, error:string|null}>}
 */
async function callLlm(ctx, input) {
  let llm
  try {
    llm = ctx.get('llm')
  } catch {
    return { ok: false, text: '', finish: null, error: 'llm service unavailable' }
  }
  if (llm === undefined || llm === null || typeof llm.stream !== 'function') {
    return { ok: false, text: '', finish: null, error: 'llm service unavailable' }
  }
  const prompt = String(input.prompt ?? '')
  const system = typeof input.system === 'string' ? input.system.trim() : ''
  // The llm seam turns a top-level `system` option into a developer-role
  // message, and several providers reject that role outright
  // outright. Folding the instructions into the user turn keeps every
  // provider on the same footing.
  const body = system === '' ? prompt : `${system}\n\n${prompt}`
  const options = {
    provider: input.provider,
    model: input.model,
    // The llm seam speaks the session message shape: a user message carries
    // typed content blocks, not a bare string.
    messages: [{ role: 'user', content: [{ type: 'text', text: body }] }],
  }
  if (Number.isFinite(input.temperature)) options.temperature = input.temperature
  if (Number.isFinite(input.maxTokens) && input.maxTokens > 0) options.maxTokens = input.maxTokens
  // Thinking budget is a model capability, not a plugin invention: the host
  // learns the legal effort ids from llm.resolveModelInfo and either forwards
  // the user's pick or omits the key entirely. Omitting it is how DSH itself
  // spells "follow whatever this provider defaults to".
  const effort = typeof input.reasoningEffort === 'string' ? input.reasoningEffort.trim() : ''
  if (effort !== '') options.reasoningEffort = effort
  let text = ''
  let finish = null
  let failure = null
  let usage = {}
  let reasoningChars = 0
  // A provider that hangs would otherwise keep this stream — and the engine
  // waiting on it — open forever.
  const controller = new AbortController()
  const deadline = setTimeout(() => controller.abort(), LLM_CALL_TIMEOUT_MS)
  options.signal = controller.signal
  try {
    for await (const chunk of llm.stream(options)) {
      if (chunk?.type === 'text-delta') text += String(chunk.text ?? '')
      else if (chunk?.type === 'reasoning-delta') {
        // Counted, never returned: it is the clearest signal that a model is
        // spending the answer budget on thinking, which is what truncates a
        // translation batch. The panel shows the number, not the reasoning.
        reasoningChars += String(chunk.text ?? '').length
      } else if (chunk?.type === 'usage') {
        usage = {
          inputTokens: Number(chunk.usage?.inputTokens ?? 0),
          outputTokens: Number(chunk.usage?.outputTokens ?? 0),
          reasoningTokens: Number(chunk.usage?.reasoningTokens ?? 0),
        }
      } else if (chunk?.type === 'finish') {
        finish = String(chunk.reason?.kind ?? 'unknown')
        if (chunk.reason?.kind === 'error' || chunk.reason?.kind === 'aborted') {
          failure = String(chunk.reason?.failure?.message ?? chunk.reason.kind)
        }
      }
    }
  } catch (error) {
    const aborted = error?.name === 'AbortError' || controller.signal.aborted === true
    const message = aborted ? `no answer within ${LLM_CALL_TIMEOUT_MS / 1000}s` : String(error?.message ?? error)
    // Upstream throttling arrives wrapped in whatever the adapter threw. A
    // rate limit is retryable but needs minutes, not the two-second retry a
    // generic stream error gets, so name it before it is flattened away.
    // Chinese alternatives must stay phrases: the bare words for "quota" or
    // "allowance" also show up in ordinary 5xx text (a billing-config
    // notice, an echoed model name), and a false positive here makes the
    // engine stop the whole run instead of retrying it.
    const throttled = !aborted
      && new RegExp('\\b429\\b|rate[_ -]?limit|too many requests|insufficient quota'
        + '|usage ceiling|token plan'
        + '|\\u7528\\u91cf\\u4e0a\\u9650|\\u4f7f\\u7528\\u4e0a\\u9650'
        + '|\\u989d\\u5ea6\\u4e0d\\u8db3|\\u989d\\u5ea6\\u5df2\\u7528\\u5c3d|\\u989d\\u5ea6\\u8017\\u5c3d'
        + '|\\u8d85\\u51fa\\u989d\\u5ea6|\\u914d\\u989d\\u4e0d\\u8db3|\\u914d\\u989d\\u5df2\\u7528\\u5c3d'
        + '|\\u8d85\\u51fa\\u914d\\u989d|\\u8bf7\\u6c42\\u8fc7\\u4e8e\\u9891\\u7e41|\\u901f\\u7387\\u9650\\u5236',
        'i').test(message)
    // The provider saying "this model has no such effort" is a configuration
    // answer, not a broken stream: asking again with the same effort gets the
    // same refusal, so name it instead of letting the caller retry it.
    const refused = !aborted
      && /does not support reasoning effort|unknown reasoning effort|unsupported reasoning/i.test(message)
    return {
      ok: false, text, finish, usage,
      code: aborted ? 'timeout' : refused ? 'unknown-effort' : throttled ? 'upstream-rate-limit' : 'stream-error',
      error: message,
    }
  } finally {
    clearTimeout(deadline)
  }
  if (reasoningChars > 0 && usage.reasoningTokens === undefined) {
    usage.reasoningChars = reasoningChars
  }
  if (failure !== null) return { ok: false, text, finish, usage, code: 'stream-error', error: failure }
  if (finish === 'max-tokens') {
    // Truncation is deterministic: asking again with the same prompt and the
    // same budget burns the same number of tokens for the same cut-off answer.
    // The text up to the cut is still good, so hand it back with a flag and let
    // the caller keep what landed instead of retrying.
    return {
      ok: false, text, finish, usage, code: 'token-limit',
      error: 'the model stopped at its token limit',
    }
  }
  if (finish === null) {
    // The contract guarantees a terminal finish chunk; without one there is no
    // way to know the text is complete, so treat it as a failure.
    return { ok: false, text, finish, usage, code: 'no-finish', error: 'the model stream ended without a finish signal' }
  }
  return { ok: true, text, finish, usage, code: null, error: null }
}

/**
 * Constant-time comparison of two secrets.
 * @param {string} left - presented value.
 * @param {string} right - expected value.
 * @returns {boolean}
 */
function secretEquals(left, right) {
  const a = Buffer.from(String(left ?? ''), 'utf8')
  const b = Buffer.from(String(right ?? ''), 'utf8')
  if (a.length === 0 || a.length !== b.length) return false
  return timingSafeEqual(a, b)
}

/* ---------------------------------- logging -------------------------------- */

/** Append one line to the host's own log (`logs/host.log`), rotating at 1 MiB. */
function appendHostLog(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`
  try {
    fs.mkdirSync(LOGS_DIR, { recursive: true, mode: 0o700 })
    const file = path.join(LOGS_DIR, 'host.log')
    try {
      if (fs.statSync(file).size > HOST_LOG_MAX_BYTES) {
        fs.renameSync(file, path.join(LOGS_DIR, 'host.log.1'))
      }
    } catch {
      // No existing log; nothing to rotate.
    }
    fs.appendFileSync(file, line, { mode: 0o600 })
  } catch {
    // Logging must never break a request.
  }
}

/** True when a path names a regular file. */
function isFile(target) {
  try {
    return fs.statSync(target).isFile()
  } catch {
    return false
  }
}

/** True when the engine CLI exists and is executable. */
function engineReady() {
  try {
    const cli = enginePath()
    const stat = fs.statSync(cli)
    if (!stat.isFile()) return false
    fs.accessSync(cli, fs.constants.X_OK)
    return true
  } catch {
    return false
  }
}

/**
 * Read the tail of a text file without loading all of it.
 * @param {string} file - absolute path.
 * @param {number} lines - how many lines to return.
 * @returns {{lines:string[],total:number,truncated:boolean,size:number}}
 */
function tailFile(file, lines) {
  const stat = fs.statSync(file)
  const window = Math.min(stat.size, 2 * 1024 * 1024)
  const start = stat.size - window
  const buffer = Buffer.alloc(window)
  const handle = fs.openSync(file, 'r')
  try {
    fs.readSync(handle, buffer, 0, window, start)
  } finally {
    fs.closeSync(handle)
  }
  let parts = buffer.toString('utf8').split(/\r?\n/)
  if (start > 0 && parts.length > 0) parts = parts.slice(1)
  if (parts.length > 0 && parts[parts.length - 1] === '') parts = parts.slice(0, -1)
  const truncated = parts.length > lines
  return {
    lines: parts.slice(-lines),
    total: parts.length,
    truncated,
    size: stat.size,
  }
}

/** Log files the panel may read, resolved and confined to the logs directory. */
function resolveLogFile(name) {
  const raw = String(name ?? 'run').trim()
  const safe = raw.endsWith('.log') ? raw : `${raw}.log`
  if (!/^[A-Za-z0-9._-]{1,64}\.log$/.test(safe)) return null
  let logsReal
  try {
    logsReal = fs.realpathSync(LOGS_DIR)
  } catch {
    return null
  }
  const candidate = path.join(logsReal, safe)
  if (!isFile(candidate)) return null
  try {
    const real = fs.realpathSync(candidate)
    if (real !== candidate || !real.startsWith(`${logsReal}${path.sep}`)) return null
    return real
  } catch {
    return null
  }
}

/** List the log files that currently exist, newest first. */
function listLogFiles() {
  try {
    const names = fs
      .readdirSync(LOGS_DIR)
      .filter((name) => /^[A-Za-z0-9._-]{1,64}\.log$/.test(name))
      .map((name) => {
        const stat = fs.statSync(path.join(LOGS_DIR, name))
        return { name, size: stat.size, mtime: stat.mtime.toISOString() }
      })
    names.sort((left, right) => (left.mtime < right.mtime ? 1 : -1))
    return names
  } catch {
    return []
  }
}

/* ------------------------------- job execution ----------------------------- */

/** The one long-running job this host allows at a time. */
const jobState = {
  current: null,
  last: null,
  history: [],
}

/** Assert and shape a finished-job summary. */
function summarize(job) {
  if (job === null) return null
  return {
    runId: job.runId,
    kind: job.kind,
    startedAt: job.startedAt,
    finishedAt: job.finishedAt ?? null,
    elapsedMs: (job.finishedAt === null ? Date.now() : job.finishedAt) - job.startedAtMs,
    exitCode: job.exitCode ?? null,
    signal: job.signal ?? null,
    canceled: job.canceled === true,
    timedOut: job.timedOut === true,
    error: job.error ?? null,
    result: job.result ?? null,
    args: job.args,
  }
}

/** Persist the last finished job so the overview survives a page reload. */
function persistLastRun(job) {
  const summary = summarize(job)
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 })
    writeFileAtomic(LAST_RUN_PATH, `${JSON.stringify(summary, null, 2)}\n`, 0o600)
  } catch (error) {
    appendHostLog(`could not persist last run: ${String(error?.message ?? error)}`)
  }
}

/** Read the persisted last-run summary, or null. */
function readLastRun() {
  if (jobState.last !== null) return summarize(jobState.last)
  try {
    return JSON.parse(fs.readFileSync(LAST_RUN_PATH, 'utf8'))
  } catch {
    return null
  }
}

/**
 * Parse the engine's `{ok, data|error}` envelope out of stdout, tolerating
 * progress lines the engine may have printed before the JSON.
 * @param {string} text - raw stdout.
 * @returns {{value:object|null,error:string|null}}
 */
function parseEnvelope(text) {
  const trimmed = String(text ?? '').trim()
  if (trimmed === '') return { value: null, error: 'engine produced no output' }
  const attempts = [trimmed]
  const first = trimmed.indexOf('{')
  const last = trimmed.lastIndexOf('}')
  if (first >= 0 && last > first) attempts.push(trimmed.slice(first, last + 1))
  const lines = trimmed.split(/\r?\n/)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index].trim()
    if (line.startsWith('{')) attempts.push(line)
  }
  for (const attempt of attempts) {
    try {
      const value = JSON.parse(attempt)
      if (isPlainObject(value)) return { value, error: null }
    } catch {
      // Try the next candidate.
    }
  }
  return { value: null, error: 'engine output is not JSON' }
}

/**
 * The engine's schema knows only `launchd` / `none` for `schedule.mode` —
 * scheduling belongs to the plugin, not the engine. When the panel stores
 * `dsh` (paste a prompt) or `off`, the engine gets a normalized copy so plan /
 * run / doctor / status keep passing the engine's own config validation.
 * @returns {string} config path to hand the engine.
 */
/**
 * Path the engine should be handed for this configuration.
 *
 * `schedule.mode` is host-side vocabulary (dsh / off); the engine only knows
 * launchd / none, so the view is normalised before it is handed over. Pass the
 * in-memory config when one is already at hand — a prompt generated before the
 * save would otherwise quote the previous mode.
 * @param {object} [source] - optional parsed config object to normalise.
 * @returns {string} absolute path to a config file the engine accepts.
 */
function engineConfigPath(source) {
  let config = isPlainObject(source) ? source : null
  if (config === null) {
    try {
      config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'))
    } catch {
      return CONFIG_PATH
    }
  }
  const mode = config && config.schedule ? config.schedule.mode : undefined
  const normalizedMode = mode === 'launchd' ? 'launchd' : mode === 'dsh' || mode === 'off' ? 'none' : undefined
  if (normalizedMode === undefined || normalizedMode === mode) return CONFIG_PATH
  const normalized = { ...config, schedule: { ...(config.schedule || {}), mode: normalizedMode } }
  const text = `${JSON.stringify(normalized, null, 2)}\n`
  const target = path.join(STATE_DIR, 'engine-config.json')
  try {
    const current = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : ''
    if (current !== text) writeFileAtomic(target, text)
  } catch (error) {
    appendHostLog('engine-config', `could not prepare the engine view of config.json: ${String(error?.message ?? error)}`)
    return CONFIG_PATH
  }
  return target
}

/**
 * Run a short engine command to completion.
 * @param {string} command - doctor | plan | status.
 * @param {string[]} extraArgs - additional CLI arguments.
 * @param {number} timeoutMs - hard wall-clock bound.
 * @returns {Promise<object>} a normal result object, never a rejection.
 */
function runEngineOnce(command, extraArgs, timeoutMs = SHORT_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const startedAt = Date.now()
    const args = [command, '--config', engineConfigPath(), ...extraArgs, '--json']
    if (!engineReady()) {
      resolve({
        ok: false,
        code: 'engine-missing',
        message: `engine CLI not found at ${enginePath()}`,
        args,
        exitCode: null,
        durationMs: 0,
      })
      return
    }
    let child
    try {
      child = spawn(enginePath(), args, {
        cwd: DATA_DIR,
        env: engineEnv(),
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      resolve({ ok: false, code: 'spawn-failed', message: String(error?.message ?? error), args, exitCode: null, durationMs: 0 })
      return
    }
    const stdout = []
    let stdoutBytes = 0
    let stderr = ''
    let settled = false
    const finish = (payload) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve({ ...payload, args, durationMs: Date.now() - startedAt })
    }
    const timer = setTimeout(() => {
      child.kill('SIGTERM')
      setTimeout(() => child.kill('SIGKILL'), 2000).unref()
      finish({ ok: false, code: 'timeout', message: `engine ${command} timed out after ${timeoutMs} ms`, exitCode: null })
    }, timeoutMs)
    timer.unref()
    child.stdout.on('data', (chunk) => {
      if (stdoutBytes < 4 * 1024 * 1024) {
        stdout.push(chunk)
        stdoutBytes += chunk.length
      }
    })
    child.stderr.on('data', (chunk) => {
      if (stderr.length < 256 * 1024) stderr += chunk.toString('utf8')
    })
    child.on('error', (error) => {
      finish({ ok: false, code: 'spawn-failed', message: String(error?.message ?? error), exitCode: null })
    })
    child.on('close', (code, signal) => {
      const text = Buffer.concat(stdout).toString('utf8')
      const parsed = parseEnvelope(text)
      finish({
        ok: code === 0 && parsed.value?.ok !== false,
        exitCode: code,
        signal: signal ?? null,
        stdout: text.slice(0, 256 * 1024),
        stderr: stderr.slice(0, 64 * 1024),
        result: parsed.value,
        parseError: parsed.error,
      })
    })
  })
}

/** Split a stream chunk into whole lines, buffering the trailing partial line. */
function makeLineSplitter(onLine) {
  let buffer = ''
  return {
    push(chunk) {
      buffer += chunk.toString('utf8')
      const parts = buffer.split(/\r?\n/)
      buffer = parts.pop() ?? ''
      for (const line of parts) onLine(line)
    },
    flush() {
      if (buffer !== '') onLine(buffer)
      buffer = ''
    },
  }
}

/**
 * Start the one long-running engine job.
 * @param {'run'|'selftest'} kind - which command to execute.
 * @param {object} options - {max, only, dryRun}.
 * @returns {{ok:boolean, code?:string, message?:string, data?:object}}
 */
function startJob(kind, options) {
  if (jobState.current !== null) {
    return {
      ok: false,
      code: 'already-running',
      message: `a ${jobState.current.kind} job is already running (${jobState.current.runId})`,
      data: { runId: jobState.current.runId, kind: jobState.current.kind, startedAt: jobState.current.startedAt },
    }
  }
  if (!engineReady()) {
    return { ok: false, code: 'engine-missing', message: `engine CLI not found at ${enginePath()}` }
  }
  const args = [kind, '--config', engineConfigPath()]
  if (kind === 'run') {
    if (options?.max !== undefined && options.max !== null) args.push('--max', String(options.max))
    if (options?.only !== undefined && options.only !== null && String(options.only) !== '') args.push('--only', String(options.only))
    if (options?.dryRun === true) args.push('--dry-run')
    args.push('--json')
  }
  const runId = `${kind}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  const startedAtMs = Date.now()
  const job = {
    runId,
    kind,
    args,
    startedAt: new Date(startedAtMs).toISOString(),
    startedAtMs,
    finishedAt: null,
    exitCode: null,
    signal: null,
    canceled: false,
    timedOut: false,
    error: null,
    result: null,
    lines: [],
    stdout: [],
    stdoutBytes: 0,
    stderr: '',
    child: null,
  }
  let child
  try {
    child = spawn(enginePath(), args, {
      cwd: DATA_DIR,
      env: engineEnv(),
      stdio: ['ignore', 'pipe', 'pipe'],
    })
  } catch (error) {
    return { ok: false, code: 'spawn-failed', message: String(error?.message ?? error) }
  }
  job.child = child
  const pushLine = (line) => {
    job.lines.push(line)
    if (job.lines.length > RING_LINES) job.lines.splice(0, job.lines.length - RING_LINES)
  }
  const outLines = makeLineSplitter(pushLine)
  const errLines = makeLineSplitter((line) => pushLine(`[stderr] ${line}`))
  child.stdout.on('data', (chunk) => {
    if (job.stdoutBytes < 8 * 1024 * 1024) {
      job.stdout.push(chunk)
      job.stdoutBytes += chunk.length
    }
    outLines.push(chunk)
  })
  child.stderr.on('data', (chunk) => {
    if (job.stderr.length < 512 * 1024) job.stderr += chunk.toString('utf8')
    errLines.push(chunk)
  })
  const finalize = (code, signal, error) => {
    if (job.finishedAt !== null) return
    outLines.flush()
    errLines.flush()
    job.finishedAt = new Date().toISOString()
    job.exitCode = code ?? null
    job.signal = signal ?? null
    if (error !== undefined) job.error = error
    const text = Buffer.concat(job.stdout).toString('utf8')
    const parsed = parseEnvelope(text)
    if (parsed.value !== null) job.result = parsed.value
    else if (parsed.error !== null) job.error = job.error ?? parsed.error
    job.stdout = []
    job.child = null
    if (jobState.current === job) jobState.current = null
    jobState.last = job
    jobState.history = [summarize(job), ...jobState.history.filter((entry) => entry.runId !== job.runId)].slice(0, 10)
    persistLastRun(job)
    appendHostLog(`${job.kind} ${job.runId} exited with ${job.exitCode} (${job.signal ?? 'no signal'})`)
  }
  child.on('error', (error) => finalize(null, null, String(error?.message ?? error)))
  child.on('close', (code, signal) => finalize(code, signal))
  jobState.current = job
  appendHostLog(`${kind} ${runId} started: ${enginePath()} ${args.join(' ')}`)
  return { ok: true, data: { runId, kind, startedAt: job.startedAt, args } }
}

/** Stop the running job, escalating to SIGKILL after a grace period. */
function cancelJob(runId) {
  const job = jobState.current
  if (job === null) return { ok: false, code: 'not-running', message: 'no job is running' }
  if (runId !== undefined && runId !== null && String(runId) !== job.runId) {
    return { ok: false, code: 'not-running', message: `job ${runId} is not the running job` }
  }
  job.canceled = true
  try {
    job.child?.kill('SIGTERM')
  } catch (error) {
    appendHostLog(`cancel failed: ${String(error?.message ?? error)}`)
  }
  const timer = setTimeout(() => {
    try {
      job.child?.kill('SIGKILL')
    } catch {
      // Already gone.
    }
  }, 5000)
  timer.unref()
  appendHostLog(`cancel requested for ${job.runId}`)
  return { ok: true, data: { runId: job.runId, canceled: true } }
}

/* ------------------------------ directory browse --------------------------- */

/** Paths the browser may never enumerate, by prefix. */
const DENIED_PREFIXES = [
  '/System',
  '/private/var/db',
  '/private/var/vm',
  '/private/var/root',
  '/var/db',
  '/var/root',
  '/private/etc',
  '/etc',
  '/dev',
  '/proc',
  '/bin',
  '/sbin',
  '/usr/bin',
  '/usr/sbin',
]

/** Credential stores that are never shown in a path picker. */
const DENIED_HOME_DIRS = [
  '.ssh',
  '.gnupg',
  '.aws',
  '.config',
  '.kube',
  '.docker',
  '.git-credentials',
  '.Trash',
  'Library/Keychains',
  'Library/Cookies',
]

/** True when a resolved path is denylisted for browsing. */
function isDeniedPath(target) {
  for (const prefix of DENIED_PREFIXES) {
    if (target === prefix || target.startsWith(`${prefix}${path.sep}`)) return true
  }
  const home = os.homedir()
  for (const relative of DENIED_HOME_DIRS) {
    const denied = path.join(home, relative)
    if (target === denied || target.startsWith(`${denied}${path.sep}`)) return true
  }
  return false
}

/** Race a promise against a wall-clock bound. */
function withTimeout(promise, ms, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms)
    timer.unref()
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

/**
 * List one directory level for the path pickers.
 * @param {string} rawPath - requested path (`~` allowed, empty means home).
 * @param {boolean} includeFiles - include non-directory entries.
 * @returns {Promise<{ok:boolean, code?:string, message?:string, data?:object}>}
 */
async function listDirectory(rawPath, includeFiles) {
  const requested = String(rawPath ?? '').trim() === '' ? os.homedir() : String(rawPath).trim()
  if (requested.length > 4096 || requested.includes('\0')) {
    return { ok: false, code: 'invalid-path', message: 'path is too long' }
  }
  const expanded = path.resolve(expandHome(requested))
  let real
  try {
    real = fs.realpathSync(expanded)
  } catch (error) {
    const code = error?.code === 'ENOENT' ? 'not-found' : 'unreadable'
    return { ok: false, code, message: String(error?.message ?? error) }
  }
  if (isDeniedPath(real)) return { ok: false, code: 'denied', message: 'this directory is not browsable' }
  let stat
  try {
    stat = fs.statSync(real)
  } catch (error) {
    return { ok: false, code: 'unreadable', message: String(error?.message ?? error) }
  }
  if (!stat.isDirectory()) return { ok: false, code: 'not-a-directory', message: 'not a directory' }
  let dirents
  try {
    dirents = await withTimeout(fs.promises.readdir(real, { withFileTypes: true }), LISTDIR_TIMEOUT_MS, 'directory listing timed out')
  } catch (error) {
    return { ok: false, code: 'unreadable', message: String(error?.message ?? error) }
  }
  const entries = []
  for (const dirent of dirents) {
    if (entries.length >= LISTDIR_ENTRY_LIMIT) break
    const child = path.join(real, dirent.name)
    let kind = 'other'
    if (dirent.isDirectory()) kind = 'dir'
    else if (dirent.isFile()) kind = 'file'
    else if (dirent.isSymbolicLink()) {
      try {
        kind = fs.statSync(child).isDirectory() ? 'dir' : 'file'
      } catch {
        kind = 'broken'
      }
      if (kind === 'dir' && isDeniedPath(child)) continue
    }
    if (kind === 'file' && !includeFiles) continue
    if (kind === 'other' || kind === 'broken') continue
    let size = null
    let mtime = null
    try {
      const childStat = fs.statSync(child)
      size = childStat.size
      mtime = childStat.mtime.toISOString()
    } catch {
      // Keep the entry with unknown metadata.
    }
    entries.push({ name: dirent.name, path: child, kind, size, mtime })
  }
  entries.sort((left, right) => {
    if (left.kind !== right.kind) return left.kind === 'dir' ? -1 : 1
    return left.name.localeCompare(right.name, 'zh-Hans-CN', { numeric: true })
  })
  const parent = real === path.parse(real).root ? null : path.dirname(real)
  return {
    ok: true,
    data: {
      path: real,
      parent,
      home: os.homedir(),
      entries,
      truncated: dirents.length > LISTDIR_ENTRY_LIMIT,
    },
  }
}

/* ---------------------------------- launchd -------------------------------- */

/** The launchd domain target for this user. */
function launchdDomain() {
  const uid = typeof process.getuid === 'function' ? process.getuid() : 501
  return `gui/${uid}`
}

/** Where a LaunchAgent with `label` lives for this user. */
function plistPathFor(label) {
  return path.join(os.homedir(), 'Library', 'LaunchAgents', `${label}.plist`)
}

/** Escape a string for XML text content. */
function xmlEscape(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&apos;')
}

/** Split `HH:MM` into launchd calendar fields. */
function parseClock(time) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(time ?? '').trim())
  if (match === null) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  if (hour > 23 || minute > 59) return null
  return { hour, minute }
}

/**
 * The LaunchAgent that runs the engine once a day.
 * @param {{label:string,time:string,enginePath:string,configPath:string,dataDir:string}} spec - values to embed.
 * @returns {string} plist XML.
 */
/**
 * The PATH handed to the scheduled job. launchd starts with a bare environment,
 * so this has to carry every directory the real tools live in: the ffmpeg the user
 * configured, whatever the host itself was started with, and only then the usual
 * system and package-manager prefixes. Hard-coding a single layout is what makes a
 * scheduled run fail on a machine the author never had.
 */
function scheduledPathEnv() {
  const dirs = []
  const addDir = (value) => {
    const clean = String(value ?? '').trim().replace(/\/+$/, '')
    if (clean.startsWith('/') && clean !== '/' && !dirs.includes(clean)) dirs.push(clean)
  }
  const addBin = (value) => {
    const clean = String(value ?? '').trim()
    if (!clean.startsWith('/')) return
    const cut = clean.lastIndexOf('/')
    if (cut > 0) addDir(clean.slice(0, cut))
  }
  try {
    const parsed = currentConfig().parsed ?? {}
    // A configured ffmpeg is a file path; its directory is what PATH needs.
    addBin(parsed.burn?.ffmpeg)
    addBin(process.env.SUBTITLE_STUDIO_FFMPEG)
  } catch {
    // An unreadable config must not stop the schedule from being written.
  }
  for (const dir of String(process.env.PATH ?? '').split(':')) addDir(dir)
  for (const dir of ['/usr/bin', '/bin', '/usr/sbin', '/sbin', '/usr/local/bin',
    '/opt/homebrew/bin', '/opt/homebrew/sbin', '/opt/local/bin']) addDir(dir)
  return dirs.join(':')
}

function buildPlist(spec) {
  const clock = parseClock(spec.time) ?? { hour: 2, minute: 10 }
  // 兜底触发点：launchd 的 StartCalendarInterval 允许写成数组，多铺几个时间点，
  // 错过主触发的那天会被后面的点补跑（guard 幂等，同一天不会重复处理）。
  const clocks = (Array.isArray(spec.times) && spec.times.length > 0 ? spec.times : [spec.time]).map(
    (entry) => parseClock(typeof entry === 'string' ? entry : entry?.time) ?? clock,
  )
  const one = (value, pad) =>
    `${' '.repeat(pad)}<dict>\n${' '.repeat(pad + 2)}<key>Hour</key>\n${' '.repeat(pad + 2)}<integer>${value.hour}</integer>\n${' '.repeat(pad + 2)}<key>Minute</key>\n${' '.repeat(pad + 2)}<integer>${value.minute}</integer>\n${' '.repeat(pad)}</dict>`
  const intervalXml =
    clocks.length === 1
      ? `  <key>StartCalendarInterval</key>\n  <dict>\n    <key>Hour</key>\n    <integer>${clocks[0].hour}</integer>\n    <key>Minute</key>\n    <integer>${clocks[0].minute}</integer>\n  </dict>`
      : `  <key>StartCalendarInterval</key>\n  <array>\n${clocks.map((value) => one(value, 4)).join('\n')}\n  </array>`
  const outLog = path.join(LOGS_DIR, 'launchd.out.log')
  const errLog = path.join(LOGS_DIR, 'launchd.err.log')
  const pathEnv = scheduledPathEnv()
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${xmlEscape(spec.label)}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${xmlEscape(spec.enginePath)}</string>
    <string>guard</string>
    <string>--config</string>
    <string>${xmlEscape(spec.configPath)}</string>
  </array>
  ${intervalXml.replace(/^  /, '')}
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${xmlEscape(pathEnv)}</string>
    <key>HOME</key>
    <string>${xmlEscape(os.homedir())}</string>
    <key>SUBTITLE_STUDIO_BRIDGE</key>
    <string>${xmlEscape(BRIDGE_ENDPOINT_PATH)}</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>${xmlEscape(spec.dataDir)}</string>
  <key>StandardOutPath</key>
  <string>${xmlEscape(outLog)}</string>
  <key>StandardErrorPath</key>
  <string>${xmlEscape(errLog)}</string>
  <key>RunAtLoad</key>
  <false/>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
`
}

/** Read the recorded launchd installation state, or an empty record. */
function readLaunchdRecord() {
  try {
    const value = JSON.parse(fs.readFileSync(LAUNCHD_STATE_PATH, 'utf8'))
    return isPlainObject(value) ? value : {}
  } catch {
    return {}
  }
}

/** Persist the launchd installation state. */
function writeLaunchdRecord(record) {
  fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 })
  writeFileAtomic(LAUNCHD_STATE_PATH, `${JSON.stringify(record, null, 2)}\n`, 0o600)
}

/** True when launchd currently knows this label. */
async function launchdLoaded(label) {
  try {
    await execFileAsync('launchctl', ['print', `${launchdDomain()}/${label}`], { timeout: 8000 })
    return true
  } catch {
    return false
  }
}

/** Remove a loaded agent; a not-loaded label is success. */
async function launchdBootout(label) {
  try {
    await execFileAsync('launchctl', ['bootout', `${launchdDomain()}/${label}`], { timeout: 15000 })
    return { ok: true }
  } catch (error) {
    const text = `${error?.stderr ?? ''}${error?.stdout ?? ''}${error?.message ?? ''}`
    if (/could not find|no such process|not found|3: /i.test(text)) return { ok: true }
    return { ok: false, message: text.trim() || 'launchctl bootout failed' }
  }
}

/** Load a LaunchAgent from its plist. */
async function launchdBootstrap(plistPath) {
  try {
    await execFileAsync('launchctl', ['bootstrap', launchdDomain(), plistPath], { timeout: 15000 })
    return { ok: true }
  } catch (error) {
    const text = `${error?.stderr ?? ''}${error?.stdout ?? ''}${error?.message ?? ''}`
    return { ok: false, message: text.trim() || 'launchctl bootstrap failed' }
  }
}

/** Validate plist syntax when `plutil` is available; absence is not an error. */
async function lintPlist(plistPath) {
  try {
    await execFileAsync('plutil', ['-lint', plistPath], { timeout: 8000 })
    return { ok: true }
  } catch (error) {
    if (error?.code === 'ENOENT') return { ok: true, skipped: true }
    const text = `${error?.stdout ?? ''}${error?.stderr ?? ''}`.trim()
    return { ok: false, message: text || 'plutil rejected the plist' }
  }
}

/**
 * Effective trigger points, primary first: `schedule.time` plus `schedule.guardTimes`.
 * Times are normalized to HH:MM, de-duplicated and only kept when they parse.
 * @param {object} config - hydrated configuration.
 * @returns {Array<{time:string, role:'primary'|'extra', minutes:number}>}
 */
function effectiveGuardTimes(config) {
  const out = []
  const seen = new Set()
  const add = (time, role) => {
    const clock = parseClock(time)
    if (clock === null) return
    const normalized = `${String(clock.hour).padStart(2, '0')}:${String(clock.minute).padStart(2, '0')}`
    if (seen.has(normalized)) return
    seen.add(normalized)
    out.push({ time: normalized, role, minutes: clock.hour * 60 + clock.minute })
  }
  add(getAt(config, 'schedule.time'), 'primary')
  for (const time of toStringList(getAt(config, 'schedule.guardTimes'))) add(time, 'extra')
  return out.sort((left, right) => (left.role === right.role ? left.minutes - right.minutes : left.role === 'primary' ? -1 : 1))
}

/**
 * The prompt text the panel shows in DSH mode (nothing is created), one entry
 * per trigger point. Every entry runs the same idempotent `guard`, so an extra
 * trigger point can never double-process a day — it only retries a missed one.
 * @param {object} config - hydrated configuration.
 * @returns {Array<{time:string, role:string, text:string}>}
 */
function buildDshPrompts(config) {
  return effectiveGuardTimes(config).map(({ time, role }) => {
    const lead =
      role === 'primary'
        ? `请运行字幕工坊的每日守卫（主触发 ${time}；幂等：今天已处理就跳过）：`
        : `请运行字幕工坊的兜底守卫（触发点 ${time}；只有今天还没跑成时才执行翻译，已处理一律跳过）：`
    return {
      time,
      role,
      text: [
        lead,
        `- 配置：${engineConfigPath(config)}`,
        `- 引擎：${enginePath()} guard --config ${engineConfigPath(config)} --json`,
        '- data.action = "skip" 时，只回报一行 data.reasonText，不要重跑；',
        '- data.action = "run" 时，读取 logs/run.log 末尾 50 行，回报处理的文件、退出码与产出路径；',
        '  失败时附上 doctor --json 的失败项与修复建议。',
        `- 日志目录：${LOGS_DIR}`,
      ].join('\n'),
    }
  })
}

/** Backward-compatible single prompt (the primary trigger point). */
function buildDshPrompt(config) {
  const prompts = buildDshPrompts(config)
  return prompts.length === 0 ? '' : prompts[0].text
}

/**
 * Current scheduler state: what the file says, what launchd says, what this
 * plugin last installed.
 * @returns {Promise<object>}
 */
async function scheduleStatus(config) {
  const label = config.schedule.label
  const plistPath = plistPathFor(label)
  const record = readLaunchdRecord()
  const loaded = await launchdLoaded(label)
  const installed = record.label === label ? record : null
  const prompts = buildDshPrompts(config)
  const nextRuns = prompts
    .map((prompt) => ({ time: prompt.time, at: nextRunIso(prompt.time) }))
    .filter((entry) => entry.at !== null)
  const nextRun = nextRuns.reduce((earliest, entry) => (earliest === null || entry.at < earliest ? entry.at : earliest), null)
  return {
    mode: config.schedule.mode,
    enabled: config.schedule.enabled,
    time: config.schedule.time,
    guardTimes: prompts.map((prompt) => prompt.time),
    label,
    plistPath,
    plistExists: isFile(plistPath),
    launchdLoaded: loaded,
    platform: process.platform,
    supported: process.platform === 'darwin',
    installedAt: installed?.installedAt ?? null,
    enginePath: enginePath(),
    configPath: CONFIG_PATH,
    nextRun,
    nextRuns,
    prompts,
    promptText: prompts.length === 0 ? '' : prompts[0].text,
    logsDir: LOGS_DIR,
  }
}

/** Next local occurrence of `HH:MM`, as an ISO string, or null. */
function nextRunIso(time) {
  const clock = parseClock(time)
  if (clock === null) return null
  const now = new Date()
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate(), clock.hour, clock.minute, 0, 0)
  if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + 1)
  return next.toISOString()
}

/**
 * Apply the configured scheduler mode. Idempotent: an unchanged, already-loaded
 * agent is left alone; every failure restores the previous plist and load state.
 * @param {object} config - hydrated configuration.
 * @returns {Promise<{ok:boolean, code?:string, message?:string, steps?:string[], data?:object}>}
 */
async function applySchedule(config) {
  const steps = []
  const wanted = config.schedule.enabled === true && config.schedule.mode === 'launchd'
  const label = config.schedule.label
  const plistPath = plistPathFor(label)
  const record = readLaunchdRecord()
  const previousLabel = typeof record.label === 'string' ? record.label : null

  if (process.platform !== 'darwin') {
    return { ok: false, code: 'unsupported-platform', message: `launchd is macOS-only (this is ${process.platform})` }
  }

  // A label change orphans the old agent: remove it first.
  if (previousLabel !== null && previousLabel !== label) {
    steps.push(`bootout:${previousLabel}`)
    await launchdBootout(previousLabel)
    try {
      fs.rmSync(plistPathFor(previousLabel), { force: true })
    } catch {
      // Nothing to remove.
    }
    writeLaunchdRecord({})
  }

  if (!wanted) {
    steps.push(`bootout:${label}`)
    const bootout = await launchdBootout(label)
    let removed = false
    if (isFile(plistPath)) {
      try {
        fs.rmSync(plistPath, { force: true })
        removed = true
        steps.push('plist:removed')
      } catch (error) {
        return { ok: false, code: 'remove-failed', message: String(error?.message ?? error), steps }
      }
    }
    writeLaunchdRecord({ label: null, removedAt: new Date().toISOString() })
    if (!bootout.ok) return { ok: false, code: 'bootout-failed', message: bootout.message, steps }
    appendHostLog(`scheduler removed (${label})`)
    return {
      ok: true,
      steps,
      data: { removed, mode: config.schedule.mode, enabled: config.schedule.enabled },
    }
  }

  const engineOk = engineReady()
  if (!engineOk) {
    return { ok: false, code: 'engine-missing', message: `engine CLI not found at ${enginePath()}`, steps }
  }
  const clock = parseClock(config.schedule.time)
  if (clock === null) return { ok: false, code: 'invalid-time', message: 'schedule.time must be HH:MM', steps }

  const text = buildPlist({
    label,
    time: config.schedule.time,
    times: effectiveGuardTimes(config),
    enginePath: enginePath(),
    configPath: CONFIG_PATH,
    dataDir: DATA_DIR,
  })
  let previousText = null
  if (isFile(plistPath)) {
    try {
      previousText = fs.readFileSync(plistPath, 'utf8')
    } catch {
      previousText = null
    }
  }
  const loaded = await launchdLoaded(label)
  if (previousText === text && loaded) {
    appendHostLog(`scheduler unchanged (${label})`)
    return { ok: true, steps: ['unchanged'], data: { changed: false } }
  }

  try {
    fs.mkdirSync(path.dirname(plistPath), { recursive: true })
    writeFileAtomic(plistPath, text, 0o644)
    steps.push('plist:written')
  } catch (error) {
    return { ok: false, code: 'write-failed', message: String(error?.message ?? error), steps }
  }
  const lint = await lintPlist(plistPath)
  if (!lint.ok) {
    restore(previousText)
    return { ok: false, code: 'invalid-plist', message: lint.message, steps }
  }
  if (loaded) {
    steps.push(`bootout:${label}`)
    const bootout = await launchdBootout(label)
    if (!bootout.ok) {
      restore(previousText)
      if (previousText !== null) await launchdBootstrap(plistPath)
      return { ok: false, code: 'bootout-failed', message: bootout.message, steps }
    }
  }
  steps.push('bootstrap')
  const bootstrap = await launchdBootstrap(plistPath)
  if (!bootstrap.ok) {
    restore(previousText)
    if (previousText !== null) await launchdBootstrap(plistPath)
    return { ok: false, code: 'bootstrap-failed', message: bootstrap.message, steps }
  }
  writeLaunchdRecord({
    label,
    plistPath,
    time: config.schedule.time,
    enginePath: enginePath(),
    configPath: CONFIG_PATH,
    installedAt: new Date().toISOString(),
  })
  appendHostLog(`scheduler installed (${label} at ${config.schedule.time})`)
  return { ok: true, steps, data: { changed: true, label, time: config.schedule.time, plistPath } }

  /** Put the previous plist back, or delete the one this call created. */
  function restore(previous) {
    try {
      if (previous === null) fs.rmSync(plistPath, { force: true })
      else fs.writeFileSync(plistPath, previous, { mode: 0o644 })
    } catch {
      // Best effort: the error already being reported is the actionable one.
    }
  }
}

/* -------------------------------- self check ------------------------------- */

/** Cached result of the last dependency self-check. */
const SELFCHECK_PATH = path.join(STATE_DIR, 'selfcheck.json')
const SELFCHECK_TTL_MS = 6 * 60 * 60 * 1000

/**
 * Read the cached self-check, or null when there is none / it is not readable.
 * @returns {object|null}
 */
function readSelfCheck() {
  try {
    const value = JSON.parse(fs.readFileSync(SELFCHECK_PATH, 'utf8'))
    return isPlainObject(value) ? value : null
  } catch {
    return null
  }
}

/**
 * Run `doctor` and cache the verdict. Called on first load after install and
 * whenever the cache expires, so a fresh install reports missing dependencies
 * (ffmpeg, whisper, …) without the user having to open the 诊断 tab.
 * @param {{force?:boolean}} [options] - force ignores the TTL.
 * @returns {Promise<object>} `{ok, data:{at, enginePath, verdict, summary, checks}}`
 */
async function runSelfCheck({ force = false } = {}) {
  const cached = readSelfCheck()
  const fresh = cached !== null && Number.isFinite(cached.atMs) && Date.now() - cached.atMs < SELFCHECK_TTL_MS
  if (fresh && !force) return { ok: true, data: cached, cached: true }
  // doctor probes every configured directory; a multi-directory setup with
  // remote endpoints needs proportionally longer than a single local folder.
  const dirCount = Math.max(1, normalizeDirs(currentConfig().dirs).length)
  const timeoutMs = Math.min(SHORT_TIMEOUT_MS, 45_000 + (dirCount - 1) * 25_000)
  const result = await runEngineOnce('doctor', [], timeoutMs)
  const payload = result?.result?.data ?? result?.parsed?.data ?? null
  const checks = Array.isArray(payload?.checks) ? payload.checks : []
  const data = {
    at: new Date().toISOString(),
    atMs: Date.now(),
    enginePath: enginePath(),
    engineExists: engineReady(),
    verdict: payload?.verdict ?? null,
    summary: payload?.summary ?? null,
    checks,
    errors: checks.filter((check) => check?.level === 'error').length,
    warnings: checks.filter((check) => check?.level === 'warn').length,
    ok: result?.ok === true,
    exitCode: result?.exitCode ?? null,
  }
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 })
    writeFileAtomic(SELFCHECK_PATH, `${JSON.stringify(data, null, 2)}\n`, 0o600)
  } catch (error) {
    appendHostLog(`selfcheck cache write failed: ${String(error?.message ?? error)}`)
  }
  appendHostLog(`selfcheck: verdict=${data.verdict ?? 'unknown'} errors=${data.errors} warnings=${data.warnings}`)
  return { ok: data.ok && data.errors === 0, data, cached: false }
}

/* --------------------------------- describe -------------------------------- */

/**
 * The payload the panel renders. Everything the UI shows as truth comes from
 * here, so a page reload never depends on browser storage.
 * @param {object} ctx - host context.
 * @returns {Promise<object>}
 */
async function describe(ctx) {
  const current = readConfigFile()
  const config = hydrate(current.parsed)
  const models = await resolveModels(ctx)
  const endpoint = readBridgeEndpoint()
  return {
    version: 1,
    configPath: CONFIG_PATH,
    dataDir: DATA_DIR,
    logsDir: LOGS_DIR,
    stateDir: STATE_DIR,
    enginePath: enginePath(),
    enginePathSource: getAt(current.parsed ?? {}, 'paths.engine') ? 'config' : 'bundled',
    engineBundlePath: ENGINE_PATH,
    selfcheck: readSelfCheck(),
    engineExists: engineReady(),
    exists: current.exists,
    readError: current.error,
    parseError: current.parseError,
    fileMode: current.mode,
    fileSize: current.size,
    raw: current.text.length > 128 * 1024 ? `${current.text.slice(0, 128 * 1024)}\n…` : current.text,
    config,
    defaults: DEFAULT_CONFIG,
    fields: FIELDS,
    engines: ENGINE_KINDS,
    transcribeEngines: TRANSCRIBE_ENGINES,
    orders: SELECTION_ORDERS,
    presets: FFMPEG_PRESETS,
    audioCodecs: AUDIO_CODECS,
    notifyChannels: NOTIFY_CHANNELS,
    notifySecret: await probeNotifySecret(config.notify),
    notifyQqSendAvailable: (() => {
      // Only a path the user configured counts. The plugin ships no fallback
      // script: a notification helper is local infrastructure, and guessing a
      // path would only ever point at something that does not exist.
      const configured = String(config.notify?.qqSendPath ?? '').trim()
      if (configured === '') return false
      try {
        return fs.existsSync(expandHome(configured))
      } catch {
        return false
      }
    })(),
    scheduleModes: SCHEDULE_MODES,
    linkKinds: LINK_KINDS,
    // REQ-47(c)：面板据此给「缺失依赖」那一行画安装按钮。只暴露动作 id 与
    // 人类可读的命令文本，命令本体留在宿主，面板永远不传命令。
    fixDeps: fixDepInfo(config),
    models,
    bridge: {
      endpointPath: BRIDGE_ENDPOINT_PATH,
      // "ready" has to mean the host can actually serve a translation, not
      // merely that the endpoint file was written once.
      ready: endpoint !== null && llmServiceAvailable(ctx),
      baseUrl: endpoint?.baseUrl ?? null,
      engine: {
        kind: String(config.engine?.kind ?? ''),
        provider: String(config.engine?.provider ?? ''),
        model: String(config.engine?.model ?? ''),
      },
    },
    schedule: await scheduleStatus(config),
    run: {
      current: summarize(jobState.current),
      last: readLastRun(),
      history: jobState.history,
    },
    logs: listLogFiles(),
    platform: process.platform,
  }
}

/* ------------------------------- dependency fix ---------------------------- */

/**
 * Which missing dependencies the panel may offer to install, and the exact text
 * of the command. The mapping is built here, not in the panel: the browser only
 * ever sends an action id, and an unknown id is refused.
 *
 * Deliberately absent: whisper.model. A model weight is a multi-GB download and
 * a much bigger commitment than `brew install`; it stays a documented manual
 * step rather than one click.
 */
function fixDepInfo(config) {
  const transcribeEngine = String(config?.transcribe?.engine ?? 'mlx')
  const byCheck = { ...FIX_DEP_BY_CHECK }
  // mlx-whisper 只有 Apple Silicon 的 wheel；Intel 上即使配置写着 mlx，
  // 推荐的也必须是 faster-whisper，否则按钮点下去必然装失败。
  const appleSilicon = process.arch === 'arm64'
  if (transcribeEngine === 'faster' || !appleSilicon) byCheck.whisper = 'pip-faster-whisper'
  const labels = {}
  for (const [id, action] of Object.entries(FIX_DEP_ACTIONS)) labels[id] = action.label
  return { byCheck, labels }
}

/**
 * Run one whitelisted install command and then re-run the dependency check.
 *
 * Safety rails, in order of importance:
 *  - only an id from FIX_DEP_ACTIONS is accepted; the panel cannot name a command
 *  - no bridge token here, deliberately: that token never reaches the browser
 *    (handing it over would 401 the panel), so what fences this route is the
 *    same-origin/Host check in `route()` plus the fixed action table
 *  - `confirm: true` must be present, so an accidental POST installs nothing
 *  - one install at a time: a second concurrent request is refused, because
 *    two brew runs fight over the same lock and report one bogus failure
 *  - nothing in the scheduled path can reach this: launchd runs
 *    `subtitle-run run`, which never calls a host route
 */
// Install output quotes absolute paths (`/Users/<name>/Library/Python/...`).
// The panel is local, but the same text ends up in screenshots and bug reports,
// so the home prefix is folded to `~` before it ever leaves the host.
function scrubHomePaths(text) {
  const raw = String(text ?? '')
  if (raw === '') return ''
  const home = String(process.env.HOME ?? '').trim()
  const folded = home === '' ? raw : raw.split(home).join('~')
  return folded.slice(-8000)
}

let fixDepInFlight = false

async function runFixDep(ctx, actionId) {
  const action = FIX_DEP_ACTIONS[actionId]
  if (action === undefined) {
    return { ok: false, code: 'unknown-action', error: `unknown action: ${actionId}` }
  }
  // launchd 起的子进程没有 /opt/homebrew/bin，brew 会「找不到命令」。
  // SUBTITLE_STUDIO_FIX_PATH 与 SUBTITLE_STUDIO_ENGINE 同一套路：包管理器装在
  // 非常见位置时由用户指路，命令与参数仍是上面那张常量表里的，改不了。
  const path = [
    String(process.env.SUBTITLE_STUDIO_FIX_PATH ?? '').trim(),
    '/opt/homebrew/bin',
    '/usr/local/bin',
    ...String(process.env.PATH ?? '').split(':').filter((part) => part !== ''),
  ].filter((part) => part !== '').join(':')
  const started = Date.now()
  try {
    const { stdout, stderr } = await execFileAsync(action.command, action.args, {
      env: { ...process.env, PATH: path, HOMEBREW_NO_AUTO_UPDATE: '1' },
      timeout: 15 * 60 * 1000,
      maxBuffer: 4 * 1024 * 1024,
      // 不设 shell：argv 是常量数组，用户输入进不来。
    })
    return {
      ok: true,
      action: actionId,
      command: action.label,
      durationMs: Date.now() - started,
      stdout: scrubHomePaths(stdout),
      stderr: scrubHomePaths(stderr),
    }
  } catch (error) {
    return {
      ok: false,
      action: actionId,
      command: action.label,
      code: error?.code === 'ENOENT' ? 'command-missing' : 'install-failed',
      error: String(error?.message ?? error),
      durationMs: Date.now() - started,
      stdout: scrubHomePaths(error?.stdout),
      stderr: scrubHomePaths(error?.stderr),
    }
  }
}

/* ------------------------------ bridge plumbing ---------------------------- */

let publishedAuthority = ''

/**
 * The authority this plugin is reachable at, remembered from the host's own
 * origin so a Host header naming anything else can be refused.
 * @param {string} origin - `ctx.webServer.getOrigin()`, e.g. http://127.0.0.1:3080 — whatever port the host is on.
 * @returns {void}
 */
function notePublishedOrigin(origin) {
  try {
    publishedAuthority = new URL(String(origin)).host
  } catch {
    publishedAuthority = ''
  }
}

/** True for the loopback literals a local GUI is served on. */
function isLoopbackHostname(hostname) {
  const bare = hostname.replace(/^\[|\]$/g, '').toLowerCase()
  return bare === 'localhost' || bare === '::1' || bare.startsWith('127.')
}

/**
 * Same-site gate. A browser request must name this server in Host, must not be
 * labelled cross-site, and must carry an Origin that equals that Host — the
 * opaque "null" origin (sandboxed iframes, file: pages) is refused.
 *
 * The Host check is the part a plain Origin comparison cannot give you: in a
 * DNS-rebinding attack the attacker's page is served from their own domain on
 * this port, the domain then resolves to 127.0.0.1, and Origin and Host agree
 * while the socket lands here. Host is the one header the victim's browser
 * fills from the URL bar and the attacker cannot forge, so it is compared
 * against the addresses this plugin is actually served on — the same fence DSH
 * applies to its own /api bridge.
 *
 * Non-browser callers (the engine's urllib, the end-to-end test) attach no
 * Origin; they still have to address us by a loopback or published authority.
 * @param {import('node:http').IncomingMessage} request - incoming request.
 * @returns {boolean}
 */
function isSameOrigin(request) {
  const site = request.headers['sec-fetch-site']
  if (typeof site === 'string' && site !== '') {
    if (site !== 'same-origin' && site !== 'same-site' && site !== 'none') return false
  }
  const host = request.headers.host
  if (typeof host !== 'string' || host === '') return false
  let hostUrl
  try {
    hostUrl = new URL(`http://${host}`)
  } catch {
    return false
  }
  if (!isLoopbackHostname(hostUrl.hostname)) {
    if (publishedAuthority === '' || hostUrl.host !== publishedAuthority) return false
  }
  const origin = request.headers.origin
  if (typeof origin === 'string' && origin !== '') {
    if (origin === 'null') return false
    try {
      if (new URL(origin).host !== host) return false
    } catch {
      return false
    }
  }
  return true
}

/** Write one JSON response. */
function writeJson(response, status, value) {
  const body = JSON.stringify(value)
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  response.end(body)
}

/** Read a bounded JSON request body. */
/**
 * Run one `security` command. Reads (`find-generic-password … -w`) never carry the
 * secret in argv — `-w` there means "print it", and the value comes back on stdout.
 * Only the store path passes a secret as an argument: `security -i` (stdin) looks
 * like the alternative but its mini-language drops `\t`/`\n` escapes and hex-mangles
 * non-ASCII, so it cannot round-trip an arbitrary token. The exposure is therefore
 * the width of one exec on a machine where other users can already read your home.
 */
function runSecurity(args) {
  return new Promise((resolve) => {
    let out = ''
    let child
    try {
      child = spawn('security', args, { stdio: ['ignore', 'pipe', 'ignore'] })
    } catch {
      resolve({ ok: false, value: '' })
      return
    }
    child.stdout.on('data', (chunk) => {
      if (out.length < 8192) out += chunk.toString('utf8')
    })
    child.on('error', () => resolve({ ok: false, value: '' }))
    child.on('close', (code) => resolve({ ok: code === 0, value: out.trim() }))
  })
}



/** 配置里的 keychain 服务名（空则回落默认值）。 */
function notifySecretService(notify) {
  return String(notify?.qqSecretService ?? '').trim() || 'subtitle-studio-qqbot'
}

/** 探测 QQ clientSecret 是否就绪：环境变量优先，其次 keychain。 */
async function probeNotifySecret(notify) {
  const service = notifySecretService(notify)
  if ((process.env.SUBTITLE_QQ_SECRET || '').trim() !== '') {
    return { configured: true, service, source: 'env' }
  }
  if (process.platform !== 'darwin') return { configured: false, service, source: '' }
  const appId = String(notify?.qqAppId ?? '').trim()
  const attempts = appId
    ? [['find-generic-password', '-a', appId, '-s', service, '-w'], ['find-generic-password', '-s', service, '-w']]
    : [['find-generic-password', '-s', service, '-w']]
  for (const args of attempts) {
    const result = await runSecurity(args)
    if (result.ok && result.value !== '') return { configured: true, service, source: 'keychain' }
  }
  return { configured: false, service, source: '' }
}

/** 把 secret 写进 keychain（-U 覆盖同账号旧值）。 */
async function storeNotifySecret(notify, secret, appIdOverride = '') {
  const service = notifySecretService(notify)
  const appId = String(appIdOverride ?? '').trim() || String(notify?.qqAppId ?? '').trim() || service
  const result = await runSecurity(['add-generic-password', '-U', '-s', service, '-a', appId, '-w', secret])
  return { ok: result.ok, service, account: appId }
}

/** 删掉 keychain 里的 secret（可能有多条同服务记录，循环删到失败）。 */
async function deleteNotifySecret(notify) {
  const service = notifySecretService(notify)
  let removed = 0
  for (let i = 0; i < 5; i += 1) {
    const result = await runSecurity(['delete-generic-password', '-s', service])
    if (!result.ok) break
    removed += 1
  }
  return { ok: true, service, removed }
}

function readJsonBody(request, maxBytes = MAX_JSON_BODY_BYTES) {
  const type = String(request.headers['content-type'] ?? '')
  if (type !== '' && !type.toLowerCase().includes('application/json')) {
    return Promise.reject(Object.assign(new Error('content-type must be application/json'), { code: 'bad-content-type' }))
  }
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    request.on('data', (chunk) => {
      size += chunk.length
      if (size > maxBytes) {
        // Stop reading but keep the socket alive long enough to answer, so the
        // caller sees a real 413 instead of a connection reset.
        const error = new Error('request body too large')
        error.code = 'payload-too-large'
        request.pause()
        reject(error)
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8')
      if (text.trim() === '') return resolve({})
      try {
        resolve(JSON.parse(text))
      } catch {
        reject(new Error('invalid JSON body'))
      }
    })
    request.on('error', reject)
  })
}

/**
 * Wrap a handler with the cross-origin gate, the method check and error mapping.
 * @param {string} method - required HTTP method.
 * @param {Function} handler - `(request, response, ctx) => Promise<boolean>`; resolves true when it answered.
 * @returns {Function}
 */
function route(method, handler) {
  return async (request, response, ctx) => {
    if (!isSameOrigin(request)) {
      writeJson(response, 403, { ok: false, error: 'forbidden', code: 'cross-origin' })
      return
    }
    if (request.method !== method) {
      response.setHeader('allow', method)
      writeJson(response, 405, { ok: false, error: 'method not allowed', code: 'method' })
      return
    }
    try {
      const handled = await handler(request, response, ctx)
      if (handled === false && !response.writableEnded) {
        writeJson(response, 500, { ok: false, error: 'unhandled request', code: 'internal' })
      }
    } catch (error) {
      if (!response.writableEnded) {
        writeJson(response, 500, { ok: false, error: String(error?.message ?? error), code: 'internal' })
      }
    }
  }
}

/** Decode one query value from a request URL. */
function queryOf(request, key) {
  try {
    const url = new URL(request.url ?? '/', 'http://localhost')
    return url.searchParams.get(key)
  } catch {
    return null
  }
}

/**
 * Ask an Ollama daemon one thing: list its models, or answer a tiny prompt.
 * The address is restricted: loopback always, anything else only when it is
 * exactly the host the user saved. Running Ollama on a NAS is a real setup, but
 * the panel must not become a probe for whatever address a caller types.
 * @param {string} rawHost - base URL from the form, or the saved config.
 * @param {string} apiPath - path under that host, e.g. `api/tags`.
 * @param {object|null} payload - POST body; null sends a GET.
 * @returns {Promise<{ok:boolean, status:number, data?:any, error?:string}>}
 */
async function callOllama(rawHost, apiPath, payload) {
  const host = String(rawHost ?? '').trim()
  if (host === '') return { ok: false, status: 0, error: 'Ollama 地址是空的' }
  let url
  try {
    url = new URL(apiPath, host.endsWith('/') ? host : `${host}/`)
  } catch {
    return { ok: false, status: 0, error: `Ollama 地址不是合法 URL：${host}` }
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { ok: false, status: 0, error: 'Ollama 地址必须是 http(s)' }
  }
  if (!isLoopbackHostname(url.hostname)) {
    const saved = String(currentConfig().engine?.ollamaHost ?? '').trim()
    let savedHost = ''
    try {
      savedHost = new URL(saved).host
    } catch {
      savedHost = ''
    }
    if (savedHost === '' || savedHost !== url.host) {
      return {
        ok: false,
        status: 0,
        error: '这个地址不是本机，也不等于你已保存的 Ollama 地址：请先保存配置，再刷新模型列表',
      }
    }
  }
  let response
  try {
    response = await fetch(url, {
      method: payload === null ? 'GET' : 'POST',
      headers: payload === null ? {} : { 'content-type': 'application/json' },
      body: payload === null ? undefined : JSON.stringify(payload),
      signal: AbortSignal.timeout(8000),
    })
  } catch (error) {
    return { ok: false, status: 0, error: `连不上 Ollama：${String(error?.message ?? error)}` }
  }
  if (!response.ok) return { ok: false, status: response.status, error: `Ollama 返回 ${response.status}` }
  try {
    return { ok: true, status: response.status, data: await response.json() }
  } catch {
    return { ok: false, status: response.status, error: 'Ollama 返回的不是 JSON' }
  }
}

/**
 * Build the bridge routes. Every route is same-origin only; no route runs a
 * shell, and path arguments are only ever used for directory listing or passed
 * verbatim to the engine.
 * @param {object} ctx - host context.
 * @returns {Array<object>}
 */
function makeRoutes(ctx) {
  const exact = (name, method, handler) => ({
    kind: 'exact',
    path: `${BRIDGE_PREFIX}/${name}`,
    handler: route(method, handler),
  })

  return [
    exact('describe', 'GET', async (request, response) => {
      writeJson(response, 200, { ok: true, data: await describe(ctx) })
      return true
    }),

    // The panel's own crash reports. The browser half posts the message and
    // the stack when React throws or a promise rejects; the host writes them
    // to logs/host.log, so a blank settings page can be diagnosed without
    // opening DevTools. Only the three fields below are read, the paths are
    // folded to ~ and the whole body is size-capped.
    exact('client-error', 'POST', async (request, response) => {
      let body
      try {
        body = await readJsonBody(request, 64 * 1024)
      } catch {
        writeJson(response, 400, { ok: false, error: 'bad body', code: 'bad-body' })
        return true
      }
      const kind = String(body?.kind ?? 'error').slice(0, 40)
      const message = scrubHomePaths(String(body?.message ?? '').slice(0, 2000))
      const stack = scrubHomePaths(String(body?.stack ?? '').slice(0, 4000))
      appendHostLog(`client ${kind}: ${message}`)
      if (stack !== '') appendHostLog(`client ${kind} stack: ${stack}`)
      writeJson(response, 200, { ok: true })
      return true
    }),

    // The engine's way into the running DSH llm service. Gated by a shared
    // secret, a per-minute budget and a concurrency cap. Being honest about
    // what the secret buys: it keeps *other users* out (the file is 0600 under
    // a 0700 tree); code running as this same user can read it, which is why
    // the route is clamped and metered rather than merely authenticated.
    exact('llm', 'POST', async (request, response) => {
      const presented = String(request.headers['x-subtitle-studio-token'] ?? '')
      if (!secretEquals(presented, bridgeToken())) {
        writeJson(response, 401, { ok: false, error: 'bridge token required', code: 'unauthorized' })
        return true
      }
      const gated = spendGate()
      if (gated !== null) {
        writeJson(response, gated.status, { ok: false, error: 'relay budget exhausted', code: gated.code })
        return true
      }
      if (relayInFlight >= relayConcurrency()) {
        writeJson(response, 429, { ok: false, error: 'too many concurrent relay calls', code: 'busy' })
        return true
      }
      relayInFlight += 1
      const releaseSlot = () => { relayInFlight = Math.max(0, relayInFlight - 1) }
      let body
      try {
        body = await readJsonBody(request, 4 * 1024 * 1024)
      } catch (error) {
        releaseSlot()
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: error?.code === 'payload-too-large' ? 'payload-too-large' : error?.code === 'bad-content-type' ? 'bad-content-type' : 'bad-body' })
        return true
      }
      const provider = String(body?.provider ?? '').trim()
      const model = String(body?.model ?? '').trim()
      const prompt = String(body?.prompt ?? '')
      if (provider === '' || model === '' || prompt === '') {
        releaseSlot()
        writeJson(response, 422, { ok: false, error: 'provider, model and prompt are required', code: 'bad-request' })
        return true
      }
      const effort = String(body?.reasoningEffort ?? '').trim()
      // An effort id is an opaque identifier, never free text: rejecting a
      // malformed one here beats forwarding it to the adapter on every batch.
      if (effort !== '' && !/^[A-Za-z0-9._-]{1,40}$/.test(effort)) {
        releaseSlot()
        writeJson(response, 422, { ok: false, error: 'reasoningEffort must be a plain identifier', code: 'bad-request' })
        return true
      }
      // A refused effort is deterministic, so find out before paying for a
      // stream that dies at the first token. Only enforce this when DSH
      // actually answered for the model: an unknown model is not evidence
      // that the effort itself is wrong.
      if (effort !== '') {
        const declared = await declaredEfforts(ctx, provider, model)
        if (declared.error === '' && declared.efforts.length > 0 && !declared.efforts.includes(effort)) {
          releaseSlot()
          writeJson(response, 422, {
            ok: false,
            error: unknownEffortMessage(provider, model, effort, declared.efforts),
            code: 'unknown-effort',
          })
          return true
        }
      }
      let result
      try {
        result = await callLlm(ctx, {
        provider,
        model,
        prompt,
        system: typeof body?.system === 'string' ? body.system : '',
        temperature: clampTemperature(body?.temperature, undefined),
          maxTokens: clampTokens(body?.maxTokens, relayMaxTokens(), relayMaxTokens()),
          reasoningEffort: effort,
        })
      } finally {
        // A throwing stream must not leak the slot and wedge the relay.
        releaseSlot()
      }
      if (result.ok !== true) {
        if (result.code === 'token-limit') {
          // A cut-off answer still contains the blocks that did land. Returning
          // it as a success with `truncated` lets the engine keep those and
          // re-ask only for the tail; a 502 would throw the whole batch away.
          writeJson(response, 200, {
            ok: true,
            truncated: true,
            data: { text: result.text, finish: result.finish, usage: result.usage ?? {} },
          })
          return true
        }
        if (result.code === 'unknown-effort') {
          // The provider refused the effort: asking again with the same effort
          // gets the same refusal, so answer with a configuration error.
          writeJson(response, 422, { ok: false, error: result.error, code: 'unknown-effort' })
          return true
        }
        if (result.code === 'upstream-rate-limit') {
          // The provider itself said "slow down". Back off like it means it.
          // 真·上游额度：用 upstream-rate-limit 与本地闸门区分开
          writeJson(response, 429, { ok: false, error: result.error, code: 'upstream-rate-limit' })
          return true
        }
        const status = result.code === 'timeout' ? 504 : 502
        writeJson(response, status, {
          ok: false,
          error: result.error ?? 'llm call failed',
          code: result.code ?? 'llm-failed',
        })
        return true
      }
      writeJson(response, 200, { ok: true, data: { text: result.text, finish: result.finish, usage: result.usage ?? {} } })
      return true
    }),

    // The panel's "try this model" button. Same-origin only, and the reply is
    // capped so a stray click cannot turn into a large bill.
    exact('model-test', 'POST', async (request, response) => {
      const gated = spendGate()
      if (gated !== null) {
        writeJson(response, gated.status, { ok: false, error: 'model test budget exhausted', code: gated.code })
        return true
      }
      let body
      try {
        body = await readJsonBody(request)
      } catch (error) {
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: error?.code === 'payload-too-large' ? 'payload-too-large' : error?.code === 'bad-content-type' ? 'bad-content-type' : 'bad-body' })
        return true
      }
      const kind = String(body?.kind ?? 'dsh').trim() || 'dsh'
      const provider = String(body?.provider ?? '').trim()
      const model = String(body?.model ?? '').trim()
      if (model === '') {
        writeJson(response, 422, { ok: false, error: 'model is required', code: 'bad-request' })
        return true
      }
      if (kind === 'ollama') {
        const host = String(body?.host ?? '').trim() || String(currentConfig().engine?.ollamaHost ?? '')
        const answer = await callOllama(host, 'api/generate', {
          model,
          prompt: 'Reply with ok.',
          stream: false,
          // qwen3 and friends think before they speak: without the flag the
          // whole budget goes to `thinking` and `response` comes back empty,
          // which reads like a dead model. Budget a little room as well.
          think: false,
          options: { num_predict: 64 },
        })
        if (answer.ok !== true) {
          writeJson(response, 502, { ok: false, error: answer.error ?? 'ollama call failed', code: 'ollama-failed' })
          return true
        }
        const text = String(answer.data?.response ?? '').trim()
        if (text === '' && String(answer.data?.thinking ?? '').trim() !== '') {
          writeJson(response, 200, {
            ok: true,
            data: { text: '有响应，但该模型只输出 thinking（翻译时建议换非思考模型）', finish: 'stop' },
          })
          return true
        }
        writeJson(response, 200, { ok: true, data: { text, finish: 'stop' } })
        return true
      }
      if (provider === '') {
        writeJson(response, 422, { ok: false, error: 'provider is required for the DSH relay', code: 'bad-request' })
        return true
      }
      const testEffort = String(body?.reasoningEffort ?? currentConfig().engine?.reasoningEffort ?? '').trim()
      if (testEffort !== '' && /^[A-Za-z0-9._-]{1,40}$/.test(testEffort)) {
        const declared = await declaredEfforts(ctx, provider, model)
        if (declared.error === '' && declared.efforts.length > 0 && !declared.efforts.includes(testEffort)) {
          writeJson(response, 200, {
            ok: false,
            warning: 'unknown-effort',
            error: unknownEffortMessage(provider, model, testEffort, declared.efforts),
          })
          return true
        }
      }
      const result = await callLlm(ctx, {
        provider,
        model,
        prompt: String(body?.prompt ?? '回复 ok 即可').slice(0, 4000),
        system: typeof body?.system === 'string' ? body.system.slice(0, 8000) : '',
        temperature: 0,
        maxTokens: clampTokens(body?.maxTokens, 32, 256),
        reasoningEffort: /^[A-Za-z0-9._-]{1,40}$/.test(testEffort) ? testEffort : '',
      })
      if (result.ok !== true) {
        // A refused effort is a configuration answer: the panel shows it as a
        // warning, because the model itself is fine.
        if (result.code === 'unknown-effort') {
          writeJson(response, 200, {
            ok: false,
            warning: 'unknown-effort',
            error: result.error ?? 'this model does not support that reasoning effort',
          })
          return true
        }
        // A truncated answer is still an answer: say so, because the panel uses
        // this button as the pre-flight for a real run.
        if (result.code === 'token-limit') {
          writeJson(response, 200, {
            ok: true,
            data: {
              text: result.text === ''
                ? '有响应预算全花在思考上，正文被截断（换非思考模型、调低思考档位，或加大最大 token）'
                : result.text,
              finish: 'max-tokens',
              usage: result.usage ?? {},
              warning: 'token-limit',
            },
          })
          return true
        }
        writeJson(response, 502, { ok: false, error: result.error ?? 'llm call failed', code: result.code ?? 'llm-failed' })
        return true
      }
      writeJson(response, 200, { ok: true, data: { text: result.text, finish: result.finish, usage: result.usage ?? {} } })
      return true
    }),

    // What can this model actually be told? Effort ids are model metadata, so
    // the panel asks instead of guessing. Failure is a 200 with an explanation:
    // this is a probe, and "no declared thinking levels" is a normal answer.
    exact('model-info', 'GET', async (request, response) => {
      const provider = String(queryOf(request, 'provider') ?? '').trim()
      const model = String(queryOf(request, 'model') ?? '').trim()
      if (provider === '' || model === '') {
        writeJson(response, 422, { ok: false, error: 'provider and model are required', code: 'bad-request' })
        return true
      }
      let llm
      try {
        llm = ctx.get('llm')
      } catch {
        llm = null
      }
      if (llm === undefined || llm === null || typeof llm.resolveModelInfo !== 'function') {
        writeJson(response, 200, { ok: true, data: { efforts: [], defaultEffort: '', error: 'DSH 未提供模型能力查询' } })
        return true
      }
      try {
        const info = await llm.resolveModelInfo(provider, model)
        const efforts = Array.isArray(info?.reasoning?.efforts)
          ? info.reasoning.efforts
            .map((entry) => ({
              id: String(entry?.id ?? ''),
              name: String(entry?.name ?? entry?.id ?? ''),
              description: typeof entry?.description === 'string' ? entry.description.slice(0, 200) : '',
            }))
            .filter((entry) => entry.id !== '')
          : []
        writeJson(response, 200, {
          ok: true,
          data: {
            efforts,
            defaultEffort: String(info?.reasoning?.defaultEffort ?? ''),
            defaultMaxTokens: Number.isFinite(info?.defaultMaxTokens) ? info.defaultMaxTokens : 0,
            error: efforts.length === 0 ? '该模型未声明思考档位（留空即跟随默认）' : '',
          },
        })
      } catch (error) {
        writeJson(response, 200, {
          ok: true,
          data: { efforts: [], defaultEffort: '', error: String(error?.message ?? error).slice(0, 200) },
        })
      }
      return true
    }),

    // 列本机 Ollama 的模型。失败也回 200 + error 文案：这是一次探测，不是错误页。
    exact('ollama-models', 'GET', async (request, response) => {
      const host = String(queryOf(request, 'host') ?? '').trim() || String(currentConfig().engine?.ollamaHost ?? '')
      const listed = await callOllama(host, 'api/tags', null)
      if (listed.ok !== true) {
        writeJson(response, 200, { ok: true, data: { host, models: [], error: listed.error ?? 'ollama unreachable' } })
        return true
      }
      const models = Array.isArray(listed.data?.models)
        ? listed.data.models
            .map((item) => String(item?.name ?? '').trim())
            .filter((name) => name !== '')
            .slice(0, 300)
        : []
      writeJson(response, 200, { ok: true, data: { host, models, error: '' } })
      return true
    }),

    exact('save', 'POST', async (request, response) => {
      let body
      try {
        body = await readJsonBody(request)
      } catch (error) {
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: error?.code === 'payload-too-large' ? 'payload-too-large' : error?.code === 'bad-content-type' ? 'bad-content-type' : 'bad-body' })
        return true
      }
      const submitted = body?.config
      if (!isPlainObject(submitted)) {
        writeJson(response, 400, { ok: false, error: 'config must be an object', code: 'bad-body' })
        return true
      }
      const saved = saveConfig(submitted)
      if (saved.ok !== true) {
        const status = saved.code === 'config-malformed' ? 409 : saved.code === 'write-failed' ? 500 : 422
        writeJson(response, status, {
          ok: false,
          error: saved.message ?? 'validation failed',
          code: saved.code ?? 'validation',
          issues: saved.issues ?? [],
        })
        return true
      }
      // Warnings survive a successful save: the panel needs them, otherwise a
      // field the engine only tolerates (Opus in an .mp4 template, an empty
      // ffmpeg path) saves silently and surprises the user at run time.
      writeJson(response, 200, {
        ok: true,
        data: await describe(ctx),
        warnings: (saved.issues ?? []).filter((issue) => issue.level === 'warn'),
      })
      return true
    }),

    exact('reset', 'POST', async (request, response) => {
      const result = resetConfig()
      if (result.ok !== true) {
        writeJson(response, 500, { ok: false, error: result.message ?? 'reset failed', code: result.code })
        return true
      }
      const data = await describe(ctx)
      writeJson(response, 200, { ok: true, data: { ...data, resetBackup: result.backup } })
      return true
    }),

    exact('plan', 'POST', async (request, response) => {
      const result = await runEngineOnce('plan', [])
      writeJson(response, 200, result)
      return true
    }),

    exact('doctor', 'POST', async (request, response) => {
      const result = await runEngineOnce('doctor', [])
      writeJson(response, 200, result)
      return true
    }),

    exact('selfcheck', 'POST', async (request, response) => {
      const result = await runSelfCheck({ force: queryOf(request, 'force') === '1' })
      writeJson(response, result.ok === true ? 200 : 500, { ok: result.ok === true, data: result.data })
      return true
    }),

    exact('fix-dep', 'POST', async (request, response) => {
      // Same fence as every other route (`route()` already rejected cross-site
      // and rebound hosts). No bridge token here: the token exists to stop the
      // engine's relay from being read by *another user's* process, and it is
      // never handed to the browser. What actually protects this route is the
      // fixed action table (the panel cannot name a command) plus `confirm`,
      // which no automatic caller sets — and anyone who could forge both is
      // already able to run `brew` themselves.
      let body
      try {
        body = await readJsonBody(request)
      } catch (error) {
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: 'bad-body' })
        return true
      }
      if (body?.confirm !== true) {
        writeJson(response, 422, {
          ok: false,
          code: 'confirm-required',
          error: 'installing a dependency changes this machine: send confirm: true once the user has agreed',
        })
        return true
      }
      const actionId = String(body?.action ?? '').trim()
      if (actionId === '') {
        writeJson(response, 422, { ok: false, error: 'action is required', code: 'invalid-action' })
        return true
      }
      if (FIX_DEP_ACTIONS[actionId] === undefined) {
        // 面板只能从这张表里挑；不在表上就地拒绝，连自检都不跑。
        writeJson(response, 422, {
          ok: false,
          code: 'unknown-action',
          error: `that action is not in the allowed list: ${actionId.slice(0, 60)}`,
        })
        return true
      }
      if (fixDepInFlight) {
        // 两条 brew 会抢同一个锁，第二条只会报一条看不懂的失败。
        writeJson(response, 409, {
          ok: false,
          code: 'already-running',
          error: 'another install is still running; wait for it to finish',
        })
        return true
      }
      fixDepInFlight = true
      let result
      try {
        result = await runFixDep(ctx, actionId)
      } finally {
        fixDepInFlight = false
      }
      appendHostLog(
        `fix-dep ${actionId}: ${result.ok === true ? 'ok' : `failed (${result.code ?? 'error'})`} in ${result.durationMs ?? 0}ms`,
      )
      // 装完立刻重跑一次自检：面板要看到的是「现在到底行不行」，
      // 而不是六小时前那份缓存。
      const check = await runSelfCheck({ force: true })
      writeJson(response, result.ok === true ? 200 : 500, {
        ok: result.ok === true,
        data: { ...result, selfcheck: check?.data ?? null },
      })
      return true
    }),

    exact('notify-test', 'POST', async (request, response) => {
      const result = await runEngineOnce('notify-test', [], 60_000)
      const data = result?.result?.data ?? null
      if (result?.ok === true && data?.sent === true) {
        writeJson(response, 200, { ok: true, data })
        return true
      }
      writeJson(response, 500, {
        ok: false,
        code: 'notify-failed',
        error: data?.reason || result?.message || 'notify test failed',
        data,
      })
      return true
    }),

    exact('notify-secret', 'POST', async (request, response) => {
      let body
      try {
        body = await readJsonBody(request)
      } catch (error) {
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: error?.code === 'payload-too-large' ? 'payload-too-large' : error?.code === 'bad-content-type' ? 'bad-content-type' : 'bad-body' })
        return true
      }
      const config = currentConfig() || {}
      const notify = config.notify || {}
      if (body?.delete === true) {
        if (process.platform !== 'darwin') {
          writeJson(response, 422, { ok: false, error: 'keychain 只在 macOS 可用；可改用 SUBTITLE_QQ_SECRET 环境变量', code: 'unsupported' })
          return true
        }
        const result = await deleteNotifySecret(notify)
        writeJson(response, 200, { ok: true, data: { configured: false, service: result.service } })
        return true
      }
      const secret = typeof body?.secret === 'string' ? body.secret.trim() : ''
      if (secret === '') {
        writeJson(response, 422, { ok: false, error: 'secret must be a non-empty string', code: 'invalid-secret' })
        return true
      }
      if (process.platform !== 'darwin') {
        writeJson(response, 422, { ok: false, error: 'keychain 只在 macOS 可用；可改用 SUBTITLE_QQ_SECRET 环境变量', code: 'unsupported' })
        return true
      }
      const appIdOverride = typeof body?.appId === 'string' ? body.appId : ''
      const result = await storeNotifySecret(notify, secret, appIdOverride)
      if (!result.ok) {
        writeJson(response, 500, { ok: false, error: 'keychain 写入失败（security add-generic-password 返回非 0）', code: 'keychain-write-failed' })
        return true
      }
      writeJson(response, 200, { ok: true, data: { configured: true, service: result.service, account: result.account } })
      return true
    }),

    exact('engine-status', 'GET', async (request, response) => {
      const result = await runEngineOnce('status', [], 120_000)
      writeJson(response, 200, result)
      return true
    }),

    exact('run', 'POST', async (request, response) => {
      let body = {}
      try {
        body = await readJsonBody(request)
      } catch (error) {
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: error?.code === 'payload-too-large' ? 'payload-too-large' : error?.code === 'bad-content-type' ? 'bad-content-type' : 'bad-body' })
        return true
      }
      const max = body?.max === undefined || body.max === null || body.max === '' ? undefined : Number(body.max)
      if (max !== undefined && (!Number.isInteger(max) || max < 0 || max > 1000)) {
        writeJson(response, 422, { ok: false, error: 'max must be an integer between 0 and 1000', code: 'invalid-max' })
        return true
      }
      const only = body?.only === undefined || body.only === null ? undefined : String(body.only)
      if (only !== undefined && (only.length > 1024 || only.includes('\0'))) {
        writeJson(response, 422, { ok: false, error: 'only is too long', code: 'invalid-only' })
        return true
      }
      const started = startJob('run', { max, only, dryRun: body?.dryRun === true })
      if (started.ok !== true) {
        writeJson(response, started.code === 'already-running' ? 409 : 424, started)
        return true
      }
      writeJson(response, 200, started)
      return true
    }),

    exact('selftest', 'POST', async (request, response) => {
      const started = startJob('selftest', {})
      if (started.ok !== true) {
        writeJson(response, started.code === 'already-running' ? 409 : 424, started)
        return true
      }
      writeJson(response, 200, started)
      return true
    }),

    exact('status', 'GET', async (request, response) => {
      const current = jobState.current
      writeJson(response, 200, {
        ok: true,
        data: {
          running: current !== null,
          current: summarize(current),
          last: readLastRun(),
          history: jobState.history,
          lines: current === null ? [] : current.lines.slice(-RUN_LOG_PREVIEW_LINES),
          engineExists: engineReady(),
          configPath: CONFIG_PATH,
        },
      })
      return true
    }),

    exact('cancel', 'POST', async (request, response) => {
      let body = {}
      try {
        body = await readJsonBody(request)
      } catch {
        body = {}
      }
      const result = cancelJob(body?.runId)
      writeJson(response, result.ok === true ? 200 : 409, result)
      return true
    }),

    exact('logs', 'GET', async (request, response) => {
      const name = queryOf(request, 'name') ?? 'run'
      const requested = Number(queryOf(request, 'lines') ?? DEFAULT_LOG_LINES)
      const lines = Number.isFinite(requested)
        ? Math.min(MAX_LOG_LINES, Math.max(1, Math.trunc(requested) || DEFAULT_LOG_LINES))
        : DEFAULT_LOG_LINES
      const file = resolveLogFile(name)
      if (file === null) {
        // Fall back to the in-memory capture of the running or last job, so the
        // default "run" view still shows something before the engine has written
        // its own log. Any other name stays a hard 404.
        const job = jobState.current ?? jobState.last
        const wanted = String(name ?? 'run').replace(/\.log$/, '')
        const jobLog = job === null || job === undefined ? null : job.kind === 'selftest' ? 'selftest' : 'run'
        if (job !== null && job !== undefined && jobLog === wanted) {
          writeJson(response, 200, {
            ok: true,
            data: {
              name,
              path: null,
              lines: job.lines.slice(-lines),
              total: job.lines.length,
              truncated: job.lines.length > lines,
              source: 'memory',
              available: listLogFiles(),
            },
          })
          return true
        }
        writeJson(response, 404, {
          ok: false,
          error: `log ${name} does not exist yet`,
          code: 'not-found',
          available: listLogFiles(),
        })
        return true
      }
      writeJson(response, 200, {
        ok: true,
        data: { name, path: file, ...tailFile(file, lines), source: 'file', available: listLogFiles() },
      })
      return true
    }),

    exact('listdir', 'GET', async (request, response) => {
      const requested = queryOf(request, 'path')
      const includeFiles = queryOf(request, 'files') === '1' || queryOf(request, 'files') === 'true'
      const result = await listDirectory(requested, includeFiles)
      writeJson(response, result.ok === true ? 200 : result.code === 'not-found' ? 404 : 400, result)
      return true
    }),

    exact('schedule', 'POST', async (request, response) => {
      let body
      try {
        body = await readJsonBody(request)
      } catch (error) {
        writeJson(response, error?.code === 'payload-too-large' ? 413 : error?.code === 'bad-content-type' ? 415 : 400, { ok: false, error: String(error?.message ?? error), code: error?.code === 'payload-too-large' ? 'payload-too-large' : error?.code === 'bad-content-type' ? 'bad-content-type' : 'bad-body' })
        return true
      }
      // The panel may send the whole draft; only scheduling keys are taken.
      const requested = isPlainObject(body?.config) ? body.config : {}
      const current = readConfigFile()
      const base = hydrate(current.parsed)
      const candidate = hydrate(deepMerge(base, isPlainObject(requested.schedule) ? { schedule: requested.schedule } : {}))
      const issues = validateSubmitted(candidate)
      if (issues.some((issue) => issue.level === 'error')) {
        writeJson(response, 422, { ok: false, error: 'validation failed', code: 'validation', issues })
        return true
      }
      const applied = await applySchedule(candidate)
      const status = await scheduleStatus(candidate)
      if (applied.ok !== true) {
        writeJson(response, 500, {
          ok: false,
          error: applied.message ?? 'schedule failed',
          code: applied.code,
          steps: applied.steps ?? [],
          data: status,
        })
        return true
      }
      // launchd is now running the new clock. If config.json still carried the
      // old one, the panel would echo a schedule that is not in effect.
      const persisted = saveConfig(candidate)
      writeJson(response, 200, {
        ok: true,
        data: {
          ...status,
          steps: applied.steps ?? [],
          ...(applied.data ?? {}),
          persisted: persisted.ok === true,
          ...(persisted.ok === true ? {} : { persistError: persisted.message ?? persisted.code ?? 'write-failed' }),
        },
      })
      return true
    }),
  ]
}

const RUN_LOG_PREVIEW_LINES = 200

/**
 * Register the bridge for as long as this plugin is mounted.
 * @param {object} ctx - host context with webServer.
 */
export function apply(ctx) {
  // 装好就自检一次：缺 ffmpeg / whisper 这类依赖必须马上可见，而不是等到
  // 用户半夜第一次真跑时才在日志里撞见。
  const selfCheckTimer = setTimeout(() => {
    runSelfCheck({ force: false }).catch((error) => {
      appendHostLog(`selfcheck failed: ${String(error?.message ?? error)}`)
    })
  }, 2_000)
  if (typeof selfCheckTimer.unref === 'function') selfCheckTimer.unref()

  // 引擎要通过这个文件找到宿主：端口变了就重写，引擎每次调用现读，不会过期。
  const publishBridge = () => {
    try {
      // 令牌文件先落地：端点文件里指着一个还不存在的令牌路径，引擎会直接报「读不到」。
      bridgeToken()
      const origin = typeof ctx.webServer?.getOrigin === 'function' ? ctx.webServer.getOrigin() : ''
      try {
        fs.chmodSync(STATE_DIR, 0o700)
      } catch {
        // best effort: the directory may belong to another uid
      }
      if (origin !== '') {
        notePublishedOrigin(origin)
        writeBridgeEndpoint(origin)
      }
    } catch (error) {
      appendHostLog(`bridge endpoint could not be published: ${String(error?.message ?? error)}`)
    }
  }
  publishBridge()

  ctx.effect(() => {
    const routes = makeRoutes(ctx)
    const disposers = routes.map((entry) => ctx.webServer.register(entry))
    return () => {
      clearTimeout(selfCheckTimer)
      for (const dispose of disposers) dispose()
      // A background engine child must not outlive the plugin.
      const job = jobState.current
      if (job !== null) {
        try {
          job.child?.kill('SIGTERM')
        } catch {
          // Already gone.
        }
      }
    }
  }, 'dsh-subtitle-studio: config and engine bridge')
}

export const inject = ['webServer']

# 字幕工坊 · 引擎（subtitle-run）

纯 Python 3 标准库 + 系统命令实现的字幕流水线引擎，零 Node 依赖、不 import DSH/插件代码，
只吃 `config.json`，只吐 stdout/JSON 与文件。UI 侧（`index.js` / `client.js`）通过下面六个
子命令与引擎交互，二者边界见 `DESIGN.md` 第 2 节。

```
engine/
├── subtitle-run          # 唯一入口（可执行，python3 shebang）
├── lib/
│   ├── config.py         # 读 config.json + 默认值深合并 + 结构化校验
│   ├── keys.py           # keychain 读取（security 三策略）+ 环境变量优先
│   ├── remote.py         # local / ssh 两类端点：列目录、下载、上传、stat、df
│   ├── select.py         # 选片：包含/排除/体积/龄期/跳过已翻译/排序/配额/dry-run 原因
│   ├── prompts.py        # prompt profile（glob）匹配与合并
│   ├── pipeline.py       # 单片流水线 + run 编排 + status + selftest
│   ├── doctor.py         # 依赖与连通性自检
│   ├── notify.py         # 通知（用户自配脚本或 QQ Bot API，缺省不发送）
│   └── runtime.py        # 日志、原子写、子进程、锁、格式化等基础设施
├── vendor/               # 外部脚本改造而来的子步骤实现
└── README.md
```

## 1. 依赖

| 依赖 | 用途 | 缺失时 |
|---|---|---|
| `python3` ≥ 3.9（引擎本身只用标准库） | 引擎 | doctor 报错（exit 3） |
| `ffmpeg` / `ffprobe`（需 `ass`、`subtitles`、`testsrc2`、`sine` 滤镜 + `libx264`/`aac` 编码器） | 抽音频、分块、压制、探测 | doctor 报错 |
| `mlx_whisper`（Apple Silicon）或 `faster-whisper`（Intel / CPU / CUDA） | 语音转写，见 `engine/requirements.txt` | doctor 报错 |
| Whisper 模型（默认 `mlx-community/whisper-large-v3-turbo`，HF 缓存需已下载） | 转写 | doctor 报错 |
| `ssh` / `scp` 语义（仅 ssh 来源/输出时） | 远端列目录、上下载 | doctor 报错 |
| `mmx` CLI（仅 `engine.kind=minimax`） | 翻译 | doctor 报错 |
| 用户自备的通知脚本（可选，`notify.qqSendPath`） | 通知 | 静默跳过 |

除调度用的 `PATH` 兜底目录外，可执行文件路径都来自 `config.json`（`burn.ffmpeg` 等）或 `PATH`，
不含任何用户机器专属的绝对路径。注意：凭据读取走 macOS 钥匙串（`security`），定时调度走 launchd，
两者都是 macOS 专有的——本包不为其他系统改造，`package.json` 已声明 `os: ["darwin"]`；
纯转写/翻译管线（`pipeline.py`、`vendor/*.py`）本身与操作系统无关，移植需要重做的是宿主与调度层。

## 2. 命令

```bash
ENGINE=<包目录>/engine/subtitle-run
CFG=~/.dsh/data/subtitle-studio/config.json

$ENGINE doctor   --config "$CFG" [--json]                      # 依赖/连通性自检
$ENGINE plan     --config "$CFG" [--max N] [--only "<文件名>"] [--json]   # 只选片，不动文件
$ENGINE run      --config "$CFG" [--max N] [--only "<文件名>"] [--dry-run] [--json]
$ENGINE guard    --config "$CFG" [--max N] [--json]            # 幂等日闸门：今天没跑才跑一轮
$ENGINE status   --config "$CFG" [--json]                      # 上次运行 + 今日用量 + 锁状态
$ENGINE selftest --config "$CFG" [--skip-translate] [--keep] [--json]    # 5 秒合成链路
```

- `--only "<完整文件名>"`：强制只处理该条（无视「已译制」与跳过名单，但不无视体积/龄期/包含规则）。
- `--max N`：单次上限，覆盖 `selection.maxPerRun`。
- `--dry-run`：只列动作计划，不下载、不转写、不翻译、不压制、不写状态、不建数据目录。
- `guard`：幂等日闸门，供定时器调用。今日用量 `count > 0`、配额已满或已有实例在跑 → 直接跳过（exit 0，不写用量、不发通知）；否则跑一轮（默认 `selection.maxPerRun` 条，未配置按 1 条）。结果同时落 `state/guard-last.json`，供插件与人工查看。
- `--json`：**stdout 只有 JSON**，所有进度/日志走 stderr 与日志文件；不带 `--json` 时输出人类可读文本。
- `--skip-translate`（仅 selftest）：跳过翻译，只验证 ffmpeg 压制链路（离线可用）。
- `--keep`（仅 selftest）：保留上一轮 selftest 工作目录（默认每次清空重建）。

## 3. 退出码

| 码 | 含义 | 典型触发 |
|---|---|---|
| 0 | 正常（含「无候选」） | plan 无选中、run 全部成功/跳过 |
| 2 | 配置错误 | 文件不存在、JSON 非法、字段非法、`nameTemplate` 缺 `{stem}` |
| 3 | 依赖缺失 | ffmpeg/ffprobe/python/whisper/mmx 不可用 |
| 4 | 来源不可达 | SSH 失败、目录不存在、`find` 不可用 |
| 5 | 引擎调用失败 | mmx/OpenAI 兼容接口报错、翻译产物为空 |
| 6 | ffmpeg 失败 | 抽音频/分块/压制非零退出 |
| 7 | 上传失败 | 远端写入或大小校验失败 |
| 8 | 磁盘不足 | 本地可用空间 < `limits.minFreeGB` |
| 9 | 已有实例在跑 | `state/run.lock` 持有者 pid 仍存活 |

`--json` 失败时仍输出 `{"ok": false, "error": {"code", "message", "exitCode", "detail"}}` 并以对应码退出
（doctor 例外：按 DESIGN 契约进程退出码固定 **0/1**，自检失败时输出
`{"ok": false, "data": {...}, "error": {"code": "doctor_failed", "exitCode": 1}}`，具体失败项见 `data.checks`）。

## 4. JSON 形状（`--json`）

统一外壳：成功 `{"ok": true, "data": {...}}`，失败 `{"ok": false, "error": {...}}`。

**doctor.data**：`version, generatedAt, configPath, dataDir, engine{kind,model}, checks[{name,group,level,message,detail}], summary{ok,warn,error,info}, verdict(green|degraded|failed)`

**plan.data**：
```
generatedAt, configPath,
source{kind,path,display,reachable,probe{ok,detail,latencyMs},include[],recursive,fileCount},
output{kind,path,display,exists,existingCount,nameTemplate},
selection{order,maxPerRun,skipTranslated,usedToday,remaining,counts{total,selected,skipped,byReason{}}},
quota{maxPerRun,usedToday,remaining,unlimited},
candidates[{name,path,sizeBytes,sizeGB,mtime,mtimeISO,ageDays,expectedOutput,selected,reason,reasonText}],
selected[], selectedDetail[{...,promptProfile,promptMatched}], skipListSize, dataDir
```

**run --dry-run**：`dryRun:true, note, generatedAt, configPath, source, output, selection, quota, candidates, selected, actions[]`，
其中 `actions[].stages` 为将执行的阶段名（`download → extract-audio → segment → transcribe → prefilter → translate
→ retry → quality-gate → trim-silence → burn → upload`），`actions[]` 另含 `sizeGB/expectedOutput/workDir/promptProfile/engine/overwrite`。

**run.data**：`runId, mode, startedAt, finishedAt, configPath, source, output, selected[],
results[{name,status(ok|skipped|failed),stage,message,code,output,outputBytes,elapsedSec,workDir,translation{blocks,untranslated,untranslatedPct},promptProfile,detail?}],
totals{selected,ok,skipped,failed}`

**guard.data**：`at, action(run|skip), reason, reasonText, today{date,count,maxPerRun}, lastRun{startedAt,finishedAt,totals}, configPath, run{runId,startedAt,finishedAt,totals,exitCode}`（跳过时无 `run`）。`reason` ∈ `already-running | quota-exhausted | already-ran-today | retry-after-failure | no-run-today`。

**status.data**：`generatedAt, running, lock{path,holder}, lastRun(同 run.data，可为 null),
today{date,count,maxPerRun,items[{name,at,status,output,elapsedSec}]}, skipList{count,entries{}},
paths{dataDir,workDir,logsDir,stateDir}, logs{runLog,sizeBytes,workerLogSizeBytes}, engine{kind,model}`

**selftest.data**：`ok, artifact, artifactBytes, workDir, translate{skipped,engine,model,blocks,hasChinese}, ffprobe{...}, sourceProbe{...}, steps[{name,detail,at}], generatedAt`

## 5. 配置

`config.json`（0600，允许 JSONC 注释）。所有字段都有内置默认值（`lib/config.py:DEFAULTS`），
未知顶层键只告警并原样保留，**密钥只存 `engine.apiKeyRef`（keychain 条目名），配置文件里禁止明文**。
关键分组：`engine / source / output / selection / prompts / subtitles / burn / transcribe / notify / schedule / paths / limits`。

- `paths.dataDir` 缺省 `~/.dsh/data/subtitle-studio`；`engine.kind` ∈ `dsh|minimax|ollama|deepseek|openai-compatible`。
  `dsh` 表示把翻译请求交给 DSH 宿主（凭据与模型都由宿主配置），引擎本身不接触任何密钥。
- 补充的可选键：`limits.minFreeGB`（默认 15）、`engine.timeoutSeconds`、`burn.encoder`（`libx264|h264_videotoolbox`）、
  `burn.videoBitrate`、`transcribe.chunkSeconds`（默认 60）、`transcribe.silenceDb`（默认 -50）、
  `translation.rerunThresholdPct/Count`（默认 5 / 50）、`notify.enabled`、`paths.keepWorkArtifacts`。
- v1.4 新增键：`output.mode`（`burn|srt`，缺省按 `burn.enabled` 回落）、`subtitles.styleTemplate`
  （`default|bold-yellow|top-white|boxed|cinema`）、`transcribe.conditionOnPreviousText`、
  `transcribe.temperature`（0–2，0 = 只走最可能一次）、`transcribe.hallucinationSilenceThreshold`、
  `transcribe.minAvgLogprob`（≤0）、`transcribe.maxNoSpeechProb`（0–1）、
  `transcribe.keepThanksPhrases`、`transcribe.keepRepeats`。
- 引擎凭据解析顺序：环境变量（`MINIMAX_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `SUBTITLE_API_KEY`）
  → keychain（`apiKeyRef`，三策略：`-l` / `-a + -s` / `-s`）。

## 6. 数据目录与原子性

```
~/.dsh/data/subtitle-studio/
├── config.json          # 正式安装位置（本引擎只读，不写）
├── work/<stem>-<md5[:8]>/   # 单片工作目录
├── logs/{run,doctor}.log    # 按天滚动为 run-YYYY-MM-DD.log；>5MB 自截断保留最后 1MB
└── state/
    ├── run.lock         # {pid, startedAt, host}；死 pid 自动回收，跨机不复用
    ├── last-run.json    # 最近一次 run 的完整结果（随处理进度增量落盘）
    ├── usage-YYYY-MM-DD.json  # 当日已处理条数（配额来源）
    ├── guard-last.json        # 最近一次 guard 的判定与结果
    └── skipped.json     # 跳过名单（如 no-speech），plan 时给出 skip-list 原因
```

原子性约定：
- 下载先写 `<target>.partial`，校验字节数与远端 `stat` 一致后再 `os.replace`；SSH 直传的静默截断会被拦下。
- 产物先写 `<name>.partial<ext>`（保留扩展名，否则 ffmpeg 无法推断 muxer）再改名。
- 上传先写远端 `<name>.partial`，大小校验通过后 `mv -f` 原子替换；`output.overwrite=false` 时拒绝覆盖。
- 失败不污染输出目录；工作目录只保留 `source.srt` / `source.zh.srt`（`subtitles.keepOriginalAsSrt`）便于重烧。

## 7. 流水线阶段

1. **download**：local 直接引用原文件；ssh 拉取到 `work/<stem>/input.<ext>` 并校验大小。
2. **extract-audio**：`ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le` → `audio.wav`。
   刻意不做 `silenceremove`（旧流程实测会导致 14 分钟以上的时间轴漂移）。
2b. **align**（音轨偏移补偿）：ffprobe 量出音频轨/视频轨的 start_time 与音轨时长、wav 时长，
    算出「容器里有、抽音频时丢了」的前置静音 `shift=(A-V0)-max(0, W-D_a)`；
    `shift ≥ 0.2s` 时用 `adelay` 把这段静音补回音频开头（`align_audio_timeline`），`shift ≤ -0.2s` 只记录不修正。
3. **segment**：按 `transcribe.chunkSeconds`（默认 60s）切片，可断点续跑（已存在 chunk 跳过）。
4. **transcribe**：`volumedetect` 低于 `silenceDb` 的 chunk 打 `.silent` 标记跳过；其余批量交给
   `vendor/transcribe_batch.py`（模型只加载一次），支持 MLX/faster-whisper 两引擎。
   反幻觉参数由 `transcribe.*` 透传：`conditionOnPreviousText`（承接上文，默认关 = 默认不承接）、
   `temperature`（0 = 只走最可能一次；>0 = 递增温度阶梯重解码）、`hallucinationSilenceThreshold`、
   `minAvgLogprob` / `maxNoSpeechProb`（置信度过滤）、`keepThanksPhrases` / `keepRepeats`
   （保留致谢片尾语与重复行；默认都丢）。
5. **merge**：用 **ffprobe 的真实 chunk 时长累加**做偏移（不是 `idx*60`），丢弃 >30s 或空文本块；
   全部为空 → 跳过该片并写入 `skipped.json`（`no-speech`）。
6. **prefilter**：`vendor/prefilter_and_rerun.py --no-rerun` 生成 `source.srt.prefilter`（纯假名/短假名/广告宣番打占位符）。
7. **translate**：`vendor/translate_srt.py`，system prompt 来自配置（`--system-file`），支持
   `dsh | minimax | ollama | deepseek | openai-compatible`，可注入 `--video-context` 与 `--series`（术语表名字，词表文件在用户数据目录
   `~/.dsh/data/subtitle-studio/series/<名字>.json`，包内不预置）。
8. **retry**：`vendor/retry_translate.py` 针对「仍是日文且无中文」的条目小批量重翻。
9. **quality-gate**：日文残留比例/条数超过阈值时，用 `prefilter_and_rerun.py` 的重跑模式二次修复。
10. **trim-silence**（`subtitles.trimSilence`）：`vendor/trim_silence.py` 用音频能量收缩每条字幕的起止。
11. **bilingual**（`subtitles.bilingual`）：生成 `source.zh.bilingual.srt` + `source.zh.ass`
    （中文在上较大、原文在下较小，`bilingual_ass.py` 按分辨率选字号与字体，样式取 `subtitles.styleTemplate`）。
12. **产出**（`output.mode`）：`burn` = 双语走 `ass=` 滤镜、单语走 `subtitles=`，压制后上传 mp4；
    `srt` = 不压制，只产出字幕文件并上传（文件名按 `output.nameTemplate` 换扩展名）。
    兼容：配置里没有 `output.mode` 时按旧的 `burn.enabled` 回落。
    压制参数：`libx264 -crf/-preset`
    或 `h264_videotoolbox`，音频 `copy` / `aac` / `opus`（libopus 192k；Opus 装进 .mp4 兼容性差，配 `.mkv`/`.webm` 模板），`+faststart`，并对输出时长做 ≥95% 的合理性校验。
13. **upload**：按 `output.nameTemplate`（必须含 `{stem}`）命名并原子上传。

单片失败只影响该片：`run` 会继续后续候选，但遇到 `3/4/8`（依赖/来源/磁盘）立即中止剩余候选；
退出码取第一个失败结果的码。通知在 `notify.onStart/onSuccess/onFailure` 控制下发送（未配置通知脚本或凭据则跳过并记日志）。

## 8. 引擎适配

| kind | 翻译实现 | 备注 |
|---|---|---|
| `dsh` | DSH 宿主中转（`ctx.llm`） | 凭据与模型都由 DSH 配置持有，引擎不接触密钥；需要宿主可达 |
| `minimax` | `mmx text chat` | 需 `mmx auth status` 已认证；key 通过 env/`apiKeyRef` 注入 |
| `ollama` | HTTP `/api/chat` | `engine.ollamaHost` / `SUBTITLE_OLLAMA_HOST` |
| `deepseek` / `openai-compatible` | OpenAI 兼容 `/chat/completions` | `engine.baseUrl` 或 `SUBTITLE_BASE_URL`，默认 `https://api.deepseek.com` |

## 9. vendor 清单与改造点

以下子步骤脚本在通用化改造后随包分发：

| 文件 | 改造 |
|---|---|
| `translate_srt.py` | 新增 `--system-file`（覆盖内置提示词）；`--series` 去固定枚举；新增 `dsh` 中转分支与 deepseek/openai-compatible HTTP 分支；key 依次读 `SUBTITLE_API_KEY`/厂商 env/keychain |
| `prefilter_and_rerun.py` | 删掉历史项目绝对路径（`SRT_TRANSLATOR_DIR`、`config/env`）；改为自相对路径 + env/keychain；新增 `--engine` 的 openai-compatible 分支；ollama URL 可配置 |
| `transcribe_srt.py` | MLX 模型名改由 `SUBTITLE_WHISPER_MODEL` 注入（默认仍是 `mlx-community/whisper-large-v3-turbo`）；新增反幻觉参数（承接上文开关、温度阶梯、幻觉静音阈值、置信度过滤）、致谢片尾语黑名单、复读折叠 |
| `transcribe_batch.py` | 批量转写、静音占位、模型只加载一次；透传反幻觉参数 |
| `retry_translate.py` / `trim_silence.py` | 原样保留 |
| `bilingual_ass.py` | Style 行改由 `lib/style_templates.py` 的预设生成（`--style`），原先硬编码的默认样式逐字不变 |
| `series_context.py` | 可选术语表加载器：按 `prompts.profiles[].series` 的名字读用户词表目录里的 JSON；包内不预置任何作品词表 |

提示词优先级：`profiles[]` 按声明顺序首个 glob 命中者（同时匹配文件名与完整路径，忽略大小写），
命中项的 `system` 覆盖默认、`context` 与默认 `context` 合并；未命中则用 `prompts.default`。

## 10. 自检方式

引擎自带 `doctor` 与 `selftest` 两条验证路径，都不依赖任何特定机器：

| 项 | 命令 | 期望 |
|---|---|---|
| 环境体检 | `subtitle-run doctor --config <config> --json` | exit 0 / verdict green：python、ffmpeg（ass/subtitles/testsrc2/sine + libx264 + aac）、whisper 引擎与模型、翻译引擎、来源/输出端点可写、磁盘余量、`dataDir` 可写 |
| 选片预演 | `subtitle-run plan --config <config> --json` | 列出候选与跳过原因（`translated-exists` / `quota-limit` 等），不写任何状态 |
| 空跑 | `subtitle-run run --config <config> --dry-run --json` | exit 0；只打印阶段计划，不创建工作目录与状态文件 |
| 自检片 | `subtitle-run selftest --config <config> --json` | exit 0；生成 5s 测试片并烧入中文字幕，`translate.hasChinese=true`，ffprobe 时长与编码符合预期 |
| 幂等 | 再次 `run` | 已译制的片子被 `translated-exists` 跳过，选中 0 条，exit 0 |
| 退出码 | 活锁/死锁/坏配置/不可达 | `9`（含持有者 pid）、死锁自动回收后正常，`2`、`4` 均符合契约 |
| 密钥扫描 | `grep -E "sk-[A-Za-z0-9_-]{8,}|tvly-"` 扫配置/日志/引擎 | 无命中 |

自检片用 `say` 之类的本机语音合成器也能造出一段真实语音；任何真实片源都不是验证的前提。

## 11. 已知限制

- **未在本机跑真实的 4 GB NAS 片源**（下载 + 转写 + 翻译 + 上传耗时以小时计，且会写入生产 NAS）。
  全链路已用合成日语音视频在同一条代码路径上验证（含 whisper 真实转写与 mmx 真实翻译）。
- `prefilter_and_rerun.py` 的广告词表是通用示例，`[广告宣番]` 之类的占位规则在非广告场景可能过度命中——
  属提示词/规则层面，可按需替换 vendor 文件或改用自定义 system prompt。
- `trim_silence` 依赖音频能量，音乐/掌声段可能把字幕裁剪得偏短；可用 `subtitles.trimSilence=false` 关闭。
- 远端 `find -printf` 依赖 Synology busybox 行为；不支持时退回 `stat` 逐条查询（慢但可用），两者都不可用则 exit 4。
- 通知实现 `qqbot`（官方 API 直连，或回退到用户自备的 `notify.qqSendPath` 脚本），其他渠道 `notify.enabled` 无效。
- `schedule` 段引擎只读不写：launchd/systemd 的安装由插件侧负责。`schedule.time` 是主触发点，
  `schedule.guardTimes`（可选 `["HH:MM", ...]`，最多 11 个）是额外触发点，launchd 模式下也会一起写进 plist；`guard` 幂等，多触发点只会补跑
  当天没跑成的情况。`guardTimes` 的格式与去重由引擎校验，非法时 exit 2。
- 单实例锁是**文件 + pid 存活检测**，跨机器不同步（同一 NAS 被两台机器同时跑需人工避免）。

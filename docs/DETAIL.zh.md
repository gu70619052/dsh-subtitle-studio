# 字幕工坊（Subtitle Studio）

把「挑片 → 转写 → 翻译 → 压制 → 落盘 → 通知」做成一条可配置的本地流水线。所有执行都交给包内的
`engine/subtitle-run`（Python 3 + ffmpeg，零 Node 依赖），插件本身只负责：**配置、调度、把 CLI 的 JSON
结果搬进设置页**。

## 它做什么

| 步骤 | 内容 | 由谁执行 |
| --- | --- | --- |
| 挑片 | 按体积/时长/年龄/已译状态/排除规则从来源目录里挑出本轮要处理的片子 | 引擎 `plan` |
| 转写 | 用 whisper（`mlx_whisper`，macOS Metal 加速）生成原文字幕 | 引擎 |
| 翻译 | 按提示词 profile 调 LLM（DSH 宿主模型 / MiniMax / Ollama / DeepSeek / 任意 OpenAI 兼容端点） | 引擎 |
| 压制 | ffmpeg 烧字幕（可选双语 ASS）、抽音轨、清音 | 引擎 |
| 落盘 | 按 `nameTemplate` 命名，写本地目录或 SSH 远端 | 引擎 |
| 通知 | QQ Bot / 自填命令 / Webhook / 关闭，四条通道任选 | 引擎 |

插件提供的是**一个设置页分区**（左栏「字幕工坊」，order 50），分区内部 8 个 tab；另有一套受限的
HTTP 桥供页面读写配置、跑引擎、看日志。

## 数据放在哪

| 路径 | 内容 |
| --- | --- |
| `~/.dsh/data/subtitle-studio/config.json` | 唯一的配置真相，权限 `0600` |
| `~/.dsh/data/subtitle-studio/logs/run.log` | 引擎运行日志（按天轮转，5 MiB 自截断） |
| `~/.dsh/data/subtitle-studio/logs/doctor.log` | 体检日志 |
| `~/.dsh/data/subtitle-studio/logs/host.log` | 插件自己记的桥层错误（写盘失败、launchd 失败等） |
| `~/.dsh/data/subtitle-studio/state/` | 运行锁 `run.lock`、插件最后一次运行摘要、launchd 装载记录、引擎配置视图 |
| `~/.dsh/data/subtitle-studio/work/` | 转写/翻译中间产物 |

## 8 个 tab

| tab | 能做的事 |
| --- | --- |
| 概览 | 一眼看配置状态（文件是否存在、权限、是否可解析）、引擎是否就绪、调度当前状态与下次运行时间、上次运行摘要；三个动作按钮：`立即运行`（可勾 `只试跑不落盘`、限定本轮最多几条）、`只选片`（只跑 `plan`，不烧字幕）、`自检`（跑 `selftest` 造一个真 mp4）。运行中显示 runId 与进度，可取消。 |
| 引擎 | **一个模型下拉，不用填别的。** 你在 DeepSeek Harness 里配的模型，和本机 Ollama 报上来的模型（`GET /api/tags`，进 tab 自动刷、也可手动刷），平级放在同一个列表里——模型就是模型。选一个就同时写好 `engine.kind`（云端模型是 `DSH`，本地是 `Ollama`）、`engine.provider` 和 `engine.model`。只有选了 Ollama 模型才会出现它唯一需要的地址：本机地址直接访问，装在别的机器上（例如 NAS）的地址要先保存。`测试模型` 做一次只出一个 token 的往返：DSH 模型走宿主中转，Ollama 模型直连 `/api/generate`，**试本地模型不烧云端额度**。末尾「手动填写 / 其他引擎…」用于填尚未列出的模型名或换用别的引擎，选中后出现自由文本框和「高级」行：引擎类型、提供方、接口地址、钥匙串条目**名字**（只存名字，不存明文），对应 MiniMax / DeepSeek / OpenAI 兼容直连各自后端。温度、seed、maxTokens、批量条数对所有引擎都常驻；DSH 中转状态块只在你选了 DSH 模型时出现。 |
| 来源与输出 | 开头是**输出方式**：`压制进视频` 把字幕烧回画面，`只生成字幕文件` 不动画面、只交付一个 `.srt`（名字沿用命名模板，扩展名换成 srt）。后面是多目录翻译的目录列表：每一行把一个来源（方式本地 / SSH、目录路径本地带内置目录浏览器 SSH 手填、`include` 通配、是否递归）与一个输出（方式、目录、命名模板 `{stem}`、是否覆盖、SSH 的 host/port/user/keyPath）配成一对，可任意增删。`selection.maxPerRun` 是所有行共享的单轮上限，靠前的行先吃掉配额。列表留空即回到经典的单来源 / 单输出模式——此时下面这两个字段就是实际生效的路径，它们同时也是新增行时的预填默认值。 |
| 选片 | 每轮上限、跳过已译、体积上下限、最大年龄（天）、排序、排除通配。选片结果表显示候选、被选中的项、以及跳过原因。 |
| 提示词 | 默认 system / context，以及 profile 列表（名称、匹配通配、system、context、系列名）；支持新增、删除、上移/下移。 |
| 画质音频 | 双语字幕、裁剪静音、每行最多字数、保留原文字幕为 srt、**字幕样式预设**（五种现成样式，不用手写 ASS Style）；压制开关、ffmpeg 路径、CRF、preset、音轨策略（copy / aac / opus）、字号、转写模型、语言、是否保留中间音频；还有一张**转写质量与反幻觉**卡片（承接上文（默认关）、温度回退上限、静音阈值、置信度下限 / 无语音上限、是否保留致谢片尾语与重复行）。面板只显示当前配置下引擎真正会读的字段：不压制时编码相关项收起，双语压制用 ASS 字号顶掉 SRT 字号，转写引擎也在这里选（Apple Silicon 用 mlx_whisper，Intel 机器选 faster-whisper），通知开关要选好渠道才出现。 |
| 调度 | `launchd`（写入 LaunchAgent 并按 `HH:MM` 定时）、`dsh`（只给出可粘贴的 prompt 文本，不替你建任务）、`关闭`（卸载 LaunchAgent）；展示 plist 路径、装载状态、下次运行。 |
| 诊断 | 跑 `doctor`，用表格列出每项检查（通过 / 警告 / 失败）、说明与修复建议；失败项红字；同时显示引擎 `status`（锁、今日处理数、日志大小）。 |

底部固定：`保存`、`还原`、`重新读取`，以及当前配置路径与校验问题数。保存失败会逐字段标红且**不落盘**。

## 已译台账与「自家成品」

plan 对「这条以前处理过」有两套彼此独立的判定，都会出现在 `reason` 列里。

**`translated-ledger`** —— `state/translated.json` 为每个产出记一条
`{key, output, mode, at, sourceDir}`；`key` 是来源文件名，多目录时带目录标签（与
`state/skipped.json` 同一套标签，所以调整目录顺序不会让旧记录失效）。台账在「翻输出目录」
之前先查，这正是「换输出目录、或从压制切到只出字幕」不再整库重译的原因。

记录只作线索、不作结论：`verify_ledger_hits()` 按成品所在目录分组，每组只列一次目录
（最多 8 个，超出的那些不去看），返回 `(核实通过, 未核实条数)`。只有**真去看过的目录**才
有否决权：成品确实不在的记录被丢掉、这部会重译——多花一轮额度，比静默跳过一部用户删掉又
重压的片子便宜。而**看不到的目录**（连不上、列目录抛错、超过 8 个没去看）保留记录并计入
`未核实`：NAS 掉线一晚不该换来整库重译。所以台账是线索不是保证——成品真没了就去
`state/translated.json` 删掉那一条。plan 里每目录报
`dirs[].ledger = {hits, missing, unchecked}`，日志也会写明几条没核实。
`translated-exists`（期望文件名就在那儿）作为第二条独立信号保留；两种 reason 都会带上
`translatedEvidence`：真正被核对过的那个绝对路径。

台账上限 `TRANSLATED_MAX = 5000` 条，按 `at` 淘汰最旧；文件以 `0600` 原子写入，解析失败
时降级成空台账，不会让整轮挂掉。

**`own-output`** —— 来源与输出是同一个目录时，`X_zh.mp4` 会被再选一次、写成
`X_zh_zh.mp4`，而覆盖策略拦不住（名字确实是新的）。判为自家成品的条件有两条：
(a) 它等于同一批里另一个文件的期望成品名；(b) 它匹配 `nameTemplate` 的反向规则且就在
输出目录里。反向匹配器每次 plan 只构造一次（`template_matcher`），把
`{stem}` / `{name}` / `{ext}` 当通配，且字面部分不足两个字符时拒绝构造——否则 `{name}`
或 `{stem}.{ext}` 会把整个来源目录判成自己的成品。模板里写 `{ext}` 时扩展名仍会补成通配，
所以 `{stem}_zh.{ext}` 能同时匹配 `X_zh.mp4` 与 `X_zh.srt`；模板里出现代码不认识的占位符
（如 `{title}`）时拒绝构造匹配器，并且保存阶段就被 `config.validate` 拦下
（`_template_renders`：只支持 `{stem}` / `{name}` / `{ext}`），而不是等到选片时每个文件炸一次。
`selection.skipOwnOutputs: false` 可以关掉这道判定，留给来源文件名真的长成这样的情形；
而**点名要处理**（`--only 剧场版_zh.mp4`，或强制重跑）一定让路——这条规则是用来防误操作的，
不是用来拒绝用户亲手打的请求的。

## 配置读写规则

- **缺失键补默认**：describe 返回的永远是「默认值 + 文件内容 + 未知键」合并后的完整视图。
- **未知键保留**：文件里插件不认识的键（比如你手写的注释性字段、或未来版本新增的键）在保存时原样写回。
- **原子写**：临时文件 + `fsync` + `rename`，权限 `0600`；写失败时旧文件保持不变。
- **文件损坏**：现有 `config.json` 不是合法 JSON 时，保存直接返回 `409`，不覆盖、不猜。
- **还原（reset）**：写默认配置，旧文件备份成 `config.json.bak`，页面上显示备份路径。
- **明文密钥**：`engine.apiKeyRef` 只接受「引用名」，填成疑似真实密钥（长串无空格的 token 形态）会被校验拒绝。

## 引擎契约

插件通过子进程调用包内 `engine/subtitle-run`：

```bash
engine/subtitle-run doctor   --config <cfg> [--json]
engine/subtitle-run plan     --config <cfg> [--max N] [--only "<文件名>"] [--json]
engine/subtitle-run run      --config <cfg> [--max N] [--only] [--dry-run] [--json]
engine/subtitle-run guard    --config <cfg> [--max N] [--json]
engine/subtitle-run status   --config <cfg> [--json]
engine/subtitle-run selftest --config <cfg> [--skip-translate] [--keep] [--json]
```

输出统一是 JSON 信封：`{"ok":true,"data":{...}}` 或 `{"ok":false,"error":{"code","message"}}`。

| 退出码 | 含义 | 页面上的表现 |
| --- | --- | --- |
| 0 | 成功（含「本轮没有候选」） | 正常结果 |
| 2 | 配置错误 | 提示检查字段，诊断 tab 会点名 |
| 3 | 缺依赖（ffmpeg / whisper / 滤镜） | 红字 + `ffmpeg-full` / `mlx-whisper` 安装建议 |
| 4 | 来源不可达（目录不存在、SSH 连不上） | 红字 + 检查路径/SSH |
| 5 | LLM 调用失败 | 红字 + 检查端点与密钥 |
| 6 | ffmpeg 失败 | 红字 + 看 run.log |
| 7 | 远端写入失败 | 红字 + 检查 SSH 权限 |
| 8 | 磁盘空间不足 | 红字 |
| 9 | 已有实例在跑（单实例锁） | 页面拒绝第二次运行 |

**`schedule.mode` 的适配**：引擎的配置 schema 只认 `launchd` / `none`（调度不是它的职责），而面板提供
`launchd` / `dsh` / `关闭`。当保存的是后两种时，插件会在 `state/engine-config.json` 写一份
`mode:"none"` 的规范化副本再交给引擎，你的 `config.json` 保持原样。

## 引擎位置与依赖自检

- **引擎默认用包内自带的那份**（`<包>/engine/subtitle-run`）。设置页「引擎」tab 会同时给出两个路径：
  `插件自带引擎`（永远指向包内）和当前生效的 `引擎路径`，并标注来源是 `内置（插件自带）` 还是 `自定义`。
- 想改用自己的构建：在 `引擎路径覆盖`（`paths.engine`）填绝对路径；「恢复内置」把它清空即可回落。
  这一项也是**未来把包分享给别人**时的关键——别人的机器上路径不同，留空用自带引擎即可零配置。
- **装好即自检**：插件启用后 2 秒内自动跑一次 `doctor`，结果缓存在
  `~/.dsh/data/subtitle-studio/state/selfcheck.json`（6 小时 TTL），概览 tab 的「依赖自检」卡片直接显示
  结论、检查项计数与告警/失败明细；随时可点「重新自检」强制刷新。
- 依赖自检覆盖：`ffmpeg` 及其 `ass`/`subtitles`/`libx264`/`aac` 滤镜、`mlx_whisper`（或 `faster_whisper`）、
  所选引擎类型需要的命令行工具、每一个已配置目录的来源与输出端点（含 SSH 可达性）、`DSH` 引擎的中转链路、
  磁盘余量与状态目录权限。缺依赖会在概览里直接红字列出，不用等到半夜真跑才发现。

## 翻译中转（引擎类型 `DSH`）

引擎是独立的 Python 进程，无法直接调用宿主里的模型接口，所以 `DSH` 类型走「中转」：

1. 插件启用时把 `{baseUrl, tokenPath, bridgePath}` 写进 `state/bridge.json`，共享令牌单独放在 `state/bridge-token`。
   两个文件都是 `0600`，且都不在 `config.json` 里——面板读不回来，发布出去的包里也不会有。
2. 引擎被启动时通过环境变量 `SUBTITLE_STUDIO_BRIDGE` 拿到这个文件的路径（launchd 的 plist 里同样注入）。
   每次翻译调用都重新读一次，所以宿主换端口重启也不需要重新配置。
3. `POST /api/dsh-subtitle-studio/llm` 要求请求头 `X-Subtitle-Studio-Token` 携带令牌（常量时间比较），
   `Content-Type` 必须是 `application/json`，请求体上限 4 MB（超出返回 `413`）；`maxTokens` 夹到 16 384、
   `temperature` 夹到 0–2，然后用 `engine.provider` / `engine.model` 调 `ctx.llm.stream()` 返回拼好的文本。
   模型流没有给出结束原因、或超过 240 秒没结果，都算失败；provider 侧报错以 `502` 返回原始信息。
4. 中转有两道预算：同时在飞最多 2 个（超出 `429 busy`）、每分钟最多 120 次（超出 `429 rate-limited`）。
   可用 `SUBTITLE_STUDIO_RELAY_MAX_TOKENS` / `SUBTITLE_STUDIO_RELAY_CONCURRENCY` /
   `SUBTITLE_STUDIO_SPEND_LIMIT` 覆盖，且每次请求现读，改完不用重启宿主。
5. `POST /api/dsh-subtitle-studio/model-test` 是面板上「测试模型」按钮用的：不需要令牌，但同样受 Host 栅栏和
   每分钟预算约束，`maxTokens` 夹到 256，因此也不会变成免费生成入口。

令牌能挡住**别的用户**的进程，挡不住以同一用户身份运行的代码——所以这条路由是「有界」而不是「可信」。
引擎侧对称：端点地址不是回环就拒发字幕正文，启动子进程时只透传白名单环境变量，不再继承宿主全量 `process.env`。

因为凭据始终由宿主持有，`DSH` 类型不需要密钥引用名，自检也不会就这件事报警。

## 多目录翻译

`dirs` 是一个可选的「来源→输出」配对列表，每项形如
`{name, source:{kind,path,ssh,include,recursive}, output:{kind,path,ssh,nameTemplate,overwrite}}`；
条目里没填的字段回落到顶层 `source` / `output` 的默认值——所以每行只填两个路径就够用。

- **`dirs` 为空就是原来的单目录模式**，已有配置不需要迁移，plan 载荷里 `multiDir:false`，
  `source` / `output` 与从前完全一致。
- **选片是全局的**：逐个目录列举并过滤，候选合并后再按 `selection.order` 统一排序；
  `selection.maxPerRun` 是所有目录共享的一个上限，靠前的行先吃掉配额。想各目录均衡，就设 0 或分轮跑。
- **每对目录彼此隔离**：成品命名用该条目的 `nameTemplate`；中间产物目录用来源路径做了盐，
  不同目录里的同名文件不会共用转写结果；失败/跳过名单也按目录加前缀——但历史上没有前缀的旧条目
  仍然对所有目录生效，不会静默失效。
- **某个来源掉线不拖垮整轮**：不可达的来源会被跳过并记入 `unreachableDirs`；
  只有当**所有**来源都不可达时，本轮才以退出码 `4` 失败，与单目录行为一致。
- 两行写进同一个输出目录是允许的，但会给出 warn：同名文件会互相覆盖。

## 通知

| 渠道 | 需要的字段 | 说明 |
| --- | --- | --- |
| `qqbot` | `notify.qqAppId`、`notify.qqOpenId`（必填）、`notify.qqSecretService`（默认 `subtitle-studio-qqbot`） | 直连 QQ Bot 官方 API：用 appId + clientSecret 换 access_token，再推给 openid 对应的用户。**clientSecret 只存钥匙串**（或 `SUBTITLE_QQ_SECRET` 环境变量），永不写进 `config.json`；也可回退到 `notify.qqSendPath` 指定的自定义脚本 |
| `command` | `notify.command`（必填） | 任意命令；正文同时从 **stdin** 与 `$SUBTITLE_NOTIFY_TEXT` 给出，命令里可用 `{text}` / `{title}` 占位（按 shell 规则转义） |
| `webhook` | `notify.webhookUrl`（必填，http(s)）、`notify.webhookBody` | 默认请求体 `{"text": "{text}"}`，`{text}` 会按 JSON 转义后填入 |
| `none` | — | 不通知 |

保存时前后端都校验：`channel=qqbot` 却没填 appId（也没给回退脚本）、填了 appId 却没填 openid、`channel=command`
却没填命令、`channel=webhook` 却没给 http(s) 地址，都会在对应字段上直接报错，不会写进 `config.json`。

`notify.successWhen` 决定「什么算这一轮成功」：`run`（默认）要求零失败才发成功通知，
保持 1.6 之前的行为；`anyOutput` 只要有一部产出就发成功，失败的那几部另发一条失败通知
——所以 `anyOutput` 下同一轮可能收到两条。既没有产出也没有失败的 `anyOutput` 轮次保持
静默；`run` 轮次仍会照旧发「本轮无新片」。前后端用同一组取值校验。

`openid` 是**接收人维度**的：用你的 QQ 私聊一次该机器人，再从 QQ 开放平台的事件里复制（通知卡里有个小工具可以
直接从收到的消息 JSON 里提取）。密钥可以随时用通知卡的「发送测试消息」验证，缺什么 `doctor` 会直接报 error 而不是
静默跳过。通知失败不会影响流水线结果（引擎侧吞掉异常并写日志）。

## 深浅色

插件不带任何硬编码颜色：13 个颜色全部取自宿主 token（`--dsw-alias-*`），宿主的
`design-platform.css` 对每个 token 定义了两套——浅色在 `body`、深色在 `body[data-ds-dark-theme]`，
所以切主题时面板自动跟随。另外面板注入了作用域样式：键盘焦点环改用主色描边（不再 `outline: none`），
原生控件声明 `color-scheme: light dark`，深色下的下拉框与滚动条也一致。

## 调度

- **launchd**：写 `~/Library/LaunchAgents/<label>.plist`，`ProgramArguments` = 引擎 CLI `guard --config <cfg>`，
  按 `schedule.time` 生成 `StartCalendarInterval`（`HH:MM`），然后 `launchctl bootout` + `bootstrap gui/<uid>`。
  写 plist 前先 `plutil -lint`；bootstrap 失败会把旧 plist 原样写回并报错。重复保存同样的调度是幂等的（返回
  `unchanged`）；改 label 会把旧 agent 卸载掉。
- **dsh**：只返回一段可粘贴的 prompt 文本。DSH 的定时是 prompt 投递型，每次触发都烧模型，是否真的要建由你决定。
  每个触发点一段：`schedule.time`（主触发）加上 `schedule.guardTimes`（额外触发点，最多 11 个）。launchd 模式下
  这组时间同样写进 plist 的 `StartCalendarInterval`（数组形式），漏跑的那晚会被后面的点补上。所有提示词都调
  同一个幂等的 `guard`，所以多出来的点只会补跑当天没跑成的情况，不会重复处理同一天。
- **关闭**：卸载 launchd agent 并删 plist。

## 安全模型

- 所有桥接路由前面都有一道 **Host 栅栏**（照搬宿主本地 API 桥的做法）：`Host` 必须是回环名字，或者等于插件
  装载时发布出去的那个 authority；`sec-fetch-site: cross-site` 直接拒；带了 `Origin` 就必须与它一致；
  `Origin: null` 这种不透明来源也拒——全部返回 `403`。这一条挡的是 DNS rebinding：攻击者先用
  `http://evil.example:8123` 让浏览器拿到页面，再把域名解析翻到 `127.0.0.1`，此时只比对「Origin 的 host 等于
  Host」会双双通过，从而能写 `notify.command`（下次 run 就是执行任意命令）；有了 Host 栅栏就进不来。
- 没有任何路由执行 shell。路径参数只有两个用途：目录枚举、作为 `--config` / 引擎参数的一部分。
- 目录枚举只读、按 realpath 规范化、单次最多 500 项、8 秒超时，并拒绝 `/System`、`/etc`、`/private/var/db`、
  `~/.ssh`、`~/Library/Keychains` 等系统与凭据目录。
- 日志文件按白名单读取（`logs/` 下的 `*.log`，realpath 必须仍在 `logs/` 内），名字不匹配就是 `404`。
- 请求体必须是 JSON：`Content-Type` 不对直接拒（`415`），超出上限干净地返回 `413` 而不是掐断连接。
- `state/` 目录建成 `0700`，`bridge-token` 与 `bridge.json` 是 `0600`；这两个值不会被 `describe` 回传、不会写进
  `config.json`、也不会出现在日志里。

## 日志查看

`logs` 接口读 `~/.dsh/data/subtitle-studio/logs/<name>.log`，默认 200 行、最多 2000 行；文件还没生成时，
退回到插件内存里捕获的当前/上一次任务的输出（只对 `run` / `selftest` 生效，其它名字直接 404）。

## 排障

| 现象 | 处理 |
| --- | --- |
| 诊断里 `ffmpeg.filters` 失败 | 本机没有能烧字幕的 ffmpeg。Homebrew 的普通 `ffmpeg` 不带 libass，带的那个 `ffmpeg-full` 是 keg-only（不进 `PATH`）；插件已会自动找 `PATH`、`brew --prefix ffmpeg-full` 和两个常见前缀，还报这条就是三处都没有滤镜 —— 装 `ffmpeg-full`，或把 `burn.ffmpeg` 指到带该滤镜的二进制 |
| `mlx_whisper` 警告 | `pip3 install --user mlx-whisper`；不用 Metal 可忽略，改用 CPU whisper |
| Ollama 连不上 | 起 `ollama serve`，或把 `engine.ollamaHost` 指向实际地址 |
| 点了运行没反应 | 看诊断里的锁状态；退出码 9 = 上一轮还在跑 |
| 保存报 409 | 现有 `config.json` 不是合法 JSON，先手工修好或点「还原」 |
| launchd 没触发 | `launchctl print gui/$(id -u)/<label>`；plist 里的程序路径是包内绝对路径，移动安装位置后要重新保存一次调度 |

## 术语表

`prompts.profiles[].series` 填的是词表名字，对应
`~/.dsh/data/subtitle-studio/series/<名字>.json`（目录可用 `SUBTITLE_STUDIO_SERIES_DIR`
覆盖）。翻译时引擎读它，渲染成「专有名词表 + ASR 错听 → 正确写法」拼进提示词，并把
`postFix` 对照用于译文后处理。

发布包里不含任何具体作品的词表，只有加载器（`engine/vendor/series_context.py`）和一份
schema 示例（`engine/vendor/series.example.json`）。词表文件不存在或读坏时，引擎打一行
提示并按无词表继续翻译——不会因为词表问题让整次运行失败。

## 输出方式、时间轴对齐与转写质量

**输出方式**（`output.mode`）决定一轮跑完产出什么。

- `burn` —— 把译文压回画面，交付压制好的视频（插件一直以来的行为）。
- `srt` —— 画面完全不动，只交付字幕文件。文件名沿用同一套 `{stem}` 模板，只把扩展名换成 `.srt`，
  所以 `ep01_zh.mp4` 对应 `ep01_zh.srt`。
- **老配置照常能用**：这个开关出现之前，配置里只有 `burn.enabled`。面板会按它推导输出方式，保存时两个键同步写；
  引擎侧同样有回落，手工改过的 config.json 也不会翻车。
- 「已翻译则跳过」的判定用的就是该模式下真实产出的文件名，所以切到「只生成字幕文件」后，流水线不会把
  自己以为做完的文件重跑一遍。
- **双语跟着输出方式走**：开着双语时，「只生成字幕文件」交付的就是双语那份（译文在上、原文在下），而不是
  只交单语译文——你要的是哪种，拿到的就是哪种。

**时间轴对齐。** 有些片源的音轨比画面晚两三秒开始（广播录制、二次封装常见）。ffmpeg 抽音频时会把这段容器里的
空隙丢掉，字幕于是整体偏早——差多少就是多少。引擎改成实测而不是猜：用 ffprobe 量出音频轨与视频轨的起始时间、
音轨时长与抽出来的音频时长，算出「容器里有、但抽音频时丢了」的那段静音，把它补到音频开头，字幕就对上了。
小于 0.2 秒的差值不处理（对字幕无感）；算出来是负数只记录、不「修正」。

**转写质量**（反幻觉那张卡）针对长音频里 Whisper 常见的毛病：

- *致谢片尾语* —— 「ご視聴ありがとうございました」「字幕制作…」「thanks for watching」这类句子会被丢掉，
  可以用开关保留。
- *重复行* —— 一段里同一个词组重复三次以上会压回一份；真的是内容重复（副歌、喊麦）不动，除非与上一条完全相同，
  那就把时间区间合并。
- *低置信度* —— 平均对数概率低于下限、或无语音概率高于上限的块会被丢弃。没有置信度字段的片段不会被这条规则误伤。
- *承接上文* —— 默认关闭。让模型承接上一句，正是它「续写」没听到的句子的原因；是否开启按轮次决定。
- *温度回退* —— 回退上限为 `0` 时只走一次「最可能」的结果；调高之后，低置信度的片段会按递增的温度阶梯重新解码。

**字幕样式预设**把手写 ASS Style 换成五种现成样式：白字黑边（默认，与插件原先硬编码的样式行逐字一致）、
醒目黄字、顶部白字（纪录片）、黑底白字、影院黑条白字。一个预设同时作用于两条路径：双语字幕用的 ASS 文件，
以及压制纯文本字幕时用的 `force_style` 串。

## 思考档位与「一批被截断」之后发生什么

`engine.reasoningEffort` 只对「先想再答」的模型有意义。它的取值是模型自己声明的
不透明标识符——面板去向宿主查询（`GET model-info?provider=…&model=…`，内部调
`resolveModelInfo`），只把声明出来的档位列出来，外加「跟随模型默认」。留空表示
**这个键根本不发**（宿主自己的 agent 也是这样表达的），所以面板不会凭空造出
`low` / `medium` / `high`。

真正要紧的失败形态是「思考把输出预算吃光，一批在 `maxTokens` 处被切断」。三处改动
让一轮运行不再悄悄丢掉它：

1. 中转回 `200` + `truncated: true` + 已写出的正文 + `usage: {inputTokens,
   outputTokens, reasoningTokens}`。如果回 `502`，引擎会把整批丢掉，而其中大部分
   其实已经写好了。
2. 翻译器把「token 上限」当作**这批的确定性结论**：拿同样的预算再问一遍只会在
   同一个地方切断，问三次就烧三次额度。能对上的块按起始时间戳收下（不按位置——
   模型会自己重排、也会漏条），其余的对半拆小重问，深度上限 `SPLIT_MAX_DEPTH=3`、
   次数上限 `SPLIT_MAX_CALLS=24`。
3. 仍然拿不到的块保留原文并记账。每轮在字幕成品旁边写一份 `<output>.report.json`：

   ```json
   {"blocks": 118, "untranslated": 4, "untranslatedIds": [{"idx": 7, "start": "00:04:12,480", "reason": "token-limit"}],
    "calls": {"primary": 4, "split": 3}, "truncatedBatches": 1, "failedBatches": 0,
    "tokens": {"input": 18234, "output": 9210, "reasoning": 3106}, "reasoningEffort": "low"}
   ```

   `translation_stats()` 会把这份账本并进运行结果，完成通知里追加未译条目，于是
   「翻译质量偏低」不再只是一个百分比，而是一张能照着处理的清单。

还有两种失败形态有了各自的答案，不再是一句笼统的「出错了」：

- **模型没有这个档位。** 中转先把请求的档位和模型声明过的档位对一遍，直接回
  `422 unknown-effort`，一分钱不花；provider 在流里拒绝档位，也会被认出来、同样
  这样回答。引擎把它当成这批的确定性结论（再问一次还是同样的拒绝），不再重试，
  也就不会为同一个错误付三次钱。面板在下拉旁边把同样的话显示成警告，并说清
  这个模型到底有哪些档位。
- **provider 在限流。** 额度用光以前会变成一条普通的 `502`，引擎等两秒就再问。
  现在它回 `429 rate-limited`，引擎按长退避重试（20 秒、40 秒、60 秒），而不是
  短短地等一下。

## 已知限制

- `DSH` 引擎的提供方与模型取自你自己的 DSH 配置；尚未出现在 `ctx.llm` 清单里的模型可以用「自定义模型名…」手填，
  插件不会因为名字不在清单里就拒绝——模型的命名空间属于 provider，不属于我们。
- 插件不安装、不启用自己：安装走 `plugin_manager install_bundle`。
- 页面里的目录浏览器只用于本地路径；SSH 路径需要手填。
- `maxPerRun` 是所有目录共享的单轮上限，按列表顺序消耗；靠后的目录在靠前的目录一直有候选时可能轮不到。
- `--only` 与 `selection` 里的 only 按文件名子串匹配，跨目录同名文件会被一起强制处理。
- 调度写的是 `launchd` LaunchAgent，因此自动运行仅支持 macOS；`dsh` 模式只给出可粘贴的提示词文本，不会代你建任务。

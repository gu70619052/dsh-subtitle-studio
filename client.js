/**
 * Client half of dsh-subtitle-studio (字幕工坊 / Subtitle Studio).
 *
 * Registers one section in the settings page and renders the pipeline panel as
 * eight internal tabs (概览 / 引擎 / 来源与输出 / 选片 / 提示词 / 画质音频 / 调度 /
 * 诊断). Every value shown as truth comes from the host's `describe` endpoint, so
 * a page reload always reflects config.json rather than anything cached in the
 * browser. Colours are host theme tokens only, so light and dark both follow.
 */
window.__ModuleLoader__.load({
  id: 'dsh-subtitle-studio',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const NS = 'dsh-subtitle-studio'
    const BRIDGE = `api/${NS}/`

    /* ------------------------------- theme tokens ------------------------------ */

    const T = {
      label: 'var(--dsw-alias-label-primary)',
      secondary: 'var(--dsw-alias-label-secondary)',
      tertiary: 'var(--dsw-alias-label-tertiary)',
      card: 'var(--dsw-alias-card-fill)',
      field: 'var(--dsw-alias-bg-layer-2)',
      border: 'var(--dsw-alias-border-l2)',
      borderStrong: 'var(--dsw-alias-border-l3)',
      error: 'var(--dsw-alias-state-error-primary)',
      warn: 'var(--dsw-alias-state-warn-primary)',
      success: 'var(--dsw-alias-state-success-primary)',
      accent: 'var(--dsw-alias-button-primary-fill)',
      onColor: 'var(--dsw-alias-label-on-color)',
      // 深色主题下 --dsw-alias-button-primary-fill 是近白色，label-on-color 仍是白色，
      // 两者搭配会「白底白字」；前景必须用 foreground 系列（浅色=白，深色=近黑）。
      onAccent: 'var(--dsw-alias-label-primary-foreground)',
      accentHover: 'var(--dsw-alias-button-primary-hover)',
      hover: 'var(--dsw-alias-interactive-bg-hover)',
    }

    const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

    /**
     * Inline styles cannot express `:focus-visible`, and `outline: none` without a
     * replacement is the one thing that hurts keyboard users. One scoped sheet
     * fixes focus rings, native control theming (dark dropdowns/scrollbars) and
     * link-like code spans; every value is a host token, so both themes adapt.
     */
    const SCOPED_CSS = `
      [data-dsh-subtitle-studio] :focus-visible {
        outline: 2px solid var(--dsw-alias-button-primary-fill);
        outline-offset: 1px;
      }
      [data-dsh-subtitle-studio] input,
      [data-dsh-subtitle-studio] select,
      [data-dsh-subtitle-studio] textarea {
        color-scheme: light dark;
      }
      /* 主按钮：深色主题下填充是近白色，前景必须跟随 foreground 系列，
         否则 hover/active 时浏览器默认样式会把文字压回白色造成「白底白字」。 */
      [data-dsh-subtitle-studio] .ss-btn-primary {
        background: var(--dsw-alias-button-primary-fill);
        border-color: var(--dsw-alias-button-primary-fill);
        color: var(--dsw-alias-label-primary-foreground);
      }
      [data-dsh-subtitle-studio] .ss-btn-primary:hover:not(:disabled) {
        background: var(--dsw-alias-button-primary-hover);
        border-color: var(--dsw-alias-button-primary-hover);
      }
      [data-dsh-subtitle-studio] .ss-btn-primary:active:not(:disabled) {
        filter: brightness(0.94);
      }
      [data-dsh-subtitle-studio] .ss-btn:not(.ss-btn-primary):hover:not(:disabled) {
        background: var(--dsw-alias-interactive-bg-hover);
      }
      [data-dsh-subtitle-studio] .ss-tab:not(.ss-tab-active):hover {
        background: var(--dsw-alias-interactive-bg-hover);
      }
      [data-dsh-subtitle-studio] .ss-tab-active {
        background: var(--dsw-alias-button-primary-fill);
        color: var(--dsw-alias-label-primary-foreground);
      }
      [data-dsh-subtitle-studio] .ss-btn:disabled {
        cursor: default;
      }
      [data-dsh-subtitle-studio] ::selection {
        background: var(--dsw-alias-interactive-bg-hover);
        color: var(--dsw-alias-label-primary);
      }
    `

    /** Inject the scoped sheet once per document. */
    function ensureScopedStyle() {
      // Headless renderers (tests, SSR probes) have no real DOM; skip silently.
      if (typeof document === 'undefined') return
      if (typeof document.getElementById !== 'function' || typeof document.createElement !== 'function') return
      if (document.getElementById('dsh-subtitle-studio-style') !== null) return
      try {
        const style = document.createElement('style')
        style.id = 'dsh-subtitle-studio-style'
        style.textContent = SCOPED_CSS
        if (document.head && typeof document.head.appendChild === 'function') document.head.appendChild(style)
      } catch {
        /* styling is cosmetic; never break the panel over it */
      }
    }

    const S = {
      root: { display: 'flex', flexDirection: 'column', gap: 14, fontSize: 13, color: T.label, padding: '2px 0 10px', colorScheme: 'light dark' },
      head: { display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' },
      h1: { fontSize: 16, fontWeight: 600, margin: 0, color: T.label },
      sub: { color: T.secondary, fontSize: 12, lineHeight: 1.55 },
      tabs: {
        display: 'flex',
        gap: 6,
        flexWrap: 'wrap',
        borderBottom: `1px solid ${T.border}`,
        paddingBottom: 8,
      },
      card: {
        background: T.card,
        border: `1px solid ${T.border}`,
        borderRadius: 10,
        padding: '12px 14px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      },
      cardTitle: { fontSize: 13, fontWeight: 600, color: T.label, margin: 0 },
      cardDesc: { fontSize: 12, color: T.secondary, lineHeight: 1.5, margin: 0 },
      grid: {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
        gap: '10px 16px',
      },
      labelRow: { display: 'flex', alignItems: 'center', gap: 6, minHeight: 18 },
      label: { fontSize: 12, color: T.secondary, fontWeight: 500 },
      control: {
        width: '100%',
        boxSizing: 'border-box',
        background: T.field,
        color: T.label,
        border: `1px solid ${T.border}`,
        borderRadius: 7,
        padding: '5px 8px',
        fontSize: 12.5,
        font: 'inherit',
        lineHeight: 1.45,
      },
      hint: { fontSize: 11, color: T.tertiary, lineHeight: 1.45 },
      err: { fontSize: 11, color: T.error, lineHeight: 1.45 },
      btn: {
        borderRadius: 7,
        border: `1px solid ${T.borderStrong}`,
        background: 'transparent',
        color: T.label,
        padding: '5px 11px',
        fontSize: 12.5,
        font: 'inherit',
        cursor: 'pointer',
        lineHeight: 1.4,
      },
      btnPrimary: {
        borderRadius: 7,
        border: `1px solid ${T.accent}`,
        background: T.accent,
        color: T.onAccent,
        padding: '5px 12px',
        fontSize: 12.5,
        font: 'inherit',
        cursor: 'pointer',
        lineHeight: 1.4,
        fontWeight: 500,
      },
      btnDisabled: { opacity: 0.5, cursor: 'default' },
      actions: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' },
      chip: (tone) => ({
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        borderRadius: 999,
        padding: '1px 9px',
        fontSize: 11.5,
        border: `1px solid ${tone === 'ok' ? T.success : tone === 'warn' ? T.warn : tone === 'err' ? T.error : T.border}`,
        color: tone === 'ok' ? T.success : tone === 'warn' ? T.warn : tone === 'err' ? T.error : T.secondary,
        background: 'transparent',
        whiteSpace: 'nowrap',
      }),
      tabs: {
        display: 'flex',
        flexWrap: 'wrap',
        gap: 4,
        borderBottom: `1px solid ${T.border}`,
        paddingBottom: 8,
      },
      tab: (active) => ({
        border: 'none',
        borderRadius: 7,
        padding: '5px 11px',
        fontSize: 12.5,
        font: 'inherit',
        lineHeight: 1.4,
        cursor: 'pointer',
        color: active ? T.onAccent : T.secondary,
        background: active ? T.accent : 'transparent',
        fontWeight: active ? 600 : 500,
      }),
      pre: {
        fontFamily: MONO,
        fontSize: 11,
        lineHeight: 1.55,
        background: T.field,
        color: T.label,
        border: `1px solid ${T.border}`,
        borderRadius: 8,
        padding: '10px 12px',
        maxHeight: 320,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
        margin: 0,
      },
      code: { fontFamily: MONO, fontSize: 11.5, color: T.secondary, wordBreak: 'break-all' },
      banner: (tone) => ({
        borderRadius: 8,
        border: `1px solid ${tone === 'err' ? T.error : tone === 'warn' ? T.warn : T.success}`,
        color: tone === 'err' ? T.error : tone === 'warn' ? T.warn : T.success,
        padding: '7px 10px',
        fontSize: 12.5,
        lineHeight: 1.5,
      }),
      table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
      th: {
        textAlign: 'left',
        fontWeight: 600,
        color: T.secondary,
        padding: '4px 8px 4px 0',
        borderBottom: `1px solid ${T.border}`,
        whiteSpace: 'nowrap',
      },
      td: {
        padding: '4px 8px 4px 0',
        borderBottom: `1px solid ${T.border}`,
        verticalAlign: 'top',
        wordBreak: 'break-word',
      },
      picker: {
        border: `1px solid ${T.border}`,
        borderRadius: 8,
        background: T.field,
        padding: 10,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        marginTop: 6,
      },
      pickerList: {
        maxHeight: 220,
        overflow: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
      },
      pickerItem: {
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 8,
        padding: '3px 6px',
        borderRadius: 6,
        cursor: 'pointer',
        fontSize: 12,
        color: T.label,
        background: 'transparent',
        border: 'none',
        font: 'inherit',
        textAlign: 'left',
        width: '100%',
      },
      kv: { display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '4px 12px', fontSize: 12 },
      kvKey: { color: T.secondary, whiteSpace: 'nowrap' },
      kvVal: { color: T.label, wordBreak: 'break-all' },
    }

    /* --------------------------------- strings -------------------------------- */

    /** `key: [zh, en]` — both locales are generated from one table so they never drift. */
    const STRINGS = {
      'nav': ['字幕工坊', 'Subtitle Studio'],
      'title': ['字幕工坊', 'Subtitle Studio'],
      'subtitle': [
        '本地视频 → 转写 → 翻译 → 双语字幕 / 压制的流水线面板。配置保存在 ~/.dsh/data/subtitle-studio/config.json。',
        'Pipeline panel for local videos: transcribe → translate → bilingual subtitles / burn-in. Config lives in ~/.dsh/data/subtitle-studio/config.json.',
      ],

      'tab.overview': ['概览', 'Overview'],
      'tab.engine': ['引擎', 'Engine'],
      'tab.paths': ['来源与输出', 'Source & output'],
      'tab.selection': ['选片', 'Selection'],
      'tab.prompts': ['提示词', 'Prompts'],
      'tab.quality': ['画质音频', 'Quality & audio'],
      'tab.schedule': ['调度', 'Schedule'],
      'tab.diagnose': ['诊断', 'Diagnostics'],

      'act.save': ['保存', 'Save'],
      'act.revert': ['还原', 'Revert'],
      'act.reload': ['重新读取', 'Reload'],
      'act.defaults': ['恢复默认', 'Restore defaults'],
      'act.run': ['立即运行', 'Run now'],
      'act.plan': ['只选片', 'Plan only'],
      'act.selftest': ['自检', 'Self-test'],
      'act.cancel': ['取消运行', 'Cancel run'],
      'act.logs': ['查看日志', 'View logs'],
      'act.doctor': ['诊断', 'Diagnose'],
      'act.browse': ['浏览…', 'Browse…'],
      'act.choose': ['使用此目录', 'Use this folder'],
      'act.up': ['上一级', 'Up'],
      'act.close': ['收起', 'Close'],
      'act.refresh': ['刷新', 'Refresh'],
      'act.addProfile': ['新增提示词配置', 'Add prompt profile'],
      'act.remove': ['删除', 'Remove'],
      'act.moveUp': ['上移', 'Move up'],
      'act.moveDown': ['下移', 'Move down'],
      'act.applySchedule': ['应用调度', 'Apply schedule'],
      'act.removeSchedule': ['卸载调度', 'Remove schedule'],
      'act.copyPrompt': ['复制提示词', 'Copy prompt'],

      'msg.loading': ['正在读取配置…', 'Reading config…'],
      'msg.loadFailed': ['读取失败', 'Could not read'],
      'msg.engineMissing': [
        '引擎尚未就绪：engine/subtitle-run 不存在或不可执行，运行/选片/诊断都会失败。',
        'Engine not ready: engine/subtitle-run is missing or not executable, so run/plan/doctor will fail.',
      ],
      'msg.configMissing': ['配置文件还不存在，保存一次即可创建。', 'No config yet; saving once creates it.'],
      'msg.configMalformed': [
        '配置文件不是合法 JSON，已拒绝覆盖。修正后重试，或点「恢复默认」（旧文件会备份为 config.json.bak）。',
        'config.json is not valid JSON and was not overwritten. Fix it, or restore defaults (the old file is backed up).',
      ],
      'msg.saved': ['已保存', 'Saved'],
      'msg.reverted': ['已还原为服务器上的配置', 'Reverted to the saved config'],
      'msg.reloaded': ['已重新读取', 'Reloaded'],
      'msg.resetDone': ['已恢复默认配置', 'Defaults restored'],
      'msg.runStarted': ['已开始运行', 'Run started'],
      'msg.runBusy': ['已有任务在运行', 'A job is already running'],
      'msg.planDone': ['选片完成', 'Selection complete'],
      'msg.doctorDone': ['诊断完成', 'Diagnostics complete'],
      'msg.noIssues': ['没有问题', 'No issues'],
      'msg.savedWithWarnings': ['已保存，但有提醒', 'Saved, with warnings'],
      'msg.localErrors': ['本地校验未通过，修正后再保存', 'Fix the highlighted fields before saving'],
      'msg.savedAt': ['保存于', 'Saved at'],
      'msg.copyFailed': ['复制失败，请手动选择文本', 'Copy failed, select the text manually'],
      'msg.copied': ['已复制', 'Copied'],
      'msg.unsaved': ['有未保存的修改', 'Unsaved changes'],
      'msg.applyNoChanges': ['调度未变化', 'Schedule unchanged'],

      'ov.status': ['环境状态', 'Environment'],
      'ov.run': ['运行', 'Run'],
      'ov.schedule': ['调度', 'Schedule'],
      'ov.maintenance': ['维护', 'Maintenance'],
      'ov.enginePath': ['引擎', 'Engine'],
      'ov.configPath': ['配置', 'Config'],
      'ov.models': ['模型清单', 'Model catalog'],
      'g.dirs': ['目录列表', 'Directories'],
  'f.dirs': ['目录列表', 'Directory list'],
  'g.dirsHint': [
    '为不同片库配置多组「来源 + 输出」；留空时沿用下方单目录设置。每轮 maxPerRun 是所有目录合计的上限。',
    'Configure several source/output pairs; leave empty to use the single-directory settings below. maxPerRun is the combined limit for all directories.',
  ],
  'act.addDir': ['添加目录', 'Add directory'],
  'act.removeDir': ['删除', 'Remove'],
  'f.dir.name': ['目录名称', 'Directory name'],
  'f.dir.source': ['来源', 'Source'],
  'f.dir.output': ['输出', 'Output'],
  'f.dir.include': ['匹配规则', 'Include patterns'],
  'f.dir.recursive': ['包含子目录', 'Include subfolders'],
  'f.dir.nameTemplate': ['命名模板', 'Name template'],
  'f.dir.overwrite': ['覆盖已有成品', 'Overwrite existing'],
  'f.dir.ssh': ['SSH 连接', 'SSH connection'],
  'f.dir.kind': ['类型', 'Kind'],
  'h.path.remote': ['端点是 SSH：远端路径请手填，目录浏览器只能读本机目录。',
    'This endpoint is SSH: type the remote path by hand — the folder browser only reads this machine.'],
  'o.dir.empty': ['尚未添加目录：当前使用下方单目录设置。', 'No directory added yet: the single-directory settings below apply.'],
  'o.dir.defaultHint': ['新增目录时，以下单目录设置会作为默认值预填。', 'The single-directory settings below prefill new directories.'],
  'ov.bridge': ['DSH 中转', 'DSH relay'],
      'ov.bridgeUrl': ['中转地址', 'Relay address'],
      'ov.bridgeReady': ['已就绪', 'Ready'],
      'ov.bridgeMissing': ['未就绪', 'Not ready'],
      'o.manualModel': ['手动填写 / 其他引擎…', 'Manual entry / other engines…'],
      'o.manualPlaceholder': ['填模型名，或先展开下方「高级」换引擎', 'Type a model name, or open Advanced below to switch engine'],
      'o.groupDsh': ['DSH 配置', 'DSH models'],
      'o.groupOllama': ['Ollama（本机）', 'Ollama (local)'],
      'act.refreshOllama': ['刷新 Ollama 模型', 'Refresh Ollama models'],
      'ov.ollamaCount': ['本机模型数', 'Local models'],
      'f.engine.ollamaModel': ['Ollama 覆盖模型', 'Ollama override model'],
      'ov.ollamaOverride': ['历史覆盖模型（只有上方没选到模型时才生效）',
        'Legacy override model (only used when the picker above is empty)'],
      'act.clearOverride': ['清除覆盖', 'Clear override'],
      'g.engineAdvanced': ['高级：换引擎 / 手填提供方与地址', 'Advanced: switch engine, set provider or base URL'],
    'g.asrQuality': ['转写质量与反幻觉', 'Transcription quality'],
    'g.asrQualityOpen': ['展开调整这 7 项', 'Adjust these seven'],
    'g.asrQualityHint': [
      '这些参数用来压住转写里的复读、幻听和片尾致谢语。默认都按最保守的方式关着，需要更稳再逐项打开。',
      'These settings tame repeated lines, hallucinations and end-card thanks in the transcript. They are off by default; turn them on one by one.',
    ],
      'o.noProvider': ['（未选择）', '(not selected)'],
  'ov.noModelsYet': ['模型清单为空：检查 DSH 的模型配置', 'model catalog is empty: check the DSH model configuration'],
      'msg.modelTestOk': ['模型可用', 'Model works'],
      'msg.modelTestFail': ['模型不可用', 'Model failed'],
      'ov.lastRun': ['最近一次运行', 'Last run'],
      'ov.currentRun': ['当前运行', 'Current run'],
      'ov.none': ['暂无', 'None'],
      'ov.nextRun': ['下次运行', 'Next run'],
      'ov.dataDir': ['数据目录', 'Data directory'],
      'ov.ready': ['就绪', 'ready'],
      'ov.missing': ['缺失', 'missing'],
      'ov.exists': ['已创建', 'present'],
      'ov.absent': ['未创建', 'absent'],
      'ov.modelSource.llm': ['来自 DSH 模型服务', 'From the DSH model service'],
      'ov.modelSource.patch': ['来自 profile 配置', 'From the profile patch'],
      'ov.modelSource.none': ['未发现清单（无妨，可直接手填）', 'No catalog (fine, type a name)'],
      'ov.resetHint': [
        '恢复默认会先用默认值重写 config.json，并把旧文件备份为 config.json.bak。',
        'Restoring defaults rewrites config.json from the documented defaults and keeps the old file as config.json.bak.',
      ],

      'f.run.max': ['本次最多处理', 'Max this run'],
      'picker.empty': ['没有子目录', 'No subfolders'],
      'col.item': ['项目', 'Item'],
      'col.value': ['值', 'Value'],
      'col.time': ['时间', 'Time'],
      'col.name': ['文件', 'File'],
      'col.reason': ['跳过原因', 'Skip reason'],
      'col.profile': ['提示词配置', 'Prompt profile'],
      'col.size': ['大小', 'Size'],
      'col.check': ['检查项', 'Check'],
      'col.state': ['状态', 'State'],
      'col.detail': ['详情', 'Detail'],
      'col.fix': ['修复建议', 'Suggested fix'],
      'col.dir': ['目录', 'Directory'],
      'plan.dirs': ['本次涉及的目录', 'Directories in this plan'],
      'plan.unreachable': ['不可达的目录（已跳过）', 'Unreachable directories (skipped)'],

      'run.id': ['运行 ID', 'Run ID'],
      'run.exit': ['退出码', 'Exit code'],
      'run.elapsed': ['耗时', 'Elapsed'],
      'run.startedAt': ['开始时间', 'Started'],
      'run.finishedAt': ['结束时间', 'Finished'],
      'run.processed': ['产出', 'Produced'],
      'run.error': ['错误', 'Error'],
      'run.dryRun': ['仅预演（不落盘）', 'Dry run (no side effects)'],
      'run.dryRunHint': [
        '勾选后调用 run --dry-run，只验证流程不写文件，用于确认配置正确。',
        'Passes run --dry-run: validates the pipeline without writing output.',
      ],
      'run.max': ['本次最多处理', 'Max this run'],
      'run.maxHint': ['0 表示沿用「选片」里的上限。', '0 keeps the limit from the Selection tab.'],
      'run.tail': ['实时输出', 'Live output'],

      'plan.title': ['选片结果', 'Selection result'],
      'plan.candidates': ['候选', 'Candidates'],
      'plan.skipped': ['跳过', 'Skipped'],
      'plan.empty': ['没有候选文件', 'No candidates'],
      'plan.needRun': ['点「只选片」后可在这里看到候选与跳过原因。', 'Run "Plan only" to see candidates and skip reasons here.'],

      'log.title': ['日志', 'Logs'],
      'log.name': ['日志文件', 'Log file'],
      'log.lines': ['行数', 'Lines'],
      'log.empty': ['暂无日志', 'No log yet'],
      'log.source.memory': ['（来自内存缓冲，引擎还没写日志）', '(from memory; the engine has not written a log yet)'],
      'log.source.file': ['（来自文件尾巴）', '(tail of the file)'],

      'diag.title': ['诊断', 'Diagnostics'],
      'diag.doctor': ['环境自检（doctor）', 'Doctor'],
      'diag.state': ['引擎状态（status）', 'Engine status'],
      'diag.report': ['报告原文', 'Raw report'],
      'diag.empty': ['还没有诊断结果，点「诊断」运行一次。', 'No diagnostics yet; press "Diagnose".'],
      'diag.summary': ['通过 {ok} · 警告 {warn} · 失败 {fail}', '{ok} ok · {warn} warn · {fail} fail'],
      'diag.hintMissing': ['引擎不存在时无法诊断：先让 engine/subtitle-run 就绪。', 'Diagnostics need engine/subtitle-run.'],

      'sch.mode': ['调度方式', 'Mode'],
      'sch.time': ['每天执行时间', 'Daily time'],
      'sch.label': ['LaunchAgent 标识', 'LaunchAgent label'],
      'sch.enabled': ['启用调度', 'Enable schedule'],
      'sch.launchdHint': [
        'launchd 模式会写入 ~/Library/LaunchAgents/<label>.plist 并 bootstrap；程序是引擎 CLI 的 guard 子命令。',
        'launchd mode writes ~/Library/LaunchAgents/<label>.plist and bootstraps it; the program is the engine CLI guard subcommand.',
      ],
      'sch.dshHint': [
        'DSH 模式不创建任务，只给出可粘贴到会话里的提示词；由你在 DSH 里决定何时建立定时任务。',
        'DSH mode creates nothing; it only returns a prompt you can paste into a session.',
      ],
      'sch.offHint': ['关闭模式会卸载并删除 LaunchAgent（幂等，可重复点）。', 'Off unloads and deletes the LaunchAgent (idempotent).'],
      'sch.state': ['当前状态', 'Current state'],
      'sch.loaded': ['已加载', 'loaded'],
      'sch.notLoaded': ['未加载', 'not loaded'],
      'sch.plist': ['plist 路径', 'plist path'],
      'sch.prompt': ['可粘贴的提示词', 'Prompt to paste'],
      'sch.promptsDesc': [
        '每个触发点一段，粘到 DSH 会话里建成定时任务即可；守卫幂等，加几个点由你决定。',
        'One prompt per trigger point; paste it into a DSH session to create the scheduled task. Guard is idempotent, so the number of points is your call.',
      ],
      'sch.role.primary': ['主触发', 'Primary'],
      'sch.role.extra': ['兜底', 'Fallback'],
      'sch.noTriggers': ['还没有可用的触发点：先把「执行时间」填成 HH:MM。', 'No usable trigger point yet: set "Time" to HH:MM first.'],
      'sch.unsupported': [
        '当前系统不是 macOS，launchd 模式不可用；请改用 DSH 模式或关闭。',
        'launchd is macOS-only; use DSH mode or turn the schedule off.',
      ],

      'g.engine': ['模型接口', 'Model endpoint'],
      'g.engineHint': [
        '选一个模型就能用：DSH 里配好的模型与本机 Ollama 的模型在同一个下拉里，切换时引擎类型自动跟着改。',
        'Pick a model and go: DSH-configured models and local Ollama models share one dropdown, and the engine kind follows.',
      ],
      'g.source': ['来源', 'Source'],
      'g.output': ['输出', 'Output'],
      'g.sourceHint': [
        '「本地」用系统目录选择器；「SSH」需手填远程路径与主机信息。',
        'Local uses the folder picker; SSH needs the remote path and host details typed in.',
      ],
      'g.ssh': ['SSH 连接', 'SSH connection'],
      'g.selectionHint': [
        '按大小、时间与排序挑本次候选；maxPerRun 是「立即运行」不指定数量时的上限。',
        'Filters pick the candidates; maxPerRun caps a run that does not override it.',
      ],
      'g.promptsHint': [
        '按文件名匹配的提示词配置，从上到下第一个命中生效；没有命中则用默认提示词。',
        'Filename-matched prompt profiles apply top-down; the first hit wins, otherwise the default is used.',
      ],
      'g.qualityHint': [
        '字幕排版、压制参数与转写选项；压制依赖 ffmpeg 可执行文件绝对路径。',
        'Subtitle layout, burn-in parameters and transcription options; burn-in needs an absolute ffmpeg path.',
      ],
      'g.notify': ['通知', 'Notifications'],
      'g.paths': ['路径', 'Paths'],
      'g.profiles': ['提示词配置', 'Prompt profiles'],
      'g.default': ['默认提示词', 'Default prompts'],
      'g.profileName': ['名称', 'Name'],
      'g.profileMatch': ['匹配模式', 'Match patterns'],
      'g.profileMatchHint': ['逗号分隔，支持通配符，如 第01话* 。', 'Comma separated, wildcards allowed, e.g. 第01话*.'],
      'g.profileSeries': ['术语表', 'Glossary'],
      'h.profileSeries': ['按名字加载 ~/.dsh/data/subtitle-studio/series/<名字>.json；留空 = 不带专有名词表',
        'Loaded by name from ~/.dsh/data/subtitle-studio/series/<name>.json; empty = no proper-noun glossary'],
      'g.profileSystem': ['系统提示词', 'System prompt'],
      'g.profileContext': ['上下文（可选）', 'Context (optional)'],
      'g.profileEmpty': ['还没有按文件名匹配的提示词配置。', 'No filename-matched prompt profiles yet.'],
      'g.matchAll': ['不填则匹配全部', 'Empty matches everything'],

      'f.engine.kind': ['引擎类型', 'Engine kind'],
      'f.engine.provider': ['提供方', 'Provider'],
      'f.engine.model': ['模型', 'Model'],
      'f.engine.temperature': ['温度', 'Temperature'],
      'f.engine.seed': ['随机种子', 'Seed'],
      'f.engine.maxTokens': ['最大输出 token', 'Max tokens'],
      'f.engine.batchSize': ['每次批量条数', 'Batch size'],
      'f.engine.ollamaHost': ['Ollama 地址', 'Ollama host'],
      'f.engine.baseUrl': ['接口地址（OpenAI 兼容）', 'Base URL (OpenAI-compatible)'],
      'f.engine.apiKeyRef': ['钥匙串条目名', 'Keychain item'],
      'f.source.kind': ['来源类型', 'Source kind'],
      'f.source.path': ['来源目录', 'Source path'],
      'f.source.ssh.host': ['主机', 'Host'],
      'f.source.ssh.port': ['端口', 'Port'],
      'f.source.ssh.user': ['用户', 'User'],
      'f.source.ssh.keyPath': ['私钥路径', 'Private key'],
      'f.source.include': ['匹配扩展名', 'Include patterns'],
      'f.source.recursive': ['递归子目录', 'Recursive'],
      'f.output.kind': ['输出类型', 'Output kind'],
      'f.output.path': ['输出目录', 'Output path'],
      'f.output.ssh.host': ['主机', 'Host'],
      'f.output.ssh.port': ['端口', 'Port'],
      'f.output.ssh.user': ['用户', 'User'],
      'f.output.ssh.keyPath': ['私钥路径', 'Private key'],
      'f.output.nameTemplate': ['文件名模板', 'Name template'],
    'f.output.mode': ['输出方式', 'Output mode'],
    'f.subtitles.styleTemplate': ['字幕样式', 'Subtitle style'],
    'f.transcribe.conditionOnPreviousText': ['承接上文', 'Use previous text as context'],
    'f.transcribe.temperature': ['温度回退上限', 'Temperature fallback ceiling'],
    'f.transcribe.hallucinationSilenceThreshold': ['静音幻觉阈值(秒)', 'Hallucination silence threshold (s)'],
    'f.transcribe.minAvgLogprob': ['最低平均置信度', 'Minimum average logprob'],
    'f.transcribe.maxNoSpeechProb': ['最高非语音概率', 'Maximum no-speech probability'],
    'f.transcribe.keepThanksPhrases': ['保留致谢片尾语', 'Keep thank-you end cards'],
    'f.transcribe.keepRepeats': ['保留重复复读', 'Keep repeated lines'],
      'f.output.overwrite': ['覆盖已有文件', 'Overwrite'],
      'f.selection.maxPerRun': ['每次最多处理', 'Max per run'],
      'f.selection.skipTranslated': ['跳过已翻译', 'Skip translated'],
      'f.selection.minSizeGB': ['最小体积（GB）', 'Min size (GB)'],
      'f.selection.maxSizeGB': ['最大体积（GB）', 'Max size (GB)'],
      'f.selection.maxAgeDays': ['最大天数', 'Max age (days)'],
      'f.selection.order': ['排序', 'Order'],
      'f.selection.excludePatterns': ['排除规则', 'Exclude patterns'],
      'f.prompts.default.system': ['系统提示词', 'System prompt'],
      'f.prompts.default.context': ['上下文', 'Context'],
      'f.prompts.profiles': ['按文件名匹配', 'Filename profiles'],
      'f.subtitles.bilingual': ['双语字幕', 'Bilingual'],
      'f.subtitles.trimSilence': ['裁剪静音', 'Trim silence'],
      'f.subtitles.maxLineChars': ['每行最多字数', 'Max chars per line'],
      'f.subtitles.keepOriginalAsSrt': ['保留原文 srt', 'Keep original srt'],
      'f.burn.enabled': ['压制进视频', 'Burn in'],
      'f.burn.ffmpeg': ['ffmpeg 路径', 'ffmpeg path'],
      'f.burn.crf': ['CRF 画质', 'CRF'],
      'f.burn.preset': ['编码预设', 'Preset'],
      'f.burn.audio': ['音频', 'Audio'],
      'f.burn.fontSize': ['字号', 'Font size'],
      'f.subtitles.fontSize': ['双语中文字号', 'Bilingual Chinese size'],
      'f.transcribe.whisperModel': ['转写模型', 'Whisper model'],
      'f.transcribe.language': ['源语言', 'Source language'],
      'f.transcribe.keepAudio': ['保留中间音频', 'Keep audio'],
      'f.notify.channel': ['通知渠道', 'Channel'],
      'f.notify.onStart': ['开始时通知', 'Notify on start'],
      'f.notify.onSuccess': ['成功时通知', 'Notify on success'],
      'f.notify.onFailure': ['失败时通知', 'Notify on failure'],
      'f.schedule.enabled': ['启用调度', 'Enabled'],
      'f.schedule.mode': ['调度方式', 'Mode'],
      'f.schedule.time': ['执行时间', 'Time'],
      'f.schedule.guardTimes': ['额外触发点', 'Extra trigger points'],
      'f.schedule.label': ['LaunchAgent 标识', 'Label'],
      'f.paths.dataDir': ['数据目录', 'Data directory'],

      'h.engine.ollamaHost': [
        '模型列表取自该地址的 /api/tags。本机地址直接刷新；装在 NAS 等其他机器上的地址，保存后才会去访问。',
        'The list comes from /api/tags on this address. A loopback host is probed directly; an address on another machine (a NAS, say) is used only once you have saved it.',
      ],
    'h.output.mode': [
      '压制 = 把字幕烧进视频再上传；只生成字幕 = 只产出 .srt 文件，不压片。',
      'Burn renders the subtitles into the video; subtitle file only produces a .srt and leaves the video alone.',
    ],
    'h.subtitles.styleTemplate': [
      '样式预设只管外观（颜色、描边、位置），字号仍按视频分辨率自动选。',
      'A preset only sets the look (colour, outline, position); the font size still follows the video resolution.',
    ],
    'h.transcribe.conditionOnPreviousText': [
      '开启后每一段都承接上一段文字，能减少断句错乱；代价是上一段的错字会被带下去。',
      'Each chunk then continues from the previous one, which fixes broken sentences but can carry a mistake forward.',
    ],
    'h.transcribe.temperature': [
      '0 = 只用最可能的结果；大于 0 时按 0/0.2/0.4… 的阶梯重试，低置信片段会自动升温重转。',
      '0 uses only the most likely result; above 0 the decoder retries on a 0/0.2/0.4… ladder, so shaky chunks are re-decoded hotter.',
    ],
    'h.transcribe.hallucinationSilenceThreshold': [
      '静音超过这个秒数的片段按幻觉丢弃，0 = 不丢。',
      'Silences longer than this many seconds are dropped as hallucinations; 0 keeps them.',
    ],
    'h.transcribe.minAvgLogprob': [
      '平均置信度低于这个值的整段丢掉，0 = 不丢（置信度是负数，所以上限是 0）。',
      'Segments whose average confidence is below this are dropped; 0 keeps them (confidence is negative, so 0 is the ceiling).',
    ],
    'h.transcribe.maxNoSpeechProb': [
      '被判为「不是语音」的概率高于这个值的整段丢掉，0 = 不丢。',
      'Segments whose not-speech probability is above this are dropped; 0 keeps them.',
    ],
    'h.transcribe.keepThanksPhrases': [
      '开启后保留「ご視聴ありがとうございました」这类片尾致谢语，默认丢掉。',
      'Keep end-card lines such as "ご視聴ありがとうございました"; they are dropped by default.',
    ],
    'h.transcribe.keepRepeats': [
      '开启后保留复读的整词重复，默认压成一份。',
      'Keep whole-phrase repetitions; by default they are collapsed into one.',
    ],
      'h.engineAdvanced': [
        '默认走 DSH 配置或本机 Ollama，这里不用动。要接 MiniMax / DeepSeek / 任意 OpenAI 兼容端点时才展开。',
        'Leave this alone for DSH or local Ollama. Open it only to point at MiniMax / DeepSeek / any OpenAI-compatible endpoint.',
      ],
      'h.engine.provider': [
        'dsh 引擎走 DSH 宿主中转：凭据用 DSH 自己的，这里只挑提供方；在模型下拉里选一个会自动填上。',
        'The dsh engine relays through the DSH host: credentials stay in DSH, here you only pick the provider. Choosing a model fills it in.',
      ],
      'h.engine.model': [
        '选一个模型就行：DSH 里配的模型与本机 Ollama 的模型放在同一个下拉里，引擎类型跟着变，其余参数不用填。',
        'Pick one model: DSH-configured models and local Ollama models share this dropdown, and the engine kind follows the choice.',
      ],
      'h.engine.baseUrl': ['仅 openai-compatible 引擎必填', 'Required for the openai-compatible engine'],
      'h.engine.apiKeyRef': [
        '只填钥匙串条目名，密钥本身不入配置文件。',
        'A keychain item name only; the secret itself never enters the config.',
      ],
      'h.engine.temperature': ['范围 0 – 2', 'Range 0 – 2'],
      'h.burn.fontSize': ['0 表示按视频高度自动', '0 means automatic from video height'],
      'h.selection.maxPerRun': ['0 表示不限制', '0 means unlimited'],
      'h.output.nameTemplate': ['必须含 {stem}，可加 {lang} 等占位', 'Must contain {stem}; other placeholders allowed'],
      'h.transcribe.whisperModel': ['留空则用引擎默认', 'Empty uses the engine default'],
      'h.source.include': ['逗号分隔，如 *.mp4, *.mkv', 'Comma separated, e.g. *.mp4, *.mkv'],
      'h.selection.excludePatterns': ['逗号分隔的子串或通配符', 'Comma separated substrings or wildcards'],
      'h.schedule.time': ['24 小时制 HH:MM，按本机时区', '24-hour HH:MM in the local time zone'],
      'h.schedule.guardTimes': [
        '逗号分隔的 HH:MM，可留空。launchd 模式会把这些点一并写进定时器做漏跑补触发，DSH 模式则每个点生成一段提示词；守卫幂等，多触发点不会重复处理当天。',
        'Comma-separated HH:MM, may be empty. In launchd mode these become extra trigger points in the same plist (catch-up if the main one is missed); in DSH mode each point gets its own prompt. Guard is idempotent, so extra points never double-process a day.',
      ],
      'h.paths.dataDir': ['插件与引擎共用的数据目录（配置、日志、状态都在这里）', 'Folder shared by the plugin and the engine (config, logs, state)'],
      'h.selection.minSizeGB': ['小于此体积的视频不处理；0 = 不限制', 'Skip files smaller than this; 0 means no limit'],
      'h.selection.maxSizeGB': [
        '大于此体积的视频不处理；0 = 不限制。默认 10 GB 会把原盘大文件直接排除，按需要调大',
        'Skip files larger than this; 0 means no limit. The default 10 GB silently drops big remuxes — raise it if needed',
      ],
      'h.selection.maxAgeDays': ['只处理最近 N 天内修改的文件；0 = 不限制', 'Only files modified within the last N days; 0 means no limit'],
      'h.burn.ffmpeg': ['留空 = 在 PATH 里找 ffmpeg（压制和转写取音频都要用它）', 'Empty = look up ffmpeg on PATH (both burning and the transcribe audio extract need it)'],
      'h.burn.audio': [
        'copy 不重编码（最快，跟随原片）；aac 兼容性最好；opus 体积更小，但请配 .mkv/.webm 命名模板，放进 .mp4 很多播放器不认',
        'copy skips re-encoding (fastest, follows the source); aac is the most compatible; opus is smaller but wants an .mkv/.webm template — many players reject Opus in .mp4',
      ],
      'h.subtitles.maxLineChars': [
        '每行最多字数，压制前按字数折行（优先在标点处断）；0 = 不折行，交给 libass 自动处理',
        'Characters per line; the text is folded before burning, preferring punctuation breaks. 0 = no folding, let libass wrap automatically',
      ],
      'h.subtitles.fontSize': ['双语 ASS 的中文字号；0 = 按视频高度自动选档（英文按 1/1.7 比例）', 'Chinese font size for the bilingual ASS track; 0 = picked from video height (English at 1/1.7)'],

      'o.minimax': ['MiniMax', 'MiniMax'],
      'o.ollama': ['Ollama（本地）', 'Ollama (local)'],
      'o.deepseek': ['DeepSeek', 'DeepSeek'],
      'o.openai-compatible': ['OpenAI 兼容接口', 'OpenAI-compatible'],
      'o.local': ['本地', 'Local'],
      'o.ssh': ['SSH 远程', 'SSH'],
      'o.name-asc': ['名称升序', 'Name ↑'],
      'o.name-desc': ['名称降序', 'Name ↓'],
      'o.mtime-asc': ['时间升序', 'Time ↑'],
      'o.mtime-desc': ['最新优先', 'Newest first'],
      'o.size-asc': ['体积升序', 'Size ↑'],
      'o.size-desc': ['体积降序', 'Size ↓'],
      'o.launchd': ['launchd（系统级）', 'launchd (system)'],
      'o.dsh': ['DSH 定时任务（给提示词）', 'DSH schedule (prompt only)'],
      'o.off': ['关闭', 'Off'],
      'o.qqbot': ['QQ Bot', 'QQ Bot'],
      'o.none': ['不通知', 'No notification'],
      'o.command': ['执行命令', 'Run command'],
      'o.webhook': ['Webhook', 'Webhook'],
      'o.src.bundled': ['内置（插件自带）', 'Bundled'],
      'o.src.config': ['自定义', 'Custom'],
      'o.verdict.green': ['全部通过', 'All good'],
      'o.verdict.degraded': ['可用但有告警', 'Degraded'],
      'o.verdict.red': ['存在失败项', 'Failing'],
      'o.verdict.unknown': ['未检查', 'Not checked'],
      'g.engineWhere': ['引擎位置', 'Engine location'],
      'g.engineWhereHint': ['默认使用插件自带的引擎；要改用自己的构建时填绝对路径即可', 'Uses the engine bundled with the plugin; set an absolute path to use your own build'],
      'ov.engineSource': ['引擎来源', 'Engine source'],
      'ov.engineBundled': ['插件自带引擎', 'Bundled engine'],
      'ov.selfcheck': ['依赖自检', 'Dependency check'],
      'ov.selfcheckHint': ['插件启用后会自动跑一次 doctor，这里显示最近一次结果', 'Doctor runs automatically once the plugin is enabled; this shows the latest result'],
      'ov.selfcheckNone': ['还没有自检结果，点「重新自检」', 'No result yet — press Re-check'],
      'ov.scVerdict': ['结论', 'Verdict'],
      'ov.scChecked': ['检查时间', 'Checked at'],
      'ov.scSummary': ['检查项', 'Checks'],
      'ov.scAllGood': ['依赖齐全，没有告警', 'All dependencies present, no warnings'],
      'act.recheck': ['重新自检', 'Re-check'],
      'act.useBundled': ['恢复内置', 'Use bundled'],
      'act.testModel': ['测试模型', 'Test model'],
      'f.paths.engine': ['引擎路径覆盖', 'Engine path override'],
      'h.paths.engine': ['留空 = 用插件自带的 engine/subtitle-run', 'Empty = bundled engine/subtitle-run'],
      'f.notify.command': ['执行命令', 'Command'],
      'h.notify.command': ['可用 {text} / {title} 占位；正文也会从 stdin 与 $SUBTITLE_NOTIFY_TEXT 给出', 'Supports {text}/{title}; the text is also on stdin and $SUBTITLE_NOTIFY_TEXT'],
      'f.notify.webhookUrl': ['Webhook 地址', 'Webhook URL'],
      'h.notify.webhookUrl': ['例如 https://example.com/hook', 'e.g. https://example.com/hook'],
      'f.notify.webhookBody': ['请求体模板', 'Request body'],
      'h.notify.webhookBody': ['默认 {"text": "{text}"}；{text} 会按 JSON 转义后填入', 'Default {"text": "{text}"}; {text} is JSON-escaped'],
      'f.notify.qqAppId': ['QQ Bot AppID', 'QQ Bot AppID'],
      'h.notify.qqAppId': ['开放平台机器人详情页里的 AppID，非机密，存在 config.json', 'AppID from the bot page on the open platform; not secret, stored in config.json'],
      'f.notify.qqOpenId': ['接收人 OpenID', "Receiver's OpenID"],
      'h.notify.qqOpenId': ['机器人收到消息后，开放平台事件里的 openid', 'The openid in the open-platform event after the bot receives a message'],
      'f.notify.qqSecretService': ['钥匙串服务名', 'Keychain service'],
      'h.notify.qqSecretService': ['留空 = subtitle-studio-qqbot；钥匙串账号是已保存配置里的 AppID', 'Empty = subtitle-studio-qqbot; the keychain account is the saved AppID'],
      'f.notify.qqSendPath': ['自定义脚本路径', 'Custom script path'],
      'h.notify.qqSendPath': ['留空 = 不使用自定义脚本（请填写你本机脚本的路径）', 'Empty = no custom script (point this at your own helper)'],
      'f.notify.qqSecret': ['AppSecret', 'AppSecret'],
      'h.notify.qqSecret': [
        '只写进系统钥匙串，永不回显；账号取已保存配置里的 AppID，所以改完 AppID 要先保存配置。',
        'Written to the system keychain only and never echoed. The account is the saved AppID, so save the config after changing the AppID.',
      ],
      'notify.secretStateSaved': ['已保存到钥匙串', 'Saved to keychain'],
      'notify.secretStateMissing': ['未配置', 'Not configured'],
      'notify.secretStateEnv': ['已由环境变量提供', 'Provided by environment variable'],
      'notify.secretPlaceholder': ['粘贴 AppSecret（仅写入钥匙串）', 'Paste AppSecret (keychain only)'],
      'notify.saveSecret': ['保存密钥', 'Save secret'],
      'notify.sendTest': ['发送测试消息', 'Send test message'],
      'notify.secretEmpty': ['先填入 AppSecret', 'Enter the AppSecret first'],
      'notify.secretSaved': ['密钥已保存到钥匙串', 'Secret saved to the keychain'],
      'notify.secretSaveFailed': ['保存密钥失败', 'Failed to save the secret'],
      'notify.testSent': ['测试消息已发送', 'Test message sent'],
      'notify.testSkipped': ['测试消息未发送', 'Test message not sent'],
      'notify.testFailed': ['测试消息发送失败', 'Failed to send the test message'],
      'notify.testHint': ['测试消息使用已保存的配置发送', 'The test message uses the saved config'],
      'notify.noneHint': ['不发送任何通知', 'No notifications will be sent'],
      'notify.openidHelp': ['粘贴机器人收到的消息 JSON，我帮你取 openid', "Paste the message JSON the bot received and I'll extract the openid"],
      'notify.openidToggle': ['粘贴消息取 openid', 'Extract openid from message'],
      'notify.extractOpenid': ['提取 openid', 'Extract openid'],
      'notify.openidMissing': ['没找到 openid', 'No openid found'],
      'notify.openidFound': ['已填入 openid', 'openid filled in'],
      'notify.advanced': ['高级：回退到自定义发送脚本', 'Advanced: fall back to a custom send script'],
      'msg.recheckDone': ['自检完成', 'Re-check finished'],
      'h.notifyHint': ['通知由引擎执行，不影响 DSH 里的运行结果', 'Notifications come from the engine; the run result is unaffected'],
    'o.mlx': ['mlx_whisper（Apple Silicon）', 'mlx_whisper (Apple Silicon)'],
    'o.faster': ['faster-whisper（CPU / GPU）', 'faster-whisper (CPU / GPU)'],
    'f.transcribe.engine': ['转写引擎', 'Transcription engine'],
    'h.transcribe.engine': ['Apple Silicon 用 mlx_whisper；Intel 机器或已装 faster-whisper 的机器选 faster-whisper。',
      'Use mlx_whisper on Apple Silicon; pick faster-whisper on an Intel Mac or wherever faster-whisper is installed.'],
    'o.burn': ['压制进视频', 'Burn into video'],
    'o.default': ['白字黑边（默认）', 'White with black outline (default)'],
    'o.bold-yellow': ['醒目黄字', 'Bold yellow'],
    'o.top-white': ['顶部白字（纪录片）', 'Top white (documentary)'],
    'o.boxed': ['黑底白字（带底框）', 'White on black box'],
    'o.cinema': ['影院黑条白字', 'Cinema bar'],
    'o.srt': ['只生成字幕文件', 'Subtitle file only'],
      'o.copy': ['直接复制音轨', 'Copy audio'],
      'o.aac': ['AAC', 'AAC'],
      'o.opus': ['Opus', 'Opus'],
      'o.ultrafast': ['ultrafast', 'ultrafast'],
      'o.superfast': ['superfast', 'superfast'],
      'o.veryfast': ['veryfast', 'veryfast'],
      'o.faster': ['faster', 'faster'],
      'o.fast': ['fast', 'fast'],
      'o.medium': ['medium', 'medium'],
      'o.slow': ['slow', 'slow'],
      'o.slower': ['slower', 'slower'],
      'o.veryslow': ['veryslow', 'veryslow'],

      'v.num': ['必须是数字', 'Must be a number'],
      'v.int': ['必须是整数', 'Must be an integer'],
      'v.min': ['不得小于 {min}', 'Must be at least {min}'],
      'v.max': ['不得大于 {max}', 'Must be at most {max}'],
      'v.time': ['格式必须是 HH:MM', 'Must be HH:MM'],
      'v.required': ['必填', 'Required'],
      'v.stem': ['必须含 {stem}', 'Must contain {stem}'],
      'v.sizeRange': ['不得小于最小体积', 'Must not be below the minimum size'],
      'v.list': ['逗号分隔的字符串', 'Comma-separated strings'],
      'v.profileName': ['名称必填且唯一', 'Name is required and must be unique'],
    }

    /* ---------------------------------- helpers -------------------------------- */

    const pick = (index) => Object.fromEntries(Object.entries(STRINGS).map(([key, pair]) => [key, pair[index]]))
    const DICT_ZH = pick(0)
    const DICT_EN = pick(1)
    const i18n = { t: (key) => DICT_ZH[key] ?? key }

    /** Deep clone plain JSON. */
    const clone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)))

    /** Read a dotted path. */
    function getAt(root, dotted) {
      let cursor = root
      for (const segment of dotted.split('.')) {
        if (cursor === null || typeof cursor !== 'object') return undefined
        cursor = cursor[segment]
      }
      return cursor
    }

    /** Immutably write a dotted path. */
    function setAt(root, dotted, value) {
      const segments = dotted.split('.')
      const copy = clone(root)
      let cursor = copy
      for (let index = 0; index < segments.length - 1; index += 1) {
        const segment = segments[index]
        if (cursor[segment] === null || typeof cursor[segment] !== 'object') cursor[segment] = {}
        cursor = cursor[segment]
      }
      cursor[segments[segments.length - 1]] = value
      return copy
    }

    /** Build one bridge URL. */
    function bridgeUrl(endpoint) {
      return new URL(`${BRIDGE}${endpoint}`, document.baseURI).toString()
    }

    /** Call a bridge endpoint; never throws on a non-2xx. */
    async function api(endpoint, init) {
      try {
        const response = await fetch(bridgeUrl(endpoint), {
          credentials: 'same-origin',
          cache: 'no-store',
          signal: typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(30000) : undefined,
          ...init,
        })
        let body = null
        try {
          body = await response.json()
        } catch {
          body = null
        }
        return { status: response.status, ok: response.ok, body }
      } catch (error) {
        return { status: 0, ok: false, body: { ok: false, error: String(error && error.message ? error.message : error) } }
      }
    }

    /** POST JSON. */
    function postJson(endpoint, value) {
      return api(endpoint, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(value === undefined ? {} : value),
      })
    }

    /** Format a byte count. */
    function humanSize(bytes) {
      if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '—'
      const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
      let value = bytes
      let unit = 0
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024
        unit += 1
      }
      return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
    }

    /** Format a duration in ms. */
    function humanMs(ms) {
      if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '—'
      if (ms < 1000) return `${Math.round(ms)} ms`
      const seconds = Math.round(ms / 1000)
      if (seconds < 60) return `${seconds} s`
      return `${Math.floor(seconds / 60)} min ${seconds % 60} s`
    }

    /** Format an ISO timestamp in local time. */
    function humanTime(iso) {
      if (typeof iso !== 'string' || iso === '') return '—'
      const date = new Date(iso)
      if (Number.isNaN(date.getTime())) return iso
      const pad = (value) => String(value).padStart(2, '0')
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
    }

    /* --------------------------------- controls -------------------------------- */

    function Btn({ children, onClick, primary, disabled, title }) {
      const style = primary === true ? { ...S.btnPrimary, ...(disabled ? S.btnDisabled : {}) } : { ...S.btn, ...(disabled ? S.btnDisabled : {}) }
      return h(
        'button',
        {
          type: 'button',
          className: primary === true ? 'ss-btn ss-btn-primary' : 'ss-btn',
          style,
          onClick: disabled ? undefined : onClick,
          disabled,
          title,
        },
        children,
      )
    }

    function Card({ title, desc, tone, children, actions }) {
      return h(
        'section',
        { style: S.card },
        title !== undefined || actions !== undefined
          ? h(
              'div',
              { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, flexWrap: 'wrap' } },
              h('h3', { style: S.cardTitle }, title),
              actions === undefined ? null : h('div', { style: S.actions }, actions),
            )
          : null,
        desc === undefined ? null : h('p', { style: S.cardDesc }, desc),
        children,
      )
    }

    function Chip({ tone, children }) {
      return h('span', { style: S.chip(tone) }, children)
    }

    function Banner({ tone, children }) {
      if (children === null || children === undefined || children === '') return null
      return h('div', { style: S.banner(tone) }, children)
    }

    function Switch({ checked, onChange, label }) {
      return h(
        'button',
        {
          type: 'button',
          role: 'switch',
          className: 'ss-switch',
          'aria-checked': checked === true,
          'aria-label': label,
          onClick: () => onChange(checked !== true),
          style: {
            width: 40,
            height: 22,
            flex: '0 0 auto',
            borderRadius: 999,
            border: `1px solid ${checked === true ? T.accent : T.borderStrong}`,
            background: checked === true ? T.accent : 'transparent',
            position: 'relative',
            cursor: 'pointer',
            padding: 0,
          },
        },
        h('span', {
          style: {
            position: 'absolute',
            top: 2,
            left: checked === true ? 19 : 2,
            width: 16,
            height: 16,
            borderRadius: 999,
            background: checked === true ? T.onAccent : T.borderStrong,
            transition: 'left 0.15s ease',
          },
        }),
      )
    }

    function TextInput({ value, onChange, placeholder, disabled }) {
      return h('input', {
        type: 'text',
        style: { ...S.control, opacity: disabled ? 0.6 : 1 },
        value: value === undefined || value === null ? '' : String(value),
        placeholder,
        disabled,
        spellCheck: false,
        onChange: (event) => onChange(event.target.value),
      })
    }

    function NumberInput({ value, onChange, min, max, step }) {
      return h('input', {
        type: 'number',
        style: S.control,
        value: value === undefined || value === null ? '' : value,
        min,
        max,
        step,
        onChange: (event) => {
          const text = event.target.value
          onChange(text === '' ? '' : Number(text))
        },
      })
    }

    function Select({ value, onChange, options, renderOption }) {
      return h(
        'select',
        {
          style: { ...S.control, appearance: 'auto' },
          value: value === undefined || value === null ? '' : String(value),
          onChange: (event) => onChange(event.target.value),
        },
        (options || []).map((option) =>
          h('option', { key: option, value: option }, renderOption === undefined ? option : renderOption(option)),
        ),
      )
    }

    function TextArea({ value, onChange, rows }) {
      return h('textarea', {
        style: { ...S.control, minHeight: rows === undefined ? 64 : rows * 20, resize: 'vertical' },
        value: value === undefined || value === null ? '' : String(value),
        onChange: (event) => onChange(event.target.value),
        spellCheck: false,
      })
    }

    /**
     * Inline folder browser for the path fields. Directories only; the host
     * enforces the denylist and realpath normalisation.
     */
    function PathPicker({ value, onChange, remote }) {
      const [open, setOpen] = React.useState(false)
      const [listing, setListing] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [busy, setBusy] = React.useState(false)

      const visit = React.useCallback(async (target) => {
        setBusy(true)
        const response = await api(`listdir?files=0&path=${encodeURIComponent(target)}`)
        setBusy(false)
        if (response.body && response.body.ok === true) {
          setListing(response.body.data)
          setError(null)
        } else {
          setError(response.body && (response.body.message || response.body.error) ? response.body.message || response.body.error : 'unreadable')
        }
      }, [])

      React.useEffect(() => {
        if (open === true && listing === null) visit(value === undefined || value === '' ? '' : value)
      }, [open, listing, value, visit])

      return h(
        'div',
        { style: { display: 'flex', flexDirection: 'column' } },
        h(
          'div',
          { style: { display: 'flex', gap: 6 } },
          h(TextInput, { value, onChange }),
          // 目录浏览器读的是本机文件系统：端点是 SSH 时点它只会领用户去挑错目录。
          remote === true
            ? null
            : h('div', { style: { flex: '0 0 auto' } }, h(Btn, { onClick: () => setOpen(open !== true) }, i18n.t(open ? 'act.close' : 'act.browse'))),
        ),
        remote === true ? h('div', { style: S.hint }, i18n.t('h.path.remote')) : null,
        open !== true
          ? null
          : h(
              'div',
              { style: S.picker },
              h(
                'div',
                { style: { display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' } },
                h(Btn, { onClick: () => visit(listing ? listing.path : value), disabled: busy }, i18n.t('act.refresh')),
                h(Btn, { onClick: () => listing && listing.parent && visit(listing.parent), disabled: busy || !listing || !listing.parent }, i18n.t('act.up')),
                h('code', { style: { ...S.code, flex: '1 1 180px' } }, listing ? listing.path : '…'),
              ),
              error === null ? null : h(Banner, { tone: 'err' }, error),
              h(
                'div',
                { style: S.pickerList },
                (listing ? listing.entries : []).map((entry) =>
                  h(
                    'button',
                    {
                      key: entry.path,
                      type: 'button',
                      style: S.pickerItem,
                      onClick: () => visit(entry.path),
                      title: entry.path,
                    },
                    h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, `📁 ${entry.name}`),
                    h('span', { style: { color: T.tertiary, fontSize: 11, flex: '0 0 auto' } }, entry.mtime ? humanTime(entry.mtime) : ''),
                  ),
                ),
                listing !== null && listing.entries.length === 0 ? h('div', { style: S.hint }, i18n.t('picker.empty')) : null,
              ),
              h(
                'div',
                { style: { display: 'flex', gap: 8 } },
                h(
                  'div',
                  { style: { flex: 1 } },
                  h(
                    Btn,
                    {
                      primary: true,
                      onClick: () => {
                        if (listing) onChange(listing.path)
                        setOpen(false)
                      },
                      disabled: listing === null,
                    },
                    i18n.t('act.choose'),
                  ),
                ),
              ),
            ),
      )
    }

    /** One labelled form control with its hint and inline error. */
    function Field({ field, children, hint, error, labelKey }) {
      const key = labelKey === undefined ? `f.${field.path}` : labelKey
      return h(
        'div',
        { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
        h('div', { style: S.labelRow }, h('span', { style: S.label }, i18n.t(key))),
        children,
        error ? h('div', { style: S.err }, error) : hint ? h('div', { style: S.hint }, hint) : null,
      )
    }

    /* -------------------------------- validation ------------------------------- */

    /** Mirror of the host's per-field rules, for immediate inline feedback. */
    function validateDraft(draft, fields) {
      const map = {}
      if (draft === null) return map
      const rows = Array.isArray(fields) ? fields : []
      const put = (path, message) => {
        if (map[path] === undefined) map[path] = message
      }
      const checkValue = (field) => {
        const value = getAt(draft, field.path)
        if (value === undefined || value === null) return
        if (field.kind === 'int' || field.kind === 'number') {
          if (value === '') return
          const numeric = Number(value)
          if (!Number.isFinite(numeric)) return put(field.path, i18n.t('v.num'))
          if (field.kind === 'int' && !Number.isInteger(numeric)) return put(field.path, i18n.t('v.int'))
          if (field.min !== undefined && numeric < field.min) {
            return put(field.path, i18n.t('v.min').replace('{min}', String(field.min)))
          }
          if (field.max !== undefined && numeric > field.max) {
            return put(field.path, i18n.t('v.max').replace('{max}', String(field.max)))
          }
        }
        if (field.kind === 'time') {
          if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(String(value))) put(field.path, i18n.t('v.time'))
        }
        if (field.kind === 'string' && field.required === true && String(value).trim() === '') {
          put(field.path, i18n.t('v.required'))
        }
      }
      for (const field of rows) {
        if (field.kind === 'profiles') continue
        checkValue(field)
      }
      const template = String(getAt(draft, 'output.nameTemplate') || '')
      if (template !== '' && !template.includes('{stem}')) put('output.nameTemplate', i18n.t('v.stem').replace('{stem}', '{stem}'))
      const min = Number(getAt(draft, 'selection.minSizeGB'))
      const max = Number(getAt(draft, 'selection.maxSizeGB'))
      if (Number.isFinite(min) && Number.isFinite(max) && max < min) put('selection.maxSizeGB', i18n.t('v.sizeRange'))
      const profiles = getAt(draft, 'prompts.profiles') || []
      const seen = {}
      profiles.forEach((profile, index) => {
        const name = String(profile && profile.name ? profile.name : '').trim()
        if (name === '') put(`prompts.profiles.${index}.name`, i18n.t('v.profileName'))
        else if (seen[name.toLowerCase()] === true) put(`prompts.profiles.${index}.name`, i18n.t('v.profileName'))
        else seen[name.toLowerCase()] = true
      })
      return map
    }

    /* ---------------------------------- panel --------------------------------- */

    /** Remembered across remounts so a settings-page refresh keeps the tab. */
    let rememberedTab = 'overview'

    function Panel() {
      const [data, setData] = React.useState(null)
      const [draft, setDraft] = React.useState(null)
      const [tab, setTab] = React.useState(rememberedTab)
      const [loadError, setLoadError] = React.useState(null)
      const [issues, setIssues] = React.useState([])
      const [banner, setBanner] = React.useState(null)
      const [busy, setBusy] = React.useState('')
      const [status, setStatus] = React.useState(null)
      const [doctor, setDoctor] = React.useState(null)
      const [engineStatus, setEngineStatus] = React.useState(null)
      const [plan, setPlan] = React.useState(null)
      const [logs, setLogs] = React.useState(null)
      const [logName, setLogName] = React.useState('run')
      const [logLines, setLogLines] = React.useState(200)
      const [maxOverride, setMaxOverride] = React.useState('')
      const [dryRun, setDryRun] = React.useState(false)
      // QQ Bot 凭证是手填的：secret 只进钥匙串，所以这里只持有输入框里的临时值。
      const [qqSecret, setQqSecret] = React.useState('')
      const [qqNotice, setQqNotice] = React.useState(null)
      const [openidOpen, setOpenidOpen] = React.useState(false)
      const [openidPaste, setOpenidPaste] = React.useState('')
      const [openidError, setOpenidError] = React.useState(null)
      const [qqAdvancedOpen, setQqAdvancedOpen] = React.useState(false)
      // 模型试跑：把「模型能不能用」当场验证掉，别等整条流水线跑失败才发现。
      const [modelTest, setModelTest] = React.useState(null)
      const [modelTesting, setModelTesting] = React.useState(false)
      // 引擎 tab 只让人选一个模型：DSH 配置的模型与本机 Ollama 模型平级放在同一个下拉里。
      const [ollamaModels, setOllamaModels] = React.useState([])
      const [ollamaNote, setOllamaNote] = React.useState('')
      const [ollamaLoading, setOllamaLoading] = React.useState(false)
      const [engineAdvanced, setEngineAdvanced] = React.useState(false)
      // 选了「手动填写」就停在那儿：别让下一帧又按目录把选中项弹回去。
      const [manualEntry, setManualEntry] = React.useState(false)

      /** Adopt a describe payload as the new truth. */
      const adopt = React.useCallback((payload) => {
        setData(payload)
        setDraft(clone(payload.config))
        setIssues([])
      }, [])

      const load = React.useCallback(async () => {
        setLoadError(null)
        const response = await api('describe')
        if (response.body && response.body.ok === true) adopt(response.body.data)
        else setLoadError((response.body && (response.body.error || response.body.message)) || `HTTP ${response.status}`)
      }, [adopt])

      React.useEffect(() => {
        load()
      }, [load])

      /** 拉本机 Ollama 的模型列表；失败只留一行说明，不影响其它配置。 */
      const refreshOllama = React.useCallback(async (rawHost) => {
        const host = String(rawHost ?? '').trim()
        if (host === '') {
          setOllamaModels([])
          setOllamaNote('')
          return
        }
        setOllamaLoading(true)
        const response = await api(`ollama-models?host=${encodeURIComponent(host)}`)
        setOllamaLoading(false)
        const payload = response.body && response.body.ok === true ? response.body.data : null
        if (payload === null || payload === undefined) {
          setOllamaModels([])
          setOllamaNote((response.body && (response.body.error || response.body.message)) || `HTTP ${response.status}`)
          return
        }
        setOllamaModels(Array.isArray(payload.models) ? payload.models : [])
        setOllamaNote(String(payload.error ?? ''))
      }, [])

      // describe 到手后探一次，让 Ollama 分组在第一屏就有内容（没装 Ollama 就留空）。
      React.useEffect(() => {
        refreshOllama(data && data.config && data.config.engine ? data.config.engine.ollamaHost : '')
      }, [data, refreshOllama])

      // Status polling keeps the run card honest without any browser storage.
      React.useEffect(() => {
        let alive = true
        const tick = async () => {
          const response = await api('status')
          if (!alive) return
          if (response.body && response.body.ok === true) setStatus(response.body.data)
        }
        tick()
        const timer = setInterval(tick, 2500)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [])

      const refreshLogs = React.useCallback(
        async (name, lines) => {
          const response = await api(`logs?name=${encodeURIComponent(name)}&lines=${lines}`)
          if (response.body && response.body.ok === true) setLogs(response.body.data)
          else setLogs({ lines: [], error: (response.body && (response.body.error || response.body.message)) || 'no log', available: [] })
        },
        [],
      )

      React.useEffect(() => {
        if (tab === 'diagnose') refreshLogs(logName, logLines)
      }, [tab, logName, logLines, refreshLogs])

      const switchTab = (next) => {
        rememberedTab = next
        setTab(next)
      }

      const localErrors = React.useMemo(
        () => validateDraft(draft, data !== null && Array.isArray(data.fields) ? data.fields : []),
        [draft, data],
      )
      const serverErrors = React.useMemo(() => {
        const map = {}
        for (const issue of issues) {
          if (issue.level === 'error' && map[issue.path] === undefined) map[issue.path] = issue.message
        }
        return map
      }, [issues])

      const fieldError = (path) => serverErrors[path] ?? localErrors[path] ?? null

      const setValue = (path, value) => setDraft((previous) => setAt(previous, path, value))

      /* ------------------------------ notify actions ----------------------------- */

      /** 桥返回的 error/message，失败时如实展示，不编造成成功。 */
      const notifyErrorText = (response) =>
        (response.body && (response.body.error || response.body.message)) || `HTTP ${response.status}`

      /** 把 AppSecret 写进钥匙串；成功后清空输入框并刷新 describe 的 notifySecret。 */
      const saveQqSecret = async () => {
        const secret = qqSecret.trim()
        if (secret === '') {
          setQqNotice({ tone: 'warn', text: i18n.t('notify.secretEmpty') })
          return
        }
        setBusy('notify-secret')
        const draftAppId = String(getAt(draft, 'notify.qqAppId') || '').trim()
        const response = await postJson('notify-secret', { secret, appId: draftAppId })
        setBusy('')
        if (response.body && response.body.ok === true) {
          setQqSecret('')
          setQqNotice({ tone: 'ok', text: i18n.t('notify.secretSaved') })
          const reported = response.body.data && response.body.data.service ? { service: response.body.data.service } : {}
          const fresh = await api('describe')
          if (fresh.body && fresh.body.ok === true) {
            // 只刷新 describe 的派生状态，不动 draft，避免丢掉还没保存的 AppID/openid。
            setData({
              ...fresh.body.data,
              notifySecret: { ...(fresh.body.data.notifySecret || {}), ...reported, configured: true },
            })
          } else {
            setData((previous) =>
              previous === null
                ? previous
                : { ...previous, notifySecret: { ...(previous.notifySecret || {}), ...reported, configured: true } },
            )
          }
        } else {
          setQqNotice({ tone: 'err', text: `${i18n.t('notify.secretSaveFailed')}: ${notifyErrorText(response)}` })
        }
      }

      /** 用已保存的配置真发一条测试消息；sent/reason 原样显示。 */
      const sendNotifyTest = async () => {
        setBusy('notify-test')
        const response = await postJson('notify-test', {})
        setBusy('')
        const payload = response.body || {}
        const sentData = payload.data || {}
        if (response.ok === true && payload.ok === true && sentData.sent === true) {
          const reason = sentData.reason ? ` · ${sentData.reason}` : ''
          setQqNotice({ tone: 'ok', text: `${i18n.t('notify.testSent')}${reason}` })
          return
        }
        // 桥在这种情况下返回 500 + data（引擎发出了但没成功），reason 必须照实带出来。
        const reason = sentData.reason ? ` · ${sentData.reason}` : ''
        if (response.ok === true && payload.ok === true && sentData.sent === false) {
          setQqNotice({ tone: 'warn', text: `${i18n.t('notify.testSkipped')}${reason}` })
          return
        }
        setQqNotice({ tone: 'err', text: `${i18n.t('notify.testFailed')}: ${notifyErrorText(response)}` })
      }

      /** 从粘贴的事件 JSON 里抓 openid；抓不到就明确说没找到。 */
      const extractQqOpenId = () => {
        const match = /(?:user_)?openid"?\s*[:=]\s*"([0-9A-Fa-f]{16,})"/.exec(openidPaste)
        if (match === null) {
          setOpenidError(i18n.t('notify.openidMissing'))
          return
        }
        setValue('notify.qqOpenId', match[1])
        setOpenidError(null)
        setQqNotice({ tone: 'ok', text: i18n.t('notify.openidFound') })
      }
      const dirty = React.useMemo(
        () => data !== null && draft !== null && JSON.stringify(data.config) !== JSON.stringify(draft),
        [data, draft],
      )

      const save = async () => {
        if (Object.keys(localErrors).length > 0) {
          setBanner({ tone: 'err', text: i18n.t('msg.localErrors') })
          return
        }
        setBusy('save')
        const response = await postJson('save', { config: draft })
        setBusy('')
        if (response.body && response.body.ok === true) {
          adopt(response.body.data)
          const warns = Array.isArray(response.body.warnings) ? response.body.warnings : []
          setIssues(warns)
          setBanner(
            warns.length === 0
              ? { tone: 'ok', text: `${i18n.t('msg.saved')} · ${humanTime(new Date().toISOString())}` }
              : {
                  tone: 'warn',
                  text: `${i18n.t('msg.savedWithWarnings')} · ${warns
                    .map((issue) => `${issue.path}: ${issue.message}`)
                    .join(' / ')}`,
                },
          )
        } else if (response.status === 422) {
          setIssues((response.body && response.body.issues) || [])
          setBanner({ tone: 'err', text: (response.body && (response.body.error || response.body.message)) || i18n.t('msg.localErrors') })
        } else if (response.status === 409) {
          setBanner({ tone: 'err', text: i18n.t('msg.configMalformed') })
        } else {
          setBanner({
            tone: 'err',
            text: `${i18n.t('msg.loadFailed')}: ${(response.body && (response.body.error || response.body.message)) || response.status}`,
          })
        }
      }

      const reload = async () => {
        setBusy('reload')
        await load()
        setBusy('')
        setBanner({ tone: 'ok', text: i18n.t('msg.reloaded') })
      }

      const revert = () => {
        if (data !== null) {
          setDraft(clone(data.config))
          setIssues([])
          setBanner({ tone: 'ok', text: i18n.t('msg.reverted') })
        }
      }

      const restoreDefaults = async () => {
        if (typeof window !== 'undefined' && typeof window.confirm === 'function') {
          if (window.confirm(i18n.t('ov.resetHint')) !== true) return
        }
        setBusy('defaults')
        const response = await postJson('reset', {})
        setBusy('')
        if (response.body && response.body.ok === true) {
          adopt(response.body.data)
          setBanner({ tone: 'ok', text: i18n.t('msg.resetDone') })
        } else {
          setBanner({ tone: 'err', text: (response.body && response.body.error) || 'reset failed' })
        }
      }

      const startRun = async () => {
        setBusy('run')
        const max = String(maxOverride).trim()
        const response = await postJson('run', {
          max: max === '' ? undefined : Number(max),
          dryRun,
        })
        setBusy('')
        if (response.body && response.body.ok === true) {
          setBanner({ tone: 'ok', text: `${i18n.t('msg.runStarted')} · ${response.body.data.runId}` })
        } else if (response.status === 409) {
          setBanner({ tone: 'warn', text: i18n.t('msg.runBusy') })
        } else {
          const code = response.body && response.body.code ? response.body.code : ''
          setBanner({
            tone: 'err',
            text: `${(response.body && (response.body.message || response.body.error)) || 'run failed'}${code === 'engine-missing' ? ` — ${i18n.t('msg.engineMissing')}` : ''}`,
          })
        }
      }

      const cancelRun = async () => {
        setBusy('cancel')
        const response = await postJson('cancel', { runId: status && status.current ? status.current.runId : undefined })
        setBusy('')
        setBanner(
          response.body && response.body.ok === true
            ? { tone: 'ok', text: 'cancelled' }
            : { tone: 'warn', text: (response.body && response.body.message) || 'cancel failed' },
        )
      }

      const runPlan = async () => {
        setBusy('plan')
        const response = await postJson('plan', {})
        setBusy('')
        if (response.body && response.body.result && response.body.result.ok === true) {
          setPlan(response.body.result.data)
          setBanner({ tone: 'ok', text: i18n.t('msg.planDone') })
        } else {
          setPlan({ candidates: [], skipped: [], error: (response.body && (response.body.parseError || response.body.message || response.body.error)) || 'plan failed' })
          setBanner({ tone: 'err', text: (response.body && (response.body.message || response.body.error || response.body.parseError)) || 'plan failed' })
        }
        switchTab('selection')
      }

      const runDoctor = async () => {
        setBusy('doctor')
        const response = await postJson('doctor', {})
        const engineReply = await api('engine-status')
        setBusy('')
        if (response.body && response.body.result) setDoctor(response.body)
        else setDoctor(response.body || {})
        if (engineReply.body && engineReply.body.result) setEngineStatus(engineReply.body)
        switchTab('diagnose')
      }

      const runSelftest = async () => {
        setBusy('selftest')
        const response = await postJson('selftest', {})
        setBusy('')
        setBanner(
          response.body && response.body.ok === true
            ? { tone: 'ok', text: `${i18n.t('msg.runStarted')} · ${response.body.data.runId}` }
            : { tone: 'err', text: (response.body && (response.body.message || response.body.error)) || 'selftest failed' },
        )
      }

      const recheckDeps = async () => {
        setBusy('recheck')
        const response = await postJson('selfcheck?force=1', {})
        setBusy('')
        if (response.body && response.body.ok === true) {
          const fresh = await api('describe')
          if (fresh.body && fresh.body.ok === true) {
            setData(fresh.body.data)
            if (draft === null) setDraft(clone(fresh.body.data.config))
          }
          setBanner({ tone: 'ok', text: i18n.t('msg.recheckDone') })
        } else {
          setBanner({ tone: 'err', text: (response.body && (response.body.message || response.body.error)) || 'selfcheck failed' })
        }
      }

      const applySchedule = async () => {
        if (draft === null) return
        setBusy('schedule')
        const response = await postJson('schedule', { config: draft })
        setBusy('')
        if (response.body && response.body.ok === true) {
          const steps = (response.body.data && response.body.data.steps) || []
          setBanner({ tone: 'ok', text: steps.includes('unchanged') ? i18n.t('msg.applyNoChanges') : `schedule ok (${steps.join(' → ')})` })
          const fresh = await api('describe')
          if (fresh.body && fresh.body.ok === true) {
            setData(fresh.body.data)
            setDraft(clone(fresh.body.data.config))
          }
        } else {
          setBanner({ tone: 'err', text: (response.body && (response.body.error || response.body.message)) || 'schedule failed' })
          const fresh = await api('describe')
          if (fresh.body && fresh.body.ok === true) setData(fresh.body.data)
        }
      }

      const removeSchedule = async () => {
        if (draft === null) return
        const next = setAt(setAt(setAt(draft, 'schedule.enabled', false), 'schedule.mode', 'off'), 'schedule.time', draft.schedule.time)
        setBusy('schedule')
        const response = await postJson('schedule', { config: next })
        setBusy('')
        setBanner(
          response.body && response.body.ok === true
            ? { tone: 'ok', text: 'schedule removed' }
            : { tone: 'err', text: (response.body && (response.body.error || response.body.message)) || 'schedule failed' },
        )
        const fresh = await api('describe')
        if (fresh.body && fresh.body.ok === true) {
          setData(fresh.body.data)
          setDraft(clone(fresh.body.data.config))
        }
      }

      /** Clear the banner after a while so the panel does not stay noisy. */
      React.useEffect(() => {
        if (banner === null) return undefined
        const timer = setTimeout(() => setBanner(null), 6000)
        return () => clearTimeout(timer)
      }, [banner])

      if (loadError !== null && data === null) {
        return h(
          'div',
          { style: S.root },
          h(Banner, { tone: 'err' }, `${i18n.t('msg.loadFailed')}: ${loadError}`),
          h('div', { style: S.actions }, h(Btn, { onClick: load }, i18n.t('act.reload'))),
        )
      }
      if (data === null || draft === null) return h('div', { style: S.root }, h('div', { style: S.hint }, i18n.t('msg.loading')))

      const fields = Array.isArray(data.fields) ? data.fields : []
      const groupFields = (group) => fields.filter((field) => field.group === group && field.kind !== 'profiles')
      const running = status !== null && status.running === true
      const currentRun = status !== null && status.current ? status.current : null
      const lastRun = status !== null && status.last ? status.last : data.run && data.run.last
      const engineReady = data.engineExists === true

      /* ------------------------------ field rendering ----------------------------- */

      /* Only show a control the engine actually reads with the current
         settings. A field that changes nothing is worse than a missing one:
         it teaches the user that the panel lies. Both the group renderer and
         the hand-built lists go through this predicate. */
      const fieldVisible = (field) => {
        const path = field.path
        // 输出方式是唯一的开关：只生成字幕文件时，压制参数与样式都读不到
        const burnMode = getAt(draft, 'output.mode') !== 'srt'
        // ffmpeg 两种模式都要用（抽音频 / 分段 / ffprobe），不随输出方式隐藏
        if (path === 'burn.ffmpeg') return true
        if (path === 'burn.crf' || path === 'burn.preset' || path === 'burn.audio' || path === 'burn.fontSize') {
          if (!burnMode) return false
          // bilingual burns go through ASS, which reads subtitles.fontSize instead
          if (path === 'burn.fontSize') return getAt(draft, 'subtitles.bilingual') !== true
          return true
        }
        if (path === 'subtitles.styleTemplate') return burnMode
        if (path === 'subtitles.fontSize') return getAt(draft, 'subtitles.bilingual') === true
        if (path === 'notify.onStart' || path === 'notify.onSuccess' || path === 'notify.onFailure') {
          return getAt(draft, 'notify.channel') !== 'none'
        }
        if (path === 'schedule.label') return getAt(draft, 'schedule.mode') === 'launchd'
        return true
      }

      const renderControl = (field) => {
        const value = getAt(draft, field.path)
        const error = fieldError(field.path)
        const hintKey = `h.${field.path}`
        const hintText = STRINGS[hintKey] !== undefined ? i18n.t(hintKey) : undefined
        // A text field that silently truncates on save is a trap: show the
        // budget once it starts to matter.
        const budget =
          typeof field.maxLength === 'number' && typeof value === 'string' && value.length > field.maxLength * 0.75
            ? `${value.length}/${field.maxLength}`
            : null
        const hint = [hintText, budget].filter((part) => part !== null && part !== undefined).join(' · ')
        let control = null
        switch (field.kind) {
          case 'bool':
            control = h(Switch, {
              checked: value === true,
              onChange: (next) => setValue(field.path, next),
              label: i18n.t(`f.${field.path}`),
            })
            break
          case 'enum':
            control = h(Select, {
              value,
              options: field.options || [],
              onChange: (next) => setValue(field.path, next),
              renderOption: (option) => (STRINGS[`o.${option}`] !== undefined ? i18n.t(`o.${option}`) : option),
            })
            break
          case 'int':
          case 'number':
            control = h(NumberInput, {
              value,
              min: field.min,
              max: field.max,
              step: field.step === undefined ? (field.kind === 'int' ? 1 : 0.01) : field.step,
              onChange: (next) => setValue(field.path, next),
            })
            break
          case 'text':
            control = h(TextArea, { value, rows: 4, onChange: (next) => setValue(field.path, next) })
            break
          case 'list':
            control = h(TextInput, {
              value: Array.isArray(value) ? value.join(', ') : value === undefined ? '' : String(value),
              placeholder: hint,
              onChange: (next) =>
                setValue(
                  field.path,
                  next
                    .split(',')
                    .map((part) => part.trim())
                    .filter((part) => part !== ''),
                ),
            })
            break
          case 'time':
            control = h('input', {
              type: 'time',
              style: S.control,
              value: typeof value === 'string' ? value : '',
              onChange: (event) => setValue(field.path, event.target.value),
            })
            break
          default:
            if (field.path === 'source.path' || field.path === 'output.path') {
              control = h(PathPicker, {
                value,
                onChange: (next) => setValue(field.path, next),
                remote: getAt(draft, `${field.path.split('.')[0]}.kind`) === 'ssh',
              })
            } else {
              control = h(TextInput, { value, onChange: (next) => setValue(field.path, next), placeholder: hint })
            }
            break
        }
        return h(
          'div',
          { key: field.path, style: field.kind === 'text' ? { gridColumn: '1 / -1' } : undefined },
          h(Field, { field, hint, error }, control),
        )
      }

      const renderGroup = (group, excludePrefix) => {
        const rows = groupFields(group)
          .filter(
            (field) => excludePrefix === undefined || field.path.startsWith(excludePrefix) === false,
          )
          .filter(fieldVisible)
        return h(
          'div',
          { style: S.grid },
          rows.map((field) => renderControl(field)),
        )
      }

      const kv = (pairs) =>
        h(
          'div',
          { style: S.kv },
          pairs.map(([key, value], index) =>
            h(
              'div',
              { key: `k${index}`, style: { display: 'contents' } },
              h('span', { style: S.kvKey }, key),
              h('span', { style: S.kvVal }, value),
            ),
          ),
        )

      /* ---------------------------------- tabs ---------------------------------- */

      const tabs = [
        ['overview', i18n.t('tab.overview')],
        ['engine', i18n.t('tab.engine')],
        ['paths', i18n.t('tab.paths')],
        ['selection', i18n.t('tab.selection')],
        ['prompts', i18n.t('tab.prompts')],
        ['quality', i18n.t('tab.quality')],
        ['schedule', i18n.t('tab.schedule')],
        ['diagnose', i18n.t('tab.diagnose')],
      ]

      const tabBar = h(
        'div',
        { style: S.tabs, role: 'tablist' },
        tabs.map(([id, label]) =>
          h(
            'button',
            {
              key: id,
              type: 'button',
              role: 'tab',
              className: tab === id ? 'ss-tab ss-tab-active' : 'ss-tab',
              'aria-selected': tab === id,
              style: S.tab(tab === id),
              onClick: () => switchTab(id),
            },
            label,
          ),
        ),
      )

      const statusChip = () => {
        if (running) return h(Chip, { tone: 'warn' }, `${i18n.t('ov.currentRun')} · ${currentRun ? currentRun.kind : ''}`)
        if (lastRun === null || lastRun === undefined) return h(Chip, { tone: 'idle' }, i18n.t('ov.none'))
        return h(Chip, { tone: lastRun.exitCode === 0 ? 'ok' : 'err' }, `exit ${lastRun.exitCode === null ? '—' : lastRun.exitCode}`)
      }

      const overviewTab = h(
        React.Fragment,
        null,
        h(
          Card,
          { title: i18n.t('ov.status'), actions: statusChip() },
          h(
            'div',
            null,
            kv([
              [
                i18n.t('ov.enginePath'),
                h('span', { style: { display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' } }, h('code', { style: S.code }, data.enginePath), h(Chip, { tone: engineReady ? 'ok' : 'err' }, engineReady ? i18n.t('ov.ready') : i18n.t('ov.missing'))),
              ],
              [
                i18n.t('ov.configPath'),
                h('span', { style: { display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' } }, h('code', { style: S.code }, data.configPath), h(Chip, { tone: data.exists ? 'ok' : 'idle' }, data.exists ? i18n.t('ov.exists') : i18n.t('ov.absent'))),
              ],
              [i18n.t('ov.dataDir'), h('code', { style: S.code }, data.dataDir)],
              [i18n.t('ov.models'), h(
                'span',
                { style: { display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' } },
                i18n.t(`ov.modelSource.${data.models && data.models.source ? data.models.source : 'none'}`),
                data.models && data.models.error
                  ? h(Chip, { tone: 'warn' }, String(data.models.error).slice(0, 120))
                  : null,
                data.models && !data.models.error && (data.models.groups ?? []).length === 0
                  ? h(Chip, { tone: 'warn' }, i18n.t('ov.noModelsYet'))
                  : null,
              )],
            ]),
          ),
          engineReady ? null : h(Banner, { tone: 'warn' }, i18n.t('msg.engineMissing')),
          data.parseError !== null && data.parseError !== undefined ? h(Banner, { tone: 'err' }, i18n.t('msg.configMalformed')) : null,
          data.exists === false ? h(Banner, { tone: 'warn' }, i18n.t('msg.configMissing')) : null,
        ),
        (() => {
          const sc = data.selfcheck
          const verdict = sc && sc.verdict ? sc.verdict : null
          const tone = verdict === 'green' ? 'ok' : verdict === 'degraded' ? 'warn' : verdict !== null ? 'err' : 'idle'
          const checks = sc && Array.isArray(sc.checks) ? sc.checks : []
          const bad = checks.filter((check) => check.level === 'error' || check.level === 'warn').slice(0, 8)
          return h(
            Card,
            {
              title: i18n.t('ov.selfcheck'),
              desc: i18n.t('ov.selfcheckHint'),
              actions: h(Btn, { onClick: recheckDeps, disabled: busy !== '' }, i18n.t('act.recheck')),
            },
            sc === null || sc === undefined
              ? h('div', { style: S.hint }, i18n.t('ov.selfcheckNone'))
              : h(
                  React.Fragment,
                  null,
                  kv([
                    [i18n.t('ov.scVerdict'), h(Chip, { tone }, i18n.t(`o.verdict.${verdict || 'unknown'}`))],
                    [i18n.t('ov.scChecked'), humanTime(sc.at)],
                    [
                      i18n.t('ov.scSummary'),
                      `${(sc.summary && sc.summary.ok) || 0} ok · ${(sc.summary && sc.summary.warn) || 0} warn · ${(sc.summary && sc.summary.error) || 0} error`,
                    ],
                  ]),
                  bad.length === 0
                    ? h('div', { style: { ...S.hint, color: T.success } }, i18n.t('ov.scAllGood'))
                    : h(
                        'ul',
                        { style: { margin: '2px 0 0', paddingLeft: 18, color: T.secondary, lineHeight: 1.6 } },
                        bad.map((check) => {
                          const hint = check.detail && check.detail.hint ? `（${check.detail.hint}）` : ''
                          return h(
                            'li',
                            { key: `${check.group || ''}:${check.name}`, style: { color: check.level === 'error' ? T.error : T.warn } },
                            h('code', { style: S.code }, check.name || '—'),
                            ` ${check.message || ''}${hint}`,
                          )
                        }),
                      ),
                ),
          )
        })(),
        h(
          Card,
          {
            title: i18n.t('ov.run'),
            actions: h(
              React.Fragment,
              null,
              h(Btn, { onClick: runPlan, disabled: busy !== '' || !engineReady }, i18n.t('act.plan')),
              h(Btn, { primary: true, onClick: startRun, disabled: busy !== '' || running || !engineReady }, i18n.t('act.run')),
              running ? h(Btn, { onClick: cancelRun, disabled: busy !== '' }, i18n.t('act.cancel')) : null,
              h(Btn, { onClick: runSelftest, disabled: busy !== '' || running || !engineReady }, i18n.t('act.selftest')),
              h(Btn, { onClick: runDoctor, disabled: busy !== '' || !engineReady }, i18n.t('act.doctor')),
            ),
          },
          h(
            'div',
            { style: { display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-end' } },
            h(
              'div',
              { style: { width: 150 } },
              h(Field, { field: { path: 'run.max' } }, h(NumberInput, { value: maxOverride, min: 0, max: 1000, step: 1, onChange: setMaxOverride })),
            ),
            h(
              'label',
              { style: { display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: T.secondary, paddingBottom: 4 } },
              h(Switch, { checked: dryRun, onChange: setDryRun, label: i18n.t('run.dryRun') }),
              i18n.t('run.dryRun'),
            ),
          ),
          h('div', { style: S.hint }, i18n.t('run.maxHint')),
          running && currentRun !== null
            ? h(
                'div',
                null,
                kv([
                  [i18n.t('run.id'), h('code', { style: S.code }, currentRun.runId)],
                  [i18n.t('run.startedAt'), humanTime(currentRun.startedAt)],
                  [i18n.t('run.elapsed'), humanMs(currentRun.elapsedMs)],
                ]),
                h('div', { style: { ...S.label, marginTop: 8, marginBottom: 4 } }, i18n.t('run.tail')),
                h('pre', { style: { ...S.pre, maxHeight: 180 } }, (status.lines || []).slice(-40).join('\n') || '—'),
              )
            : lastRun !== null && lastRun !== undefined
              ? h(
                  'div',
                  null,
                  kv([
                    [i18n.t('run.id'), h('code', { style: S.code }, lastRun.runId)],
                    [i18n.t('run.exit'), String(lastRun.exitCode)],
                    [i18n.t('run.elapsed'), humanMs(lastRun.elapsedMs)],
                    [i18n.t('run.finishedAt'), humanTime(lastRun.finishedAt)],
                    ...(lastRun.result && lastRun.result.data && Array.isArray(lastRun.result.data.processed)
                      ? [[i18n.t('run.processed'), lastRun.result.data.processed.join('\n')]]
                      : []),
                    ...(lastRun.error ? [[i18n.t('run.error'), String(lastRun.error)]] : []),
                  ]),
                )
              : h('div', { style: S.hint }, i18n.t('ov.none')),
        ),
        h(
          Card,
          { title: i18n.t('ov.schedule') },
          kv([
            [i18n.t('sch.mode'), i18n.t(`o.${draft.schedule.mode}`)],
            [i18n.t('sch.time'), draft.schedule.time],
            [i18n.t('sch.plist'), h('code', { style: S.code }, data.schedule.plistPath)],
            [
              i18n.t('sch.state'),
              h(Chip, { tone: data.schedule.launchdLoaded ? 'ok' : 'idle' }, data.schedule.launchdLoaded ? i18n.t('sch.loaded') : i18n.t('sch.notLoaded')),
            ],
            [i18n.t('ov.nextRun'), humanTime(data.schedule.nextRun)],
          ]),
        ),
        h(
          Card,
          { title: i18n.t('ov.maintenance'), desc: i18n.t('ov.resetHint') },
          h(
            'div',
            { style: S.actions },
            h(Btn, { onClick: restoreDefaults, disabled: busy !== '' }, i18n.t('act.defaults')),
            h(Btn, { onClick: () => switchTab('diagnose') }, i18n.t('act.logs')),
          ),
        ),
      )

      /* ------------------------------ model picker ------------------------------ */

      const MANUAL_MODEL = '__manual__'
      const modelGroups = (Array.isArray(data.models && data.models.groups) ? data.models.groups : []).filter(
        (group) => Array.isArray(group.models) && group.models.length > 0,
      )
      const engineKind = String(getAt(draft, 'engine.kind') ?? '')
      const providerValue = String(getAt(draft, 'engine.provider') ?? '')
      const modelValue = String(getAt(draft, 'engine.model') ?? '')
      const ollamaHostValue = String(getAt(draft, 'engine.ollamaHost') ?? '')
      const legacyEngine = !['dsh', 'ollama'].includes(engineKind)
      const engineFieldByPath = (path) =>
        groupFields('engine').find((field) => field.path === path) || { path }
      // 下拉里的一项 = (引擎, 提供方, 模型) 三件事，所以选完就都能推导出来，不必再逐个填。
      const unifiedValue =
        manualEntry === true
          ? MANUAL_MODEL
          : engineKind === 'ollama'
          ? `ollama::${modelValue}`
          : engineKind === 'dsh' && providerValue !== '' && modelGroups.some((group) => String(group.provider) === providerValue && group.models.includes(modelValue))
            ? `dsh::${providerValue}::${modelValue}`
            : MANUAL_MODEL

      const setValues = (updates) =>
        setDraft((previous) => {
          let next = previous
          for (const [path, value] of Object.entries(updates)) next = setAt(next, path, value)
          return next
        })

      /** 选一个模型 = 同时定下引擎类型；只有 Ollama 还需要一个地址。 */
      const pickSource = (next) => {
        setModelTest(null)
        if (next === MANUAL_MODEL) {
          setEngineAdvanced(true)
          setManualEntry(true)
          return
        }
        setManualEntry(false)
        if (next.startsWith('ollama::')) {
          setValues({
            'engine.kind': 'ollama',
            'engine.provider': '',
            'engine.model': next.slice('ollama::'.length),
            'engine.ollamaHost': ollamaHostValue === '' ? 'http://127.0.0.1:11434' : ollamaHostValue,
          })
          return
        }
        const [, provider, model] = next.split('::')
        setValues({ 'engine.kind': 'dsh', 'engine.provider': provider, 'engine.model': model })
      }

      /** 走哪条路就验哪条路：DSH 用宿主中转，Ollama 直连本机守护进程。 */
      const testModel = async () => {
        setModelTesting(true)
        setModelTest(null)
        const response = await postJson('model-test', {
          kind: engineKind,
          provider: providerValue,
          model: modelValue,
          host: ollamaHostValue,
        })
        setModelTesting(false)
        if (response.status === 0) {
          setModelTest({ ok: false, message: String((response.body && response.body.error) || '网络不可达') })
          return
        }
        if (response.body && response.body.ok === true) {
          const text = String((response.body.data && response.body.data.text) || '').trim()
          setModelTest({ ok: true, message: `${i18n.t('msg.modelTestOk')}${text === '' ? '' : `：${text.slice(0, 80)}`}` })
          return
        }
        setModelTest({
          ok: false,
          message: `${i18n.t('msg.modelTestFail')}：${String((response.body && (response.body.error || response.body.message)) || `HTTP ${response.status}`)}`,
        })
      }

      const testReady = modelValue !== '' && (engineKind !== 'dsh' || providerValue !== '')

      const engineTab = h(
        Card,
        { title: i18n.t('g.engine'), desc: i18n.t('g.engineHint') },
        h(
          'div',
          { style: S.grid },
          h(
            Field,
            { field: { path: 'engine.model' }, hint: i18n.t('h.engine.model'), error: fieldError('engine.model') },
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
              h(
                'select',
                {
                  style: { ...S.control, appearance: 'auto' },
                  value: unifiedValue,
                  spellCheck: false,
                  onChange: (event) => pickSource(event.target.value),
                },
                modelGroups.map((group) =>
                  h(
                    'optgroup',
                    { key: `dsh:${String(group.provider)}`, label: `${i18n.t('o.groupDsh')} · ${String(group.provider)}` },
                    group.models.map((model) =>
                      h('option', { key: `dsh::${group.provider}::${model}`, value: `dsh::${group.provider}::${model}` }, model),
                    ),
                  ),
                ),
                ollamaModels.length > 0
                  ? h(
                      'optgroup',
                      { key: 'ollama', label: i18n.t('o.groupOllama') },
                      ollamaModels.map((model) =>
                        h('option', { key: `ollama::${model}`, value: `ollama::${model}` }, model),
                      ),
                    )
                  : null,
                h('option', { key: MANUAL_MODEL, value: MANUAL_MODEL }, i18n.t('o.manualModel')),
              ),
              unifiedValue === MANUAL_MODEL
                ? h(TextInput, {
                    value: modelValue,
                    placeholder: i18n.t('o.manualPlaceholder'),
                    onChange: (next) => {
                      setValue('engine.model', next)
                      setModelTest(null)
                    },
                  })
                : null,
              engineKind === 'ollama'
                ? h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
                    h(
                      Field,
                      {
                        field: engineFieldByPath('engine.ollamaHost'),
                        hint: i18n.t('h.engine.ollamaHost'),
                        error: fieldError('engine.ollamaHost'),
                      },
                      h(TextInput, {
                        value: ollamaHostValue,
                        placeholder: 'http://127.0.0.1:11434',
                        onChange: (next) => {
                          setValue('engine.ollamaHost', next)
                          setModelTest(null)
                        },
                      }),
                    ),
                    h(
                      'div',
                      { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
                      h(
                        Btn,
                        { onClick: () => refreshOllama(ollamaHostValue), disabled: ollamaLoading === true },
                        ollamaLoading === true ? `${i18n.t('act.refreshOllama')}…` : i18n.t('act.refreshOllama'),
                      ),
                      ollamaNote === ''
                        ? h('span', { style: S.hint }, `${i18n.t('ov.ollamaCount')}：${ollamaModels.length}`)
                        : h(Chip, { tone: 'warn' }, ollamaNote),
                    ),

                    /*
                     * config.json keeps keys the panel never shows, so a stale
                     * `engine.ollamaModel` used to silently outrank whatever the
                     * user picked here. Show it, and let them get rid of it.
                     */
                    String(getAt(draft, 'engine.ollamaModel') ?? '') === ''
                      ? null
                      : h(
                          'div',
                          { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
                          h(
                            Chip,
                            { tone: 'warn' },
                            `${i18n.t('ov.ollamaOverride')}：${String(getAt(draft, 'engine.ollamaModel'))}`,
                          ),
                          h(
                            Btn,
                            { onClick: () => setValues({ 'engine.ollamaModel': '' }) },
                            i18n.t('act.clearOverride'),
                          ),
                        ),                  )
                : null,
              engineKind === 'dsh' && providerValue === ''
                ? h(Chip, { tone: 'warn' }, i18n.t('h.engine.provider'))
                : null,
              h(
                'div',
                { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' } },
                h(
                  Btn,
                  {
                    onClick: testModel,
                    disabled: modelTesting === true || testReady === false,
                    title: testReady === false ? i18n.t('h.engine.model') : undefined,
                  },
                  modelTesting === true ? `${i18n.t('act.testModel')}…` : i18n.t('act.testModel'),
                ),
                modelTest === null ? null : h(Chip, { tone: modelTest.ok === true ? 'ok' : 'err' }, modelTest.message),
              ),
            ),
          ),
          ...['engine.temperature', 'engine.seed', 'engine.maxTokens', 'engine.batchSize']
            .map(engineFieldByPath)
            .map((field) => renderControl(field)),
        ),
        engineKind === 'dsh'
          ? h(
              'div',
              { style: S.grid },
              h(
                'div',
                { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                h('div', { style: S.labelRow }, h('span', { style: S.label }, i18n.t('ov.bridge'))),
                kv([
                  [
                    i18n.t('ov.bridge'),
                    h(
                      Chip,
                      { tone: data.bridge && data.bridge.ready === true ? 'ok' : 'warn' },
                      data.bridge && data.bridge.ready === true ? i18n.t('ov.bridgeReady') : i18n.t('ov.bridgeMissing'),
                    ),
                  ],
                  [i18n.t('ov.bridgeUrl'), h('code', { style: S.code }, (data.bridge && data.bridge.baseUrl) || '—')],
                ]),
              ),
            )
          : null,
        h(
          'details',
          {
            open: engineAdvanced || legacyEngine,
            onToggle: (event) => setEngineAdvanced(event.target.open),
            style: { display: 'flex', flexDirection: 'column', gap: 8 },
          },
          h('summary', { style: { ...S.label, cursor: 'pointer' } }, i18n.t('g.engineAdvanced')),
          h('div', { style: S.hint }, i18n.t('h.engineAdvanced')),
          h(
            'div',
            { style: S.grid },
            renderControl(engineFieldByPath('engine.kind')),
            engineKind === 'dsh' || legacyEngine
              ? h(
                  Field,
                  { field: engineFieldByPath('engine.provider'), hint: i18n.t('h.engine.provider'), error: fieldError('engine.provider') },
                  h(TextInput, {
                    value: providerValue,
                    placeholder: 'openai',
                    onChange: (next) => {
                      setValue('engine.provider', next)
                      setModelTest(null)
                    },
                  }),
                )
              : null,
            engineKind === 'deepseek' || engineKind === 'openai-compatible'
              ? renderControl(engineFieldByPath('engine.baseUrl'))
              : null,
            legacyEngine ? renderControl(engineFieldByPath('engine.apiKeyRef')) : null,
          ),
        ),
        h('div', { style: { ...S.label, marginTop: 4 } }, i18n.t('g.engineWhere')),
        h('div', { style: S.hint }, i18n.t('g.engineWhereHint')),
        kv([
          [i18n.t('ov.engineSource'), h(Chip, { tone: data.enginePathSource === 'config' ? 'warn' : 'ok' }, i18n.t(`o.src.${data.enginePathSource || 'bundled'}`))],
          [i18n.t('ov.enginePath'), h('code', { style: S.code }, data.enginePath || '—')],
          [i18n.t('ov.engineBundled'), h('code', { style: S.code }, data.engineBundlePath || '—')],
        ]),
        h(
          'div',
          { style: S.grid },
          h(
            Field,
            { field: { path: 'paths.engine' }, hint: i18n.t('h.paths.engine'), error: fieldError('paths.engine') },
            h(
              'div',
              { style: { display: 'flex', gap: 8, alignItems: 'center' } },
              h(TextInput, {
                value: getAt(draft, 'paths.engine') || '',
                placeholder: data.engineBundlePath || '',
                onChange: (next) => setValue('paths.engine', next),
              }),
              h(Btn, { onClick: () => setValue('paths.engine', ''), disabled: !getAt(draft, 'paths.engine') }, i18n.t('act.useBundled')),
            ),
          ),
        ),
      )

      /*
       * Multi-directory editor: each entry is one source/output pair. The
       * top-level source/output below stay as the template for new entries and
       * keep working on their own when the list is empty.
       */
      const dirEntries = Array.isArray(getAt(draft, 'dirs')) ? getAt(draft, 'dirs') : []
      const setDirs = (next) => setValue('dirs', next)
      const blankDir = () => ({
        name: '',
        source: {
          kind: getAt(draft, 'source.kind') || 'local',
          path: '',
          include: getAt(draft, 'source.include') || ['*.mp4'],
          recursive: getAt(draft, 'source.recursive') === true,
          ssh: { host: '', port: 22, user: '', keyPath: '' },
        },
        output: {
          kind: getAt(draft, 'output.kind') || 'local',
          path: '',
          nameTemplate: getAt(draft, 'output.nameTemplate') || '{stem}_zh.mp4',
          overwrite: getAt(draft, 'output.overwrite') === true,
          ssh: { host: '', port: 22, user: '', keyPath: '' },
        },
      })
      const updateDir = (index, part, patch) =>
        setDirs(
          dirEntries.map((entry, position) =>
            position === index ? { ...entry, [part]: { ...(entry[part] || {}), ...patch } } : entry,
          ),
        )
      const updateDirField = (index, key, value) =>
        setDirs(dirEntries.map((entry, position) => (position === index ? { ...entry, [key]: value } : entry)))
      const dirRow = (entry, index) => {
        const source = entry.source || {}
        const output = entry.output || {}
        const sourceSsh = source.ssh || {}
        const outputSsh = output.ssh || {}
        const includeText = (source.include || []).join(', ')
        return h(
          'div',
          {
            key: `dir-${index}`,
            style: {
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
              padding: '10px 12px',
              border: `1px solid ${T.border}`,
              borderRadius: 9,
              background: T.field,
            },
          },
          h(
            'div',
            { style: { display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' } },
            h(
              'div',
              { style: { flex: '1 1 220px', display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.dir.name')),
              h(TextInput, {
                value: entry.name || '',
                placeholder: `${i18n.t('f.dir.name')} ${index + 1}`,
                onChange: (next) => updateDirField(index, 'name', next),
              }),
            ),
            h(
              'div',
              { style: { flex: '0 0 auto' } },
              h(Btn, { onClick: () => setDirs(dirEntries.filter((_entry, position) => position !== index)) }, i18n.t('act.removeDir')),
            ),
          ),
          h('div', { style: { ...S.label, marginTop: 2 } }, i18n.t('f.dir.source')),
          h(
            'div',
            { style: S.grid },
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.dir.kind')),
              h(Select, {
                value: source.kind || 'local',
                options: ['local', 'ssh'],
                onChange: (next) => updateDir(index, 'source', { kind: next }),
              }),
            ),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.source.path')),
              h(PathPicker, {
                value: source.path || '',
                onChange: (next) => updateDir(index, 'source', { path: next }),
                remote: source.kind === 'ssh',
              }),
            ),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.dir.include')),
              h(TextInput, {
                value: includeText,
                placeholder: '*.mp4, *.mkv',
                onChange: (next) =>
                  updateDir(index, 'source', {
                    include: next
                      .split(',')
                      .map((item) => item.trim())
                      .filter((item) => item !== ''),
                  }),
              }),
            ),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3, justifyContent: 'center' } },
              h('div', { style: S.label }, i18n.t('f.dir.recursive')),
              h(Switch, {
                checked: source.recursive === true,
                onChange: (next) => updateDir(index, 'source', { recursive: next }),
              }),
            ),
          ),
          source.kind === 'ssh'
            ? h(
                'div',
                { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
                h('div', { style: S.label }, i18n.t('f.dir.ssh')),
                h(
                  'div',
                  { style: S.grid },
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.source.ssh.host')),
                    h(TextInput, { value: sourceSsh.host || '', onChange: (next) => updateDir(index, 'source', { ssh: { ...sourceSsh, host: next } }) }),
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.source.ssh.port')),
                    h(TextInput, { value: sourceSsh.port === undefined ? 22 : sourceSsh.port, onChange: (next) => updateDir(index, 'source', { ssh: { ...sourceSsh, port: Number(next) || 22 } }) }),
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.source.ssh.user')),
                    h(TextInput, { value: sourceSsh.user || '', onChange: (next) => updateDir(index, 'source', { ssh: { ...sourceSsh, user: next } }) }),
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.source.ssh.keyPath')),
                    h(TextInput, { value: sourceSsh.keyPath || '', onChange: (next) => updateDir(index, 'source', { ssh: { ...sourceSsh, keyPath: next } }) }),
                  ),
                ),
              )
            : null,
          h('div', { style: { ...S.label, marginTop: 2 } }, i18n.t('f.dir.output')),
          h(
            'div',
            { style: S.grid },
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.dir.kind')),
              h(Select, {
                value: output.kind || 'local',
                options: ['local', 'ssh'],
                onChange: (next) => updateDir(index, 'output', { kind: next }),
              }),
            ),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.output.path')),
              h(PathPicker, {
                value: output.path || '',
                onChange: (next) => updateDir(index, 'output', { path: next }),
                remote: output.kind === 'ssh',
              }),
            ),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
              h('div', { style: S.label }, i18n.t('f.dir.nameTemplate')),
              h(TextInput, {
                value: output.nameTemplate || '{stem}_zh.mp4',
                onChange: (next) => updateDir(index, 'output', { nameTemplate: next }),
              }),
            ),
            h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 3, justifyContent: 'center' } },
              h('div', { style: S.label }, i18n.t('f.dir.overwrite')),
              h(Switch, {
                checked: output.overwrite === true,
                onChange: (next) => updateDir(index, 'output', { overwrite: next }),
              }),
            ),
          ),
          output.kind === 'ssh'
            ? h(
                'div',
                { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
                h('div', { style: S.label }, `${i18n.t('f.dir.output')} · ${i18n.t('f.dir.ssh')}`),
                h(
                  'div',
                  { style: S.grid },
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.output.ssh.host')),
                    h(TextInput, { value: outputSsh.host || '', onChange: (next) => updateDir(index, 'output', { ssh: { ...outputSsh, host: next } }) }),
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.output.ssh.port')),
                    h(TextInput, { value: outputSsh.port === undefined ? 22 : outputSsh.port, onChange: (next) => updateDir(index, 'output', { ssh: { ...outputSsh, port: Number(next) || 22 } }) }),
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.output.ssh.user')),
                    h(TextInput, { value: outputSsh.user || '', onChange: (next) => updateDir(index, 'output', { ssh: { ...outputSsh, user: next } }) }),
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 3 } },
                    h('div', { style: S.label }, i18n.t('f.output.ssh.keyPath')),
                    h(TextInput, { value: outputSsh.keyPath || '', onChange: (next) => updateDir(index, 'output', { ssh: { ...outputSsh, keyPath: next } }) }),
                  ),
                ),
              )
            : null,
        )
      }

      const dirsCard = h(
        Card,
        {
          title: i18n.t('g.dirs'),
          desc: i18n.t('g.dirsHint'),
          actions: h(Btn, { onClick: () => setDirs([...dirEntries, blankDir()]) }, i18n.t('act.addDir')),
        },
        dirEntries.length === 0
          ? h('div', { style: S.hint }, i18n.t('o.dir.empty'))
          : h(
              'div',
              { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
              dirEntries.map((entry, index) => dirRow(entry, index)),
            ),
        h('div', { style: S.hint }, i18n.t('o.dir.defaultHint')),
      )

      /*
       * SSH only matters when that endpoint actually IS ssh — the engine reads
       * source.ssh.* / output.ssh.* exclusively on the ssh path (see
       * engine/lib/remote.py). Showing four empty boxes under a local folder is
       * noise that also implies the user has to fill them, so gate the whole
       * group on the kind, exactly like the per-directory rows do.
       */
      const sshGroup = (prefix) =>
        getAt(draft, `${prefix}.kind`) === 'ssh'
          ? h(
              React.Fragment,
              null,
              h('div', { style: { ...S.label, marginTop: 4 } }, i18n.t('g.ssh')),
              h(
                'div',
                { style: S.grid },
                [`${prefix}.ssh.host`, `${prefix}.ssh.port`, `${prefix}.ssh.user`, `${prefix}.ssh.keyPath`].map((path) => {
                  const field = fields.find((entry) => entry.path === path)
                  return field === undefined ? null : renderControl(field)
                }),
              ),
            )
          : null

      const pathsTab = h(
        React.Fragment,
        null,
        dirsCard,
        h(
          Card,
          { title: i18n.t('g.source'), desc: i18n.t('g.sourceHint') },
          h(
            'div',
            { style: S.grid },
            ['source.kind', 'source.path', 'source.include', 'source.recursive'].map((path) => {
              const field = fields.find((entry) => entry.path === path)
              return field === undefined ? null : renderControl(field)
            }),
          ),
          sshGroup('source'),
        ),
        h(
          Card,
          { title: i18n.t('g.output') },
          h(
            'div',
            { style: S.grid },
            ['output.mode', 'output.kind', 'output.path', 'output.nameTemplate', 'output.overwrite'].map((path) => {
              const field = fields.find((entry) => entry.path === path)
              return field === undefined ? null : renderControl(field)
            }),
          ),
          sshGroup('output'),
        ),
      )

      /*
       * Plan payloads differ between engine builds: the shipped engine marks
       * every candidate with `selected` plus a `reason`, while the plain
       * contract returns a separate `skipped` list. Normalize both so the
       * table shows what would actually run and why the rest is skipped.
       */
      const planRows = plan === null || plan === undefined || Array.isArray(plan.candidates) === false ? [] : plan.candidates
      const planSelected = planRows.filter((candidate) => candidate.selected !== false)
      const planSkipped = Array.isArray(plan && plan.skipped)
        ? plan.skipped
        : planRows
            .filter((candidate) => candidate.selected === false)
            .map((candidate) => ({
              name: candidate.name,
              reason: candidate.reasonText || candidate.reason,
              dirName: candidate.dirName,
              sourceDir: candidate.sourceDir,
            }))
      // Multi-directory plans carry the owning directory on every candidate.
      const planDirs = Array.isArray(plan && plan.dirs) ? plan.dirs : []
      const showDirColumn = planDirs.length > 1
      const dirLabel = (candidate) => {
        if (candidate === null || candidate === undefined) return '—'
        if (typeof candidate.dirName === 'string' && candidate.dirName !== '') return candidate.dirName
        if (typeof candidate.sourceDir === 'string' && candidate.sourceDir !== '') return candidate.sourceDir
        return '—'
      }
      const planUnreachable = Array.isArray(plan && plan.unreachableDirs) ? plan.unreachableDirs : []

      const selectionTab = h(
        React.Fragment,
        null,
        h(Card, { title: i18n.t('tab.selection'), desc: i18n.t('g.selectionHint') }, renderGroup('selection')),
        h(
          Card,
          {
            title: i18n.t('plan.title'),
            actions: h(Btn, { onClick: runPlan, disabled: busy !== '' || !engineReady }, i18n.t('act.plan')),
          },
          plan === null
            ? h('div', { style: S.hint }, i18n.t('plan.needRun'))
            : h(
                React.Fragment,
                null,
                plan.error ? h(Banner, { tone: 'err' }, String(plan.error)) : null,
                planUnreachable.length > 0
                  ? h(
                      Banner,
                      { tone: 'warn' },
                      `${i18n.t('plan.unreachable')}：${planUnreachable.join('、')}`,
                    )
                  : null,
                showDirColumn
                  ? h(
                      'div',
                      { style: S.label },
                      `${i18n.t('plan.dirs')}：${planDirs
                        .map((entry) => {
                          const count = entry.counts && typeof entry.counts.selected === 'number' ? entry.counts.selected : 0
                          return `${entry.name || '—'}（${count}）`
                        })
                        .join('、')}`,
                    )
                  : null,
                h('div', { style: S.label }, `${i18n.t('plan.candidates')} · ${planSelected.length}`),
                h(
                  'table',
                  { style: S.table },
                  h(
                    'thead',
                    null,
                    h(
                      'tr',
                      null,
                      (showDirColumn
                        ? [i18n.t('col.dir'), i18n.t('col.name'), i18n.t('col.size'), i18n.t('col.profile'), i18n.t('col.time')]
                        : [i18n.t('col.name'), i18n.t('col.size'), i18n.t('col.profile'), i18n.t('col.time')]
                      ).map((label) => h('th', { key: label, style: S.th }, label)),
                    ),
                  ),
                  h(
                    'tbody',
                    null,
                    planSelected.map((candidate, index) =>
                      h(
                        'tr',
                        { key: `${candidate.name}-${index}` },
                        showDirColumn ? h('td', { style: S.td }, dirLabel(candidate)) : null,
                        h('td', { style: S.td }, candidate.name),
                        h('td', { style: S.td }, humanSize(candidate.sizeBytes)),
                        h('td', { style: S.td }, candidate.profile || candidate.profileSeries || '—'),
                        h('td', { style: S.td }, humanTime(candidate.mtimeISO || candidate.mtime)),
                      ),
                    ),
                  ),
                ),
                planSelected.length === 0 ? h('div', { style: S.hint }, i18n.t('plan.empty')) : null,
                h('div', { style: { ...S.label, marginTop: 10 } }, `${i18n.t('plan.skipped')} · ${planSkipped.length}`),
                h(
                  'table',
                  { style: S.table },
                  h(
                    'thead',
                    null,
                    h(
                      'tr',
                      null,
                      (showDirColumn
                        ? [i18n.t('col.dir'), i18n.t('col.name'), i18n.t('col.reason')]
                        : [i18n.t('col.name'), i18n.t('col.reason')]
                      ).map((label) => h('th', { key: label, style: S.th }, label)),
                    ),
                  ),
                  h(
                    'tbody',
                    null,
                    planSkipped.map((entry, index) =>
                      h(
                        'tr',
                        { key: `${entry.name}-${index}` },
                        showDirColumn ? h('td', { style: S.td }, dirLabel(entry)) : null,
                        h('td', { style: S.td }, entry.name),
                        h('td', { style: S.td }, entry.reason || '—'),
                      ),
                    ),
                  ),
                ),
              ),
        ),
      )

      const profiles = getAt(draft, 'prompts.profiles') || []
      const moveProfile = (index, delta) => {
        const next = profiles.slice()
        const target = index + delta
        if (target < 0 || target >= next.length) return
        const [item] = next.splice(index, 1)
        next.splice(target, 0, item)
        setValue('prompts.profiles', next)
      }
      const updateProfile = (index, patch) => {
        const next = profiles.map((profile, position) => (position === index ? { ...profile, ...patch } : profile))
        setValue('prompts.profiles', next)
      }

      const promptsTab = h(
        React.Fragment,
        null,
        h(Card, { title: i18n.t('g.default') }, renderGroup('prompts')),
        h(
          Card,
          {
            title: i18n.t('g.profiles'),
            desc: i18n.t('g.promptsHint'),
            actions: h(
              Btn,
              {
                onClick: () => setValue('prompts.profiles', profiles.concat([{ name: `profile-${profiles.length + 1}`, match: [], system: '', context: '', series: '' }])),
              },
              i18n.t('act.addProfile'),
            ),
          },
          profiles.length === 0 ? h('div', { style: S.hint }, i18n.t('g.profileEmpty')) : null,
          profiles.map((profile, index) =>
            h(
              'div',
              { key: index, style: { border: `1px solid ${T.border}`, borderRadius: 8, padding: 10, display: 'flex', flexDirection: 'column', gap: 8 } },
              h(
                'div',
                { style: { display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' } },
                h(
                  'div',
                  { style: { width: 200 } },
                  h(Field, { field: { path: `prompts.profiles.${index}.name` }, labelKey: 'g.profileName', error: fieldError(`prompts.profiles.${index}.name`) }, h(TextInput, { value: profile.name, onChange: (next) => updateProfile(index, { name: next }) })),
                ),
                h(
                  'div',
                  { style: { flex: '1 1 200px' } },
                  h(Field, { field: { path: `prompts.profiles.${index}.match` }, labelKey: 'g.profileMatch', hint: i18n.t('g.profileMatchHint') }, h(TextInput, {
                    value: Array.isArray(profile.match) ? profile.match.join(', ') : '',
                    onChange: (next) =>
                      updateProfile(index, {
                        match: next
                          .split(',')
                          .map((part) => part.trim())
                          .filter((part) => part !== ''),
                      }),
                  })),
                ),
                h(
                  'div',
                  { style: { width: 130 } },
                  h(Field, { field: { path: `prompts.profiles.${index}.series` }, labelKey: 'g.profileSeries', hint: i18n.t('h.profileSeries') }, h(TextInput, { value: profile.series, placeholder: 'example', onChange: (next) => updateProfile(index, { series: next }) })),
                ),
                h(
                  'div',
                  { style: { display: 'flex', gap: 4, paddingBottom: 2 } },
                  h(Btn, { onClick: () => moveProfile(index, -1), disabled: index === 0, title: i18n.t('act.moveUp') }, '↑'),
                  h(Btn, { onClick: () => moveProfile(index, 1), disabled: index === profiles.length - 1, title: i18n.t('act.moveDown') }, '↓'),
                  h(Btn, { onClick: () => setValue('prompts.profiles', profiles.filter((_, position) => position !== index)) }, i18n.t('act.remove')),
                ),
              ),
              h(
                'div',
                { style: S.grid },
                h(Field, { field: { path: `prompts.profiles.${index}.system` }, labelKey: 'g.profileSystem' }, h(TextArea, { value: profile.system, rows: 3, onChange: (next) => updateProfile(index, { system: next }) })),
                h(Field, { field: { path: `prompts.profiles.${index}.context` }, labelKey: 'g.profileContext' }, h(TextArea, { value: profile.context, rows: 3, onChange: (next) => updateProfile(index, { context: next }) })),
              ),
            ),
          ),
        ),
      )

      /* ---------------------------- 通知：渠道分支渲染 ---------------------------- */

      /** 从 describe 的 fields 里取一个字段交给 renderControl；缺失时静默跳过。 */
      const notifyField = (path) => {
        const field = fields.find((entry) => entry.path === path)
        return field === undefined ? null : renderControl(field)
      }

      const notifyChannel = getAt(draft, 'notify.channel')
      const notifySecret = (data === null ? null : data.notifySecret) || {}
      const qqSendAvailable = data !== null && data.notifyQqSendAvailable === true
      const secretReady = notifySecret.configured === true
      const secretFromEnv = secretReady && notifySecret.source === 'env'
      const secretStateText = secretFromEnv
        ? 'notify.secretStateEnv'
        : secretReady
          ? 'notify.secretStateSaved'
          : 'notify.secretStateMissing'
      const notifyBusy = busy !== ''
      const qqAppId = getAt(draft, 'notify.qqAppId')
      const qqOpenId = getAt(draft, 'notify.qqOpenId')

      // QQ Bot：手填 AppID + 接收人 openid，secret 只写钥匙串（永不回显）。
      const qqbotBranch = h(
        React.Fragment,
        null,
        h(
          'div',
          { style: S.grid },
          h(
            Field,
            { field: { path: 'notify.qqAppId' }, hint: i18n.t('h.notify.qqAppId') },
            h(TextInput, {
              value: qqAppId === undefined || qqAppId === null ? '' : qqAppId,
              placeholder: '1029xxxx',
              onChange: (next) => setValue('notify.qqAppId', next),
            }),
          ),
          h(
            Field,
            { field: { path: 'notify.qqOpenId' } },
            h('input', {
              type: 'text',
              style: { ...S.control, fontFamily: MONO },
              value: qqOpenId === undefined || qqOpenId === null ? '' : String(qqOpenId),
              placeholder: i18n.t('h.notify.qqOpenId'),
              spellCheck: false,
              onChange: (event) => setValue('notify.qqOpenId', event.target.value),
            }),
          ),
        ),
        h(
          Field,
          { field: { path: 'notify.qqSecret' }, labelKey: 'f.notify.qqSecret', hint: i18n.t('h.notify.qqSecret') },
          h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: 8 } },
            h('input', {
              type: 'password',
              autoComplete: 'new-password',
              style: { ...S.control, fontFamily: MONO },
              value: qqSecret,
              placeholder: i18n.t('notify.secretPlaceholder'),
              spellCheck: false,
              onChange: (event) => setQqSecret(event.target.value),
            }),
            h(Chip, { tone: secretReady ? 'ok' : 'warn' }, i18n.t(secretStateText)),
          ),
        ),
        h(
          'div',
          { style: S.actions },
          h(Btn, { onClick: saveQqSecret, primary: true, disabled: notifyBusy }, i18n.t('notify.saveSecret')),
          h(Btn, { onClick: sendNotifyTest, disabled: notifyBusy }, i18n.t('notify.sendTest')),
        ),
        h('div', { style: S.hint }, i18n.t('notify.testHint')),
        qqNotice === null ? null : h(Banner, { tone: qqNotice.tone }, qqNotice.text),
        h('div', { style: S.hint }, i18n.t('notify.openidHelp')),
        h(
          'div',
          { style: S.actions },
          h(Btn, { onClick: () => setOpenidOpen((previous) => !previous) }, i18n.t(openidOpen ? 'act.close' : 'notify.openidToggle')),
        ),
        openidOpen
          ? h(
              React.Fragment,
              null,
              h(TextArea, { value: openidPaste, rows: 3, onChange: setOpenidPaste }),
              h(
                'div',
                { style: S.actions },
                h(Btn, { onClick: extractQqOpenId }, i18n.t('notify.extractOpenid')),
              ),
              openidError === null ? null : h('div', { style: S.err }, openidError),
            )
          : null,
        h(
          React.Fragment,
          null,
          h(
            'div',
            { style: S.actions },
            h(
              Btn,
              { onClick: () => setQqAdvancedOpen((previous) => !previous) },
              i18n.t(qqAdvancedOpen ? 'act.close' : 'notify.advanced'),
            ),
          ),
          qqAdvancedOpen
            ? h(
                'div',
                { style: S.grid },
                qqSendAvailable ? notifyField('notify.qqSendPath') : null,
                notifyField('notify.qqSecretService'),
              )
            : null,
        ),
      )

      // command / webhook 沿用原有字段，不再把 qqSendPath 混进 command 分支。
      const notifyBranch =
        notifyChannel === 'none'
          ? h('div', { style: S.hint }, i18n.t('notify.noneHint'))
          : notifyChannel === 'qqbot'
            ? qqbotBranch
            : h(
                'div',
                { style: S.grid },
                (notifyChannel === 'command'
                  ? ['notify.command']
                  : notifyChannel === 'webhook'
                    ? ['notify.webhookUrl', 'notify.webhookBody']
                    : []
                ).map((path) => h(React.Fragment, { key: path }, notifyField(path))),
              )

      const qualityTab = h(
        React.Fragment,
        null,
        h(Card, { title: i18n.t('tab.quality'), desc: i18n.t('g.qualityHint') }, renderGroup('quality', 'notify.')),
        h(
          Card,
          { title: i18n.t('g.asrQuality'), desc: i18n.t('g.asrQualityHint') },
          h(
            'details',
            { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
            h('summary', { style: { ...S.label, cursor: 'pointer' } }, i18n.t('g.asrQualityOpen')),
            h(
              'div',
              { style: S.grid },
              [
                'transcribe.conditionOnPreviousText',
                'transcribe.temperature',
                'transcribe.hallucinationSilenceThreshold',
                'transcribe.minAvgLogprob',
                'transcribe.maxNoSpeechProb',
                'transcribe.keepThanksPhrases',
                'transcribe.keepRepeats',
              ]
                .map((path) => fields.find((entry) => entry.path === path))
                .filter((field) => field !== undefined && fieldVisible(field))
                .map((field) => renderControl(field)),
            ),
          ),
        ),
        h(
          Card,
          { title: i18n.t('g.notify') },
          h(
            'div',
            { style: S.grid },
            ['notify.channel', 'notify.onStart', 'notify.onSuccess', 'notify.onFailure']
              .map((path) => fields.find((entry) => entry.path === path))
              .filter((field) => field !== undefined && fieldVisible(field))
              .map((field) => h(React.Fragment, { key: field.path }, renderControl(field))),
          ),
          notifyBranch,
          h('div', { style: S.hint }, i18n.t('h.notifyHint')),
        ),
      )

      const schedule = data.schedule || {}
      const schedulePrompts =
        Array.isArray(schedule.prompts) && schedule.prompts.length > 0
          ? schedule.prompts
          : schedule.promptText
            ? [{ time: draft.schedule.time, role: 'primary', text: schedule.promptText }]
            : []
      const copyPrompt = async (text) => {
        try {
          await navigator.clipboard.writeText(text || '')
          setBanner({ tone: 'ok', text: i18n.t('msg.copied') })
        } catch {
          setBanner({ tone: 'warn', text: i18n.t('msg.copyFailed') })
        }
      }
      const scheduleTab = h(
        React.Fragment,
        null,
        h(
          Card,
          {
            title: i18n.t('tab.schedule'),
            desc: i18n.t(draft.schedule.mode === 'launchd' ? 'sch.launchdHint' : draft.schedule.mode === 'dsh' ? 'sch.dshHint' : 'sch.offHint'),
            actions: h(
              React.Fragment,
              null,
              h(Btn, { primary: true, onClick: applySchedule, disabled: busy !== '' }, i18n.t('act.applySchedule')),
              h(Btn, { onClick: removeSchedule, disabled: busy !== '' }, i18n.t('act.removeSchedule')),
            ),
          },
          data.schedule && data.schedule.supported === false ? h(Banner, { tone: 'warn' }, i18n.t('sch.unsupported')) : null,
          h(
            'div',
            { style: S.grid },
            ['schedule.enabled', 'schedule.mode', 'schedule.time', 'schedule.guardTimes', 'schedule.label']
              .map((path) => fields.find((entry) => entry.path === path))
              .filter((field) => field !== undefined && fieldVisible(field))
              .map((field) => renderControl(field)),
            h(
              'div',
              null,
              h(Field, { field: { path: 'paths.dataDir' }, hint: i18n.t('h.paths.dataDir') }, h(TextInput, { value: getAt(draft, 'paths.dataDir'), onChange: (next) => setValue('paths.dataDir', next) })),
            ),
          ),
        ),
        draft.schedule.mode !== 'launchd'
          ? null
          : h(
          Card,
          { title: i18n.t('sch.state') },
          kv([
            [i18n.t('sch.mode'), i18n.t(`o.${schedule.mode || draft.schedule.mode}`)],
            [i18n.t('sch.plist'), h('code', { style: S.code }, schedule.plistPath || '—')],
            [
              i18n.t('sch.loaded'),
              h(Chip, { tone: schedule.launchdLoaded ? 'ok' : 'idle' }, schedule.launchdLoaded ? i18n.t('sch.loaded') : i18n.t('sch.notLoaded')),
            ],
            [i18n.t('ov.nextRun'), humanTime(schedule.nextRun)],
            [i18n.t('run.startedAt'), humanTime(schedule.installedAt)],
          ]),
        ),
        draft.schedule.mode !== 'dsh'
          ? null
          : h(
          Card,
          { title: i18n.t('sch.prompt'), desc: i18n.t('sch.promptsDesc') },
          schedulePrompts.length === 0
            ? h(Banner, { tone: 'warn' }, i18n.t('sch.noTriggers'))
            : schedulePrompts.map((prompt, index) =>
                h(
                  'div',
                  { key: `${prompt.time}-${index}`, style: { marginBottom: index === schedulePrompts.length - 1 ? 0 : 14 } },
                  h(
                    'div',
                    { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 6 } },
                    h(
                      'strong',
                      { style: { fontSize: 13 } },
                      `${prompt.time} · ${i18n.t(prompt.role === 'extra' ? 'sch.role.extra' : 'sch.role.primary')}`,
                    ),
                    h(Btn, { onClick: () => copyPrompt(prompt.text) }, i18n.t('act.copyPrompt')),
                  ),
                  h('pre', { style: { ...S.pre, maxHeight: 180 } }, prompt.text || '—'),
                ),
              ),
        ),
      )

      /*
       * Doctor checks arrive either as the plugin contract's
       * {id,label,status,detail,hint} or as the shipped engine's
       * {name,group,level,message,detail:{hint}}. Both are normalized here so
       * the table renders identically; `ok:false` still carries the data.
       */
      const doctorEnvelope = doctor && doctor.result ? doctor.result : null
      const doctorData =
        doctorEnvelope !== null && doctorEnvelope.data && Array.isArray(doctorEnvelope.data.checks) ? doctorEnvelope.data : null
      const doctorChecks =
        doctorData === null
          ? null
          : doctorData.checks.map((check, index) => {
              const level = check.status || check.level || 'ok'
              const status = level === 'error' || level === 'fail' ? 'fail' : level === 'warn' ? 'warn' : level === 'info' ? 'info' : 'ok'
              const detail = check.detail
              return {
                id: check.id || check.name || `check-${index}`,
                label: check.label || check.name || check.id || `#${index + 1}`,
                status,
                detail: typeof detail === 'string' ? detail : check.message || '',
                hint: check.hint || (detail && typeof detail === 'object' ? detail.hint : '') || '',
              }
            })
      const doctorSummary =
        doctorData === null
          ? null
          : {
              ok: Number((doctorData.summary && doctorData.summary.ok) || 0),
              warn: Number((doctorData.summary && doctorData.summary.warn) || 0),
              fail: Number((doctorData.summary && (doctorData.summary.fail !== undefined ? doctorData.summary.fail : doctorData.summary.error)) || 0),
            }
      const diagnoseTab = h(
        React.Fragment,
        null,
        h(
          Card,
          {
            title: i18n.t('diag.doctor'),
            actions: h(
              React.Fragment,
              null,
              h(Btn, { primary: true, onClick: runDoctor, disabled: busy !== '' || !engineReady }, i18n.t('act.doctor')),
              h(Btn, { onClick: () => refreshLogs(logName, logLines) }, i18n.t('act.refresh')),
            ),
          },
          engineReady ? null : h(Banner, { tone: 'warn' }, i18n.t('diag.hintMissing')),
          doctor === null
            ? h('div', { style: S.hint }, i18n.t('diag.empty'))
            : doctorEnvelope !== null && doctorData === null && doctorEnvelope.ok === false
              ? h(
                  Banner,
                  { tone: 'err' },
                  String(
                    doctorEnvelope.error && doctorEnvelope.error.message
                      ? doctorEnvelope.error.message
                      : doctor.parseError || 'doctor failed',
                  ),
                )
              : doctorChecks === null
                ? h(Banner, { tone: 'warn' }, i18n.t('diag.empty'))
                : h(
                    React.Fragment,
                    null,
                    h(
                      'div',
                      { style: S.actions },
                      h(
                        Chip,
                        { tone: doctorSummary.fail > 0 ? 'err' : doctorSummary.warn > 0 ? 'warn' : 'ok' },
                        i18n
                          .t('diag.summary')
                          .replace('{ok}', String(doctorSummary.ok))
                          .replace('{warn}', String(doctorSummary.warn))
                          .replace('{fail}', String(doctorSummary.fail)),
                      ),
                    ),
                    h(
                      'table',
                      { style: S.table },
                      h(
                        'thead',
                        null,
                        h('tr', null, [i18n.t('col.check'), i18n.t('col.state'), i18n.t('col.detail'), i18n.t('col.fix')].map((label) => h('th', { key: label, style: S.th }, label))),
                      ),
                      h(
                        'tbody',
                        null,
                        doctorChecks.map((check, index) => {
                          const tone =
                            check.status === 'ok' ? T.success : check.status === 'warn' ? T.warn : check.status === 'info' ? T.secondary : T.error
                          return h(
                            'tr',
                            { key: check.id === undefined ? index : check.id },
                            h('td', { style: S.td }, check.label || check.name || check.id || '—'),
                            h(
                              'td',
                              { style: { ...S.td, color: tone, whiteSpace: 'nowrap' } },
                              check.status === 'ok' ? '✓' : check.status === 'warn' ? '!' : check.status === 'info' ? 'i' : '✕',
                            ),
                            h('td', { style: S.td }, check.detail || '—'),
                            h('td', { style: { ...S.td, color: check.status === 'ok' ? T.secondary : tone } }, check.hint || check.fix || ''),
                          )
                        }),
                      ),
                    ),
                  ),
          doctor === null ? null : h('details', null, h('summary', { style: { ...S.hint, cursor: 'pointer' } }, i18n.t('diag.report')), h('pre', { style: S.pre }, JSON.stringify(doctor.result || doctor, null, 2))),
        ),
        h(
          Card,
          { title: i18n.t('diag.state') },
          engineStatus === null
            ? h('div', { style: S.hint }, i18n.t('diag.empty'))
            : h('pre', { style: S.pre }, JSON.stringify(engineStatus.result ? engineStatus.result.data : engineStatus, null, 2)),
        ),
        h(
          Card,
          {
            title: i18n.t('log.title'),
            actions: h(
              React.Fragment,
              null,
              h(
                'div',
                { style: { width: 130 } },
                h(Select, {
                  value: logName,
                  options: (logs && Array.isArray(logs.available) && logs.available.length > 0 ? logs.available.map((entry) => entry.name.replace(/\.log$/, '')) : ['run', 'doctor', 'host']),
                  onChange: (next) => setLogName(next),
                }),
              ),
              h(
                'div',
                { style: { width: 90 } },
                h(Select, {
                  value: String(logLines),
                  options: ['200', '500', '1000', '2000'],
                  onChange: (next) => setLogLines(Number(next)),
                }),
              ),
              h(Btn, { onClick: () => refreshLogs(logName, logLines) }, i18n.t('act.refresh')),
            ),
          },
          logs === null
            ? h('div', { style: S.hint }, i18n.t('msg.loading'))
            : h(
                React.Fragment,
                null,
                logs.error ? h(Banner, { tone: 'warn' }, String(logs.error)) : null,
                logs.path ? h('div', { style: S.hint }, `${logs.path} ${logs.source === 'memory' ? i18n.t('log.source.memory') : i18n.t('log.source.file')}`) : null,
                h('pre', { style: S.pre }, (logs.lines || []).join('\n') || i18n.t('log.empty')),
              ),
        ),
      )

      const body =
        tab === 'overview'
          ? overviewTab
          : tab === 'engine'
            ? engineTab
            : tab === 'paths'
              ? pathsTab
              : tab === 'selection'
                ? selectionTab
                : tab === 'prompts'
                  ? promptsTab
                  : tab === 'quality'
                    ? qualityTab
                    : tab === 'schedule'
                      ? scheduleTab
                      : diagnoseTab

      const issueCount = Object.keys(localErrors).length

      ensureScopedStyle()
      return h(
        'div',
        { style: S.root, 'data-dsh-subtitle-studio': '' },
        h(
          'div',
          { style: S.head },
          h('h2', { style: S.h1 }, i18n.t('title')),
          dirty ? h(Chip, { tone: 'warn' }, i18n.t('msg.unsaved')) : null,
          h('span', { style: S.sub }, i18n.t('subtitle')),
        ),
        banner === null ? null : h(Banner, { tone: banner.tone }, banner.text),
        tabBar,
        body,
        h(
          'div',
          {
            style: {
              display: 'flex',
              gap: 8,
              flexWrap: 'wrap',
              alignItems: 'center',
              borderTop: `1px solid ${T.border}`,
              paddingTop: 10,
              marginTop: 2,
            },
          },
          h(Btn, { primary: true, onClick: save, disabled: busy !== '' }, i18n.t('act.save')),
          h(Btn, { onClick: revert, disabled: busy !== '' || !dirty }, i18n.t('act.revert')),
          h(Btn, { onClick: reload, disabled: busy !== '' }, i18n.t('act.reload')),
          issueCount > 0 ? h(Chip, { tone: 'err' }, `${issueCount}`) : null,
          h('span', { style: { flex: 1 } }),
          data.configPath ? h('code', { style: S.code }, data.configPath) : null,
        ),
      )
    }

    /* ----------------------------------- apply -------------------------------- */

    const inject = ['slots', 'locale']

    function apply(ctx) {
      /* 语言表的清理函数必须交回宿主：同一个页面里插件可以被重新激活
       * （装包走 HMR、开关插件、换一份模块代际），而平台对「同一命名空间 +
       * 同一语言」只允许注册一次，上一份没回收就再注册会直接抛。
       * 这里既把 disposer 交回去，也不让注册失败把整个设置分区带走。 */
      ctx.effect(() => {
        let release
        try {
          release = ctx.locale.register(NS, { zh: DICT_ZH, en: DICT_EN })
        } catch (error) {
          // 命名空间还被上一份未回收的客户端占着：字典内容完全一样，
          // 复用它的即可 —— 面板照常渲染，只是这一次不拥有回收权。
          console.warn(`[${NS}] locale register skipped: ${String(error?.message ?? error)}`)
        }
        return () => {
          if (typeof release === 'function') release()
        }
      }, `${NS}: locale`)
      i18n.t = typeof ctx.locale.bind === 'function' ? ctx.locale.bind(NS) : (key) => DICT_ZH[key] ?? key
      ctx.slots.inject('settings.section', () =>
        ctx.slots.register(
          {
            name: 'settings.section',
            id: 'subtitle-studio',
            order: 50,
            label: () => i18n.t('nav'),
          },
          Panel,
        ),
      )
    }

    return { apply, inject }
  },
})

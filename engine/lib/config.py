# -*- coding: utf-8 -*-
"""字幕工坊引擎 - config.json 读取 / 默认值合并 / 校验。

契约见 DESIGN.md §3。要点：
- 只吃 config.json，不读插件状态；
- 未知键保留（不丢用户手写字段）；
- 校验返回结构化 issues（level: error|warn，path: 点分路径），error 会以退出码 2 终止；
- 允许 JSONC 注释（// 与 /* */），方便用户手改；
- 密钥永不进入 config：只认 apiKeyRef（keychain）。
"""

import json
import os
import platform
import re
import stat

from lib.runtime import EngineError, expand, iso_from_epoch  # noqa: F401 (re-export 便于调用方)
from lib.style_templates import style_ids as _style_ids

ENGINE_KINDS = ("dsh", "minimax", "ollama", "deepseek", "openai-compatible")
SOURCE_KINDS = ("local", "ssh")
ORDERS = ("name-asc", "name-desc", "mtime-asc", "mtime-desc", "size-asc", "size-desc")
NOTIFY_CHANNELS = ("qqbot", "command", "webhook", "none")
# 「什么算成功」的两种口径：整轮零失败 / 至少产出一部。
NOTIFY_SUCCESS_WHEN = ("run", "anyOutput")
OUTPUT_MODES = ("burn", "srt")
STYLE_TEMPLATE_IDS = tuple(_style_ids())
SSH_DEFAULT_PORTS = {"default": 22}

DEFAULTS = {
    "version": 1,
    "engine": {
        "kind": "dsh",
        "provider": "",
        "model": "",
        "temperature": 0.1,
        "seed": 42,
        "maxTokens": 16000,
        "batchSize": 20,
        "reasoningEffort": "",
        "ollamaHost": "http://127.0.0.1:11434",
        "baseUrl": "",
        "apiKeyRef": "",
        "ollamaModel": "",
        "timeoutSeconds": 300,
    },
    "source": {
        "kind": "local",
        "path": "",
        "include": ["*.mp4"],
        "recursive": False,
        "ssh": {"host": "", "port": 22, "user": "", "keyPath": "~/.ssh/id_ed25519", "options": []},
    },
    "output": {
        "kind": "local",
        "path": "",
        "nameTemplate": "{stem}_zh.mp4",
        "overwrite": False,
        # mode = burn（压制进视频） / srt（只出字幕文件）。空 = 跟随 burn.enabled（旧配置）。
        # 输出方式只认顶层 output.mode；目录项里带这个键也不会生效（见 pipeline.output_mode）
        "mode": "",
        "ssh": {"host": "", "port": 22, "user": "", "keyPath": "~/.ssh/id_ed25519", "options": []},
    },
    # 多目录模式：非空时，每个元素是一组「来源 + 输出」。
    # 为空时回退到顶层 source / output（旧配置无需迁移）。
    "dirs": [],
    "selection": {
        "maxPerRun": 1,
        "skipTranslated": True,
        "respectSkipList": True,
        "minSizeGB": 0,
        "maxSizeGB": 10,
        "maxAgeDays": 36500,
        "order": "mtime-asc",
        "excludePatterns": ["*.partial", "*.tmp", "*.part"],
        # 输出目录与来源目录相同时，按命名模板认出自家的成品并排掉，
        # 避免 X_zh.mp4 被再翻成 X_zh_zh.mp4（REQ-60）。
        "skipOwnOutputs": True,
    },
    "prompts": {
        "default": {"system": "", "context": ""},
        "profiles": [],
    },
    "subtitles": {
        "bilingual": False,
        "trimSilence": True,
        "maxLineChars": 12,
        "keepOriginalAsSrt": True,
        "fontSize": 0,
        "font": "",
        # 样式模板 id：default / bold-yellow / top-white / boxed / cinema
        "styleTemplate": "default",
    },
    "burn": {
        "enabled": True,
        "ffmpeg": "",
        "crf": 20,
        "preset": "medium",
        "audio": "copy",
        "encoder": "libx264",
        "videoBitrate": "6000k",
        "fontSize": 0,
    },
    "transcribe": {
        # mlx 只在 Apple Silicon 上跑得起来；其它机器默认 faster-whisper，
        # 与宿主 index.js 的 defaultTranscribeEngine() 同一口径。
        "engine": "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster",
        "whisperModel": "",
        "language": "ja",
        "keepAudio": False,
        "chunkSeconds": 60,
        "maxLineMs": 4000,
        "pauseMs": 500,
        "silenceDb": -50,
        # 反幻觉参数（REQ-02 / REQ-03）：0 / false = 关闭对应行为
        "conditionOnPreviousText": False,
        "temperature": 0,
        "hallucinationSilenceThreshold": 0,
        "minAvgLogprob": 0,
        "maxNoSpeechProb": 0,
        "keepThanksPhrases": False,
        "keepRepeats": False,
    },
    "translation": {
        "rerunThresholdPct": 5,
        "rerunThresholdCount": 50,
    },
    "notify": {
        "enabled": True,
        "channel": "none",
        "qqAppId": "",
        "qqOpenId": "",
        "qqSecretService": "subtitle-studio-qqbot",
        "qqSendPath": "",
        "command": "",
        "webhookUrl": "",
        "webhookBody": "",
        "onStart": False,
        "onSuccess": True,
        "onFailure": True,
        # 什么算「成功」：run = 整轮零失败（旧语义，默认）；
        # anyOutput = 至少有产出一部（部分成功也算成功，另有失败通知）。
        "successWhen": "run",
    },
    "schedule": {
        "enabled": True,
        "mode": "launchd",
        "time": "02:10",
        "guardTimes": [],
        "label": "com.subtitle-studio.run",
    },
    "paths": {
        "dataDir": "~/.dsh/data/subtitle-studio",
        "keepWorkArtifacts": False,
    },
    "limits": {"minFreeGB": 15},
}

# 已知顶层键；出现其它键只 warn 不报错（保留给未来/用户注释性字段）
KNOWN_TOP_KEYS = set(DEFAULTS.keys())


# ---------------------------------------------------------------- JSONC

def strip_jsonc(text):
    """去掉 // 与 /* */ 注释（字符串内不动），并把行尾逗号容错交给 json。"""
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    # 移除对象/数组尾随逗号
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


# ---------------------------------------------------------------- 合并 / 取值

def deep_merge(defaults, override):
    """默认值 + 用户值深合并；用户值为 None 时保留默认。"""
    if not isinstance(defaults, dict) or not isinstance(override, dict):
        return override if override is not None else defaults
    merged = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            merged[key] = deep_merge(defaults[key], value)
        elif value is None:
            merged[key] = defaults.get(key)
        else:
            merged[key] = value
    return merged


def dotted(data, path, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# ---------------------------------------------------------------- Config 对象

class Config(object):
    def __init__(self, path, raw, data, issues):
        self.path = path
        self.raw = raw
        self.data = data
        self.issues = issues

    def get(self, path, default=None):
        return dotted(self.data, path, default)

    @property
    def errors(self):
        return [i for i in self.issues if i["level"] == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i["level"] == "warn"]

    @property
    def data_dir(self):
        return expand(self.get("paths.dataDir") or DEFAULTS["paths"]["dataDir"])

    @property
    def work_dir(self):
        return os.path.join(self.data_dir, "work")

    @property
    def logs_dir(self):
        return os.path.join(self.data_dir, "logs")

    @property
    def state_dir(self):
        return os.path.join(self.data_dir, "state")

    def data_dirs(self):
        return {"dataDir": self.data_dir, "workDir": self.work_dir,
                "logsDir": self.logs_dir, "stateDir": self.state_dir}


# ---------------------------------------------------------------- 校验

def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ssh 把 host / user 拼成 `user@host` 交给命令行程序：以 `-` 开头的值会被当成选项
# （`-oProxyCommand=…` 一类），空格与引号同样不该出现在主机名 / 用户名里。
SSH_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")


def _check_ssh(issues, prefix, ssh, required):
    if not isinstance(ssh, dict):
        issues.append({"path": prefix, "level": "error", "message": "ssh 必须是对象"})
        return
    host = (ssh.get("host") or "").strip()
    if required and not host:
        issues.append({"path": prefix + ".host", "level": "error", "message": "ssh.host 不能为空"})
    if host and not SSH_TOKEN_RE.match(host):
        issues.append({"path": prefix + ".host", "level": "error",
                       "message": "ssh.host 只能是主机名 / IP（不能以 - 开头，不能含空格或引号）"})
    port = ssh.get("port")
    if port is not None and (not _is_int(port) or not (1 <= port <= 65535)):
        issues.append({"path": prefix + ".port", "level": "error", "message": "ssh.port 必须是 1-65535 的整数"})
    user = (ssh.get("user") or "").strip()
    if required and not user:
        issues.append({"path": prefix + ".user", "level": "error", "message": "ssh.user 不能为空"})
    if user and not SSH_TOKEN_RE.match(user):
        issues.append({"path": prefix + ".user", "level": "error",
                       "message": "ssh.user 只能是合法用户名（不能以 - 开头，不能含空格或引号）"})
    if ssh.get("keyPath") and not os.path.exists(expand(ssh["keyPath"])):
        issues.append({"path": prefix + ".keyPath", "level": "warn",
                       "message": "SSH 私钥不存在：%s（若用 ssh-agent 可忽略）" % ssh["keyPath"]})
    if not isinstance(ssh.get("options") or [], list):
        issues.append({"path": prefix + ".options", "level": "error", "message": "ssh.options 必须是数组"})


def _template_renders(template):
    """命名模板只支持 {stem} / {name} / {ext} 三个键（见 select.output_name）。

    写成 `{stem[:10]}` 或 `{stem.lower()}` 时 str.format 抛的是 TypeError /
    AttributeError，而 output_name 只吞 KeyError/IndexError/ValueError ——
    选片阶段每扫一个文件炸一次，整轮直接挂（R3）。所以校验时先试渲染一次，
    把错误提前到配置页，而不是留到夜里。
    """
    try:
        template.format(stem="sample", name="sample.mkv", ext="mp4")
    except Exception:  # noqa: BLE001 - 坏格式串的异常形态很多
        return False
    return True


def _check_dir_pair(issues, prefix, source, output):
    """校验一个目录项（来源 + 输出）。"""
    def err(path, message):
        issues.append({"path": path, "level": "error", "message": message})

    if not isinstance(source, dict):
        err(prefix + ".source", "必须是对象")
        source = {}
    if not isinstance(output, dict):
        err(prefix + ".output", "必须是对象")
        output = {}
    if source.get("kind") not in SOURCE_KINDS:
        err(prefix + ".source.kind", "必须是 local / ssh，当前 %r" % (source.get("kind"),))
    if not (source.get("path") or "").strip():
        err(prefix + ".source.path", "不能为空")
    include = source.get("include") or []
    if not isinstance(include, list) or not include:
        err(prefix + ".source.include", "必须是非空数组（如 [\"*.mp4\"]）")
    if not isinstance(source.get("recursive"), bool):
        err(prefix + ".source.recursive", "必须是 true/false")
    _check_ssh(issues, prefix + ".source.ssh", source.get("ssh") or {}, source.get("kind") == "ssh")

    if output.get("kind") not in SOURCE_KINDS:
        err(prefix + ".output.kind", "必须是 local / ssh，当前 %r" % (output.get("kind"),))
    if not (output.get("path") or "").strip():
        err(prefix + ".output.path", "不能为空")
    template = output.get("nameTemplate") or ""
    if "{stem}" not in template:
        err(prefix + ".output.nameTemplate", "必须包含 {stem} 占位符，当前 %r" % (template,))
    elif not _template_renders(template):
        err(prefix + ".output.nameTemplate",
            "模板渲染不了，只支持 {stem} / {name} / {ext} 三个占位符，当前 %r" % (template,))
    if not isinstance(output.get("overwrite"), bool):
        err(prefix + ".output.overwrite", "必须是 true/false")
    _check_ssh(issues, prefix + ".output.ssh", output.get("ssh") or {}, output.get("kind") == "ssh")
    if (source.get("kind") == output.get("kind")
            and (source.get("path") or "").strip() == (output.get("path") or "").strip()
            and (output.get("path") or "").strip()):
        issues.append({"path": prefix + ".output.path", "level": "warn",
                       "message": "输出路径与来源路径相同，产物会落回输入目录"})


def _merge_dir_section(base, override):
    """把目录项里的 source/output 覆盖到默认值上（ssh 逐字段合并）。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if key == "ssh" and isinstance(value, dict):
            merged_ssh = dict(out.get("ssh") or {})
            merged_ssh.update(value)
            out["ssh"] = merged_ssh
        else:
            out[key] = value
    return out


def dir_entries(data):
    """返回本次运行要处理的目录列表。

    dirs 非空 → 多目录模式；否则回退到顶层 source / output（旧配置零迁移）。
    每项：{index, name, source, output}。
    """
    entries = []
    raw = (data or {}).get("dirs")
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            source = _merge_dir_section(DEFAULTS["source"], item.get("source") or {})
            output = _merge_dir_section(DEFAULTS["output"], item.get("output") or {})
            name = str(item.get("name") or "").strip() or ("目录 %d" % (index + 1))
            entries.append({"index": index, "name": name, "source": source, "output": output})
    if not entries:
        source = _merge_dir_section(DEFAULTS["source"], (data or {}).get("source") or {})
        output = _merge_dir_section(DEFAULTS["output"], (data or {}).get("output") or {})
        entries.append({"index": 0, "name": "默认目录", "source": source, "output": output})
    return entries


def validate(data):
    issues = []

    def err(path, message):
        issues.append({"path": path, "level": "error", "message": message})

    def warn(path, message):
        issues.append({"path": path, "level": "warn", "message": message})

    version = data.get("version")
    if version != 1:
        warn("version", "config version=%r，引擎按 v1 解析" % (version,))

    # ---- engine
    engine = data.get("engine") or {}
    if engine.get("kind") not in ENGINE_KINDS:
        err("engine.kind", "必须是 %s 之一，当前 %r" % (list(ENGINE_KINDS), engine.get("kind")))
    if not (engine.get("model") or "").strip():
        err("engine.model", "engine.model 不能为空")
    if not _is_num(engine.get("temperature")) or not (0 <= float(engine["temperature"]) <= 2):
        err("engine.temperature", "必须在 0-2 之间")
    if not _is_int(engine.get("seed")):
        err("engine.seed", "必须是整数")
    if not _is_int(engine.get("maxTokens")) or engine["maxTokens"] <= 0:
        err("engine.maxTokens", "必须是正整数")
    if not _is_int(engine.get("batchSize")) or not (1 <= engine["batchSize"] <= 500):
        err("engine.batchSize", "必须是 1-500 的整数")
    effort = str(engine.get("reasoningEffort") or "").strip()
    if effort and not re.match(r"^[A-Za-z0-9._-]{1,40}$", effort):
        err("engine.reasoningEffort", "只能是字母/数字/. _ - 组成的档位名，或留空跟随模型默认")
    if engine.get("kind") == "dsh":
        if not (engine.get("provider") or "").strip():
            err("engine.provider", "dsh 引擎必须配置 provider（DSH 侧的模型提供方）")
    if engine.get("kind") == "ollama":
        if not (engine.get("ollamaHost") or "").strip():
            err("engine.ollamaHost", "ollama 引擎必须配置 ollamaHost")
        model = (engine.get("model") or "").strip()
        legacy = (engine.get("ollamaModel") or "").strip()
        if model == "" and legacy == "":
            err("engine.model", "ollama 引擎必须选择模型")
        elif model != "" and legacy != "" and legacy != model:
            warn("engine.ollamaModel",
                 "历史覆盖项 ollamaModel=%s 不再生效，以上面选的 model=%s 为准" % (legacy, model))
    if engine.get("kind") in ("deepseek", "openai-compatible"):
        if not (engine.get("baseUrl") or "").strip():
            err("engine.baseUrl", "%s 引擎必须配置 baseUrl" % engine.get("kind"))
        if not (engine.get("apiKeyRef") or "").strip() and not os.environ.get("DEEPSEEK_API_KEY"):
            warn("engine.apiKeyRef", "未配置 apiKeyRef，且环境变量无 DEEPSEEK_API_KEY")
    if _is_int(engine.get("timeoutSeconds")) is False or (engine.get("timeoutSeconds") or 0) <= 0:
        err("engine.timeoutSeconds", "必须是正整数（秒）")

    # ---- 多目录
    raw_dirs = data.get("dirs")
    if raw_dirs is None:
        raw_dirs = []
    if not isinstance(raw_dirs, list):
        err("dirs", "必须是数组（每项含 source 与 output）")
        raw_dirs = []
    multi_dir = len(raw_dirs) > 0
    seen_outputs = {}
    for index, item in enumerate(raw_dirs):
        prefix = "dirs[%d]" % index
        if not isinstance(item, dict):
            err(prefix, "必须是对象 {name, source, output}")
            continue
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            err(prefix + ".name", "必须是字符串")
        # 与 dir_entries 的归一化保持一致：缺省字段按默认值补齐后再校验，
        # 避免用户只填路径就报一堆“缺字段”。
        dir_source = _merge_dir_section(DEFAULTS["source"], item.get("source") or {})
        dir_output = _merge_dir_section(DEFAULTS["output"], item.get("output") or {})
        _check_dir_pair(issues, prefix, dir_source, dir_output)
        output_path = (dir_output.get("path") or "").strip()
        if output_path:
            if output_path in seen_outputs:
                warn(prefix + ".output.path",
                     "与 %s 指向同一个输出目录，同名文件可能互相覆盖" % seen_outputs[output_path])
            else:
                seen_outputs[output_path] = prefix

    # ---- source（多目录模式下只是新建目录时的默认值）
    source = data.get("source") or {}
    if source.get("kind") not in SOURCE_KINDS:
        err("source.kind", "必须是 local / ssh，当前 %r" % (source.get("kind"),))
    if not (source.get("path") or "").strip() and not multi_dir:
        err("source.path", "source.path 不能为空")
    include = source.get("include") or []
    if not isinstance(include, list) or not include:
        err("source.include", "必须是非空数组（如 [\"*.mp4\"]）")
    if not isinstance(source.get("recursive"), bool):
        err("source.recursive", "必须是 true/false")
    _check_ssh(issues, "source.ssh", source.get("ssh") or {}, source.get("kind") == "ssh")

    # ---- output（多目录模式下只是新建目录时的默认值）
    output = data.get("output") or {}
    if output.get("kind") not in SOURCE_KINDS:
        err("output.kind", "必须是 local / ssh，当前 %r" % (output.get("kind"),))
    if not (output.get("path") or "").strip() and not multi_dir:
        err("output.path", "output.path 不能为空")
    template = output.get("nameTemplate") or ""
    if "{stem}" not in template:
        err("output.nameTemplate", "必须包含 {stem} 占位符，当前 %r" % (template,))
    elif not _template_renders(template):
        err("output.nameTemplate",
            "模板渲染不了，只支持 {stem} / {name} / {ext} 三个占位符，当前 %r" % (template,))
    if not isinstance(output.get("overwrite"), bool):
        err("output.overwrite", "必须是 true/false")
    _check_ssh(issues, "output.ssh", output.get("ssh") or {}, output.get("kind") == "ssh")

    # ---- selection
    sel = data.get("selection") or {}
    if not _is_int(sel.get("maxPerRun")) or sel["maxPerRun"] < 0:
        err("selection.maxPerRun", "必须是 >=0 的整数（0 表示不限额）")
    if not isinstance(sel.get("skipTranslated"), bool):
        err("selection.skipTranslated", "必须是 true/false")
    if not isinstance(sel.get("skipOwnOutputs"), bool):
        err("selection.skipOwnOutputs", "必须是 true/false")
    for key in ("minSizeGB", "maxSizeGB"):
        if not _is_num(sel.get(key)) or sel[key] < 0:
            err("selection." + key, "必须是 >=0 的数字")
    if _is_num(sel.get("minSizeGB")) and _is_num(sel.get("maxSizeGB")) and sel["maxSizeGB"] > 0 \
            and sel["minSizeGB"] > sel["maxSizeGB"]:
        err("selection.minSizeGB", "minSizeGB 不能大于 maxSizeGB")
    if not _is_num(sel.get("maxAgeDays")) or sel["maxAgeDays"] <= 0:
        err("selection.maxAgeDays", "必须是正数（天）")
    if sel.get("order") not in ORDERS:
        err("selection.order", "必须是 %s 之一" % (list(ORDERS),))
    if not isinstance(sel.get("excludePatterns") or [], list):
        err("selection.excludePatterns", "必须是数组")

    # ---- prompts
    prompts = data.get("prompts") or {}
    default_prompt = prompts.get("default") or {}
    if not isinstance(default_prompt, dict):
        err("prompts.default", "必须是对象 {system, context}")
    else:
        if not (default_prompt.get("system") or "").strip():
            warn("prompts.default.system", "system 为空，将使用引擎内置提示词")
        if not isinstance(default_prompt.get("context") or "", str):
            err("prompts.default.context", "必须是字符串")
    profiles = prompts.get("profiles") or []
    if not isinstance(profiles, list):
        err("prompts.profiles", "必须是数组")
    else:
        for idx, profile in enumerate(profiles):
            base = "prompts.profiles[%d]" % idx
            if not isinstance(profile, dict):
                err(base, "必须是对象")
                continue
            if not (profile.get("name") or "").strip():
                err(base + ".name", "profile 需要 name（用于日志/计划展示）")
            match = profile.get("match") or []
            if not isinstance(match, list) or not match:
                err(base + ".match", "必须是非空 glob 数组")
            for key in ("system", "context", "series"):
                if profile.get(key) is not None and not isinstance(profile.get(key), str):
                    err("%s.%s" % (base, key), "必须是字符串")

    # ---- subtitles / burn
    subs = data.get("subtitles") or {}
    for key in ("bilingual", "trimSilence", "keepOriginalAsSrt"):
        if not isinstance(subs.get(key), bool):
            err("subtitles." + key, "必须是 true/false")
    if not _is_int(subs.get("maxLineChars")) or subs["maxLineChars"] < 0:
        err("subtitles.maxLineChars", "必须是 >=0 的整数")
    burn = data.get("burn") or {}
    if not isinstance(burn.get("enabled"), bool):
        err("burn.enabled", "必须是 true/false")
    if not _is_int(burn.get("crf")) or not (0 <= burn["crf"] <= 51):
        err("burn.crf", "必须是 0-51 的整数")
    if not (burn.get("preset") or "").strip():
        err("burn.preset", "不能为空（如 medium）")
    if burn.get("audio") not in ("copy", "aac", "opus"):
        err("burn.audio", "必须是 copy / aac / opus")
    if burn.get("encoder") not in ("libx264", "h264_videotoolbox"):
        err("burn.encoder", "必须是 libx264 / h264_videotoolbox")
    if not _is_int(burn.get("fontSize")) or burn["fontSize"] < 0:
        err("burn.fontSize", "必须是 >=0 的整数")

    # ---- output.mode（REQ-42）：空 = 跟随 burn.enabled
    out_mode = (data.get("output") or {}).get("mode")
    if out_mode not in (None, "", "burn", "srt"):
        err("output.mode", "必须是 burn / srt（或留空跟随 burn.enabled）")
    if (subs.get("styleTemplate") or "") and subs["styleTemplate"] not in STYLE_TEMPLATE_IDS:
        err("subtitles.styleTemplate", "必须是 %s 之一" % (list(STYLE_TEMPLATE_IDS),))

    # ---- transcribe
    tr = data.get("transcribe") or {}
    if not (tr.get("language") or "").strip():
        warn("transcribe.language", "未指定语言，转写将自动检测语种")
    if not _is_int(tr.get("chunkSeconds")) or tr["chunkSeconds"] < 10:
        err("transcribe.chunkSeconds", "必须是 >=10 的整数")
    for key in ("maxLineMs", "pauseMs"):
        if not _is_int(tr.get(key)) or tr[key] <= 0:
            err("transcribe." + key, "必须是正整数")
    if not _is_num(tr.get("silenceDb")):
        err("transcribe.silenceDb", "必须是数字（如 -50）")
    font = str((data.get("subtitles") or {}).get("font") or "").strip()
    if font and re.search(r"""['":;=\\<>]|[\x00-\x1f]""", font):
        err("subtitles.font", "字体名不能包含引号、冒号、分号、等号或尖括号")

    if tr.get("engine") not in ("mlx", "faster"):
        err("transcribe.engine", "必须是 mlx / faster")
    for key in ("conditionOnPreviousText", "keepThanksPhrases", "keepRepeats"):
        if not isinstance(tr.get(key), bool):
            err("transcribe." + key, "必须是 true/false")
    if not _is_num(tr.get("temperature")) or not (0 <= tr["temperature"] <= 2):
        err("transcribe.temperature", "必须是 0-2 的数字（0 = 不做温度回退）")
    for key in ("hallucinationSilenceThreshold", "maxNoSpeechProb"):
        if not _is_num(tr.get(key)) or tr[key] < 0:
            err("transcribe." + key, "必须是 >=0 的数字（0 = 关闭）")
    if not _is_num(tr.get("minAvgLogprob")) or tr["minAvgLogprob"] > 0:
        err("transcribe.minAvgLogprob", "必须是 <=0 的数字（0 = 关闭）")

    # ---- translation / notify / schedule / paths / limits
    trans = data.get("translation") or {}
    if not _is_num(trans.get("rerunThresholdPct")) or trans["rerunThresholdPct"] < 0:
        err("translation.rerunThresholdPct", "必须是 >=0 的数字")
    if not _is_int(trans.get("rerunThresholdCount")) or trans["rerunThresholdCount"] < 0:
        err("translation.rerunThresholdCount", "必须是 >=0 的整数")
    notify = data.get("notify") or {}
    if notify.get("channel") not in NOTIFY_CHANNELS:
        err("notify.channel", "必须是 %s 之一" % (list(NOTIFY_CHANNELS),))
    for key in ("enabled", "onStart", "onSuccess", "onFailure"):
        if not isinstance(notify.get(key), bool):
            err("notify." + key, "必须是 true/false")
    if notify.get("successWhen") not in NOTIFY_SUCCESS_WHEN:
        err("notify.successWhen", "必须是 %s 之一" % (list(NOTIFY_SUCCESS_WHEN),))
    if notify.get("channel") == "qqbot":
        app_id = (notify.get("qqAppId") or "").strip()
        open_id = (notify.get("qqOpenId") or "").strip()
        send_path = (notify.get("qqSendPath") or "").strip()
        if not app_id and not send_path:
            err("notify.qqAppId", "channel=qqbot 时必须填写 QQ Bot 的 appId（或回退脚本 qqSendPath）")
        if app_id and not open_id:
            err("notify.qqOpenId", "填写了 qqAppId 就必须同时填写接收人的 qqOpenId")
    if notify.get("channel") == "command" and not (notify.get("command") or "").strip():
        err("notify.command", "channel=command 时必须填写要执行的命令")
    if notify.get("channel") == "webhook":
        url = (notify.get("webhookUrl") or "").strip()
        if not url.startswith(("http://", "https://")):
            err("notify.webhookUrl", "channel=webhook 时必须是以 http(s):// 开头的地址")
    sched = data.get("schedule") or {}
    # dsh / off 是宿主侧的调度语义，引擎不消费，但必须能原样读这份配置：
    # 手工跑 `subtitle-run guard --config config.json` 不能因为面板选了 DSH 调度就退出 2。
    if sched.get("mode") not in ("launchd", "none", "dsh", "off"):
        err("schedule.mode", "必须是 launchd / none / dsh / off")
    if not re.match(r"^\d{1,2}:\d{2}$", str(sched.get("time") or "")):
        err("schedule.time", "必须是 HH:MM")
    guard_times = sched.get("guardTimes")
    if guard_times is not None:
        if not isinstance(guard_times, list):
            err("schedule.guardTimes", "必须是 HH:MM 字符串数组")
        else:
            if len(guard_times) > 11:
                err("schedule.guardTimes", "最多 11 个额外触发点")
            seen_times = set()
            for index, item in enumerate(guard_times):
                label = "schedule.guardTimes[%d]" % index
                if not isinstance(item, str) or not re.match(r"^\d{1,2}:\d{2}$", item.strip()):
                    err(label, "必须是 HH:MM")
                    continue
                hour_text, minute_text = item.strip().split(":")
                if int(hour_text) > 23 or int(minute_text) > 59:
                    err(label, "必须是有效的时钟时间")
                    continue
                normalized = "%02d:%s" % (int(hour_text), minute_text)
                if normalized in seen_times:
                    err(label, "重复的触发点")
                seen_times.add(normalized)
    paths = data.get("paths") or {}
    if not (paths.get("dataDir") or "").strip():
        err("paths.dataDir", "不能为空")
    limits = data.get("limits") or {}
    if not _is_num(limits.get("minFreeGB")) or limits["minFreeGB"] < 0:
        err("limits.minFreeGB", "必须是 >=0 的数字")

    for key in data.keys():
        if key not in KNOWN_TOP_KEYS:
            warn(key, "未知顶层键（已保留，不参与逻辑）")

    return issues


def load_config(path):
    """读取并校验 config.json；结构错误抛 EngineError(2)。"""
    path = expand(path)
    if not path:
        raise EngineError(2, "未指定 --config 路径")
    if not os.path.isfile(path):
        raise EngineError(2, "config 文件不存在：%s" % path)
    try:
        text = open(path, "r", encoding="utf-8").read()
    except OSError as exc:
        raise EngineError(2, "config 读取失败：%s" % exc)
    try:
        raw = json.loads(strip_jsonc(text))
    except ValueError as exc:
        raise EngineError(2, "config 不是合法 JSON：%s" % exc, detail="文件：%s" % path)
    if not isinstance(raw, dict):
        raise EngineError(2, "config 顶层必须是对象")
    data = deep_merge(DEFAULTS, raw)
    issues = validate(data)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        issues.append({"path": "$file", "level": "warn",
                       "message": "config 权限 %o，建议 chmod 600（含 NAS/引擎配置）" % mode})
    cfg = Config(path, raw, data, issues)
    if cfg.errors:
        raise EngineError(2, "config 校验失败：%d 个错误" % len(cfg.errors),
                          detail=cfg.errors)
    return cfg

# -*- coding: utf-8 -*-
"""字幕工坊引擎 - doctor 自检。

每个检查项返回 {name, group, level, message, detail}：
level = ok / warn / error / info。
- error 决定 verdict=failed（退出码 1）；
- warn 只是提示（verdict=degraded，退出码 0）；
- info 仅展示（不影响判定，例如非配置端口的探测结果）。
"""

import json
import os
import re
import shutil
import socket
import sys

from lib import config as config_mod
from lib import keys, notify as notify_mod, pipeline
from lib.remote import Endpoint, tcp_probe
from lib.runtime import (EngineError, df_avail_bytes, ensure_dir, expand, file_size,
                         human_size, now_iso, read_text, run_cmd)

ENGINE_VERSION = "1.4.0"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|tvly-[A-Za-z0-9_\-]{8,})")


class Checks(object):
    def __init__(self):
        self.items = []

    def add(self, name, group, level, message, detail=None):
        entry = {"name": name, "group": group, "level": level, "message": message}
        if detail is not None:
            entry["detail"] = detail
        self.items.append(entry)
        return entry

    def ok(self, name, group, message, detail=None):
        return self.add(name, group, "ok", message, detail)

    def warn(self, name, group, message, detail=None):
        return self.add(name, group, "warn", message, detail)

    def error(self, name, group, message, detail=None):
        return self.add(name, group, "error", message, detail)

    def info(self, name, group, message, detail=None):
        return self.add(name, group, "info", message, detail)

    def summary(self):
        counts = {"ok": 0, "warn": 0, "error": 0, "info": 0}
        for item in self.items:
            counts[item["level"]] = counts.get(item["level"], 0) + 1
        if counts["error"]:
            verdict = "failed"
        elif counts["warn"]:
            verdict = "degraded"
        else:
            verdict = "green"
        return counts, verdict


# ---------------------------------------------------------------- 单项检查

def check_config(checks, cfg):
    checks.ok("config", "config", "配置已加载：%s" % cfg.path,
              {"dataDir": cfg.data_dir, "version": cfg.get("version")})
    for issue in cfg.warnings:
        checks.warn("config.%s" % issue["path"], "config",
                    "配置提示：%s" % issue["message"])
    try:
        mode = os.stat(cfg.path).st_mode & 0o777
        if mode & 0o077:
            checks.warn("config.permissions", "config",
                        "config.json 权限为 %o，建议 chmod 600" % mode)
        else:
            checks.ok("config.permissions", "config", "config.json 权限 %o" % mode)
    except OSError as exc:
        checks.warn("config.permissions", "config", "无法读取权限：%s" % exc)

    text = read_text(cfg.path)
    hits = SECRET_RE.findall(text) if text else []
    if hits:
        checks.error("config.secrets", "config",
                     "config.json 中疑似存在明文密钥（%d 处），应只保留 apiKeyRef" % len(hits),
                     {"samples": [h[:6] + "..." for h in hits[:3]]})
    else:
        checks.ok("config.secrets", "config", "config.json 未发现明文密钥")


def check_python(checks):
    version = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info[:2] < (3, 9):
        checks.error("python", "deps", "python 版本过低：%s（需要 ≥3.9）" % version)
    else:
        checks.ok("python", "deps", "python3 %s（%s）" % (version, sys.executable))


def check_ffmpeg(checks, cfg):
    try:
        ffmpeg = pipeline.ffmpeg_bin(cfg)
    except EngineError as exc:
        checks.error("ffmpeg", "deps", exc.message)
        return
    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-version"], timeout=30)
    version_line = (out or err).splitlines()[0] if (out or err) else "?"
    if rc != 0:
        checks.error("ffmpeg", "deps", "ffmpeg 无法执行（rc=%s）" % rc, {"path": ffmpeg})
        return
    checks.ok("ffmpeg", "deps", version_line, {"path": ffmpeg})

    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-filters"], timeout=60)
    filters = out or ""
    missing = [name for name in ("ass", "subtitles", "testsrc2", "sine")
               if not re.search(r"\b%s\b" % name, filters)]
    if missing:
        checks.error("ffmpeg.filters", "deps",
                     "ffmpeg 缺少必需滤镜/源：%s（需要 ffmpeg-full）" % ", ".join(missing))
    else:
        checks.ok("ffmpeg.filters", "deps", "ass/subtitles/testsrc2/sine 均可用")

    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-encoders"], timeout=60)
    encoders = out or ""
    for encoder in ("libx264", "aac"):
        if re.search(r"\b%s\b" % encoder, encoders):
            checks.ok("ffmpeg.encoder.%s" % encoder, "deps", "编码器 %s 可用" % encoder)
        else:
            checks.error("ffmpeg.encoder.%s" % encoder, "deps",
                         "缺少编码器 %s" % encoder)


def check_ffprobe(checks, cfg):
    try:
        ffprobe = pipeline.ffprobe_bin(cfg)
    except EngineError as exc:
        checks.error("ffprobe", "deps", exc.message)
        return
    rc, out, err = run_cmd([ffprobe, "-hide_banner", "-version"], timeout=30)
    line = (out or err).splitlines()[0] if (out or err) else "?"
    if rc == 0:
        checks.ok("ffprobe", "deps", line, {"path": ffprobe})
    else:
        checks.error("ffprobe", "deps", "ffprobe 无法执行（rc=%s）" % rc, {"path": ffprobe})


def check_whisper(checks, cfg):
    engine = cfg.get("transcribe.engine") or "mlx"
    if engine == "faster":
        module = "faster_whisper"
    else:
        module = "mlx_whisper"
    rc, out, err = run_cmd([pipeline.python_bin(), "-c",
                            "import %s, sys; print(getattr(%s, '__version__', 'unknown'))"
                            % (module, module)], timeout=60)
    if rc != 0:
        checks.warn("whisper", "deps",
                    "无法 import %s（转写会失败）：%s" % (module, (err or out).strip()[-200:]),
                    {"hint": "pip3 install --user %s"
                          % ("faster-whisper" if engine == "faster" else "mlx-whisper")})
    else:
        checks.ok("whisper", "deps", "%s 可用（版本 %s）" % (module, (out or "").strip()))

    model = (cfg.get("transcribe.whisperModel") or "").strip()
    if not model:
        model = "mlx-community/whisper-large-v3-turbo"
    cache = os.path.join(expand("~/.cache/huggingface/hub"),
                         "models--" + model.replace("/", "--"))
    if os.path.isdir(cache):
        checks.ok("whisper.model", "deps", "模型已缓存：%s" % model)
    else:
        checks.warn("whisper.model", "deps",
                    "模型未缓存（首次转写需联网下载）：%s" % model, {"cache": cache})


def _http_get(url, headers=None, timeout=10):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
            return response.status, body, None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(512).decode("utf-8", "replace"), "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - 网络错误类型很多，统一报告
        return None, "", "%s: %s" % (type(exc).__name__, exc)


def _http_post(url, payload, headers=None, timeout=10):
    import urllib.error
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data,
                                     headers=dict(headers or {}, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
            return response.status, body, None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(512).decode("utf-8", "replace"), "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - 网络错误类型很多，统一报告
        return None, "", "%s: %s" % (type(exc).__name__, exc)


def _bridge_endpoint():
    """读取宿主发布的中转端点描述文件（与 vendor/translate_srt.py 同规则）。"""
    raw = os.environ.get("SUBTITLE_STUDIO_BRIDGE")
    if not raw:
        base = os.environ.get("SUBTITLE_STUDIO_DATA_DIR") or os.path.join(
            os.path.expanduser("~"), ".dsh", "data", "subtitle-studio")
        raw = os.path.join(base, "state", "bridge.json")
    try:
        with open(raw, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data


def check_engine(checks, cfg):
    kind = cfg.get("engine.kind")
    model = pipeline.model_for_engine(cfg) or "(未设置)"
    api_key, source = keys.resolve_api_key(kind, cfg.get("engine.apiKeyRef"))

    if kind == "minimax":
        mmx = shutil.which("mmx")
        if not mmx:
            checks.error("engine.mmx", "engine",
                         "未找到 mmx CLI（minimax 引擎必需）", {"hint": "npm i -g @minimax/mmx"})
        else:
            rc, out, err = run_cmd([mmx, "auth", "status"], timeout=30)
            text = (out or "").strip() or (err or "").strip()
            if rc == 0 and text:
                method = ""
                try:
                    payload = json.loads(out)
                    method = payload.get("method") or ""
                except ValueError:
                    pass
                checks.ok("engine.mmx", "engine",
                          "mmx 已认证%s" % ("（%s）" % method if method else ""),
                          {"path": mmx, "model": model})
            elif re.search(r"(已认证|authenticated|api[_-]?key)", text, re.I):
                checks.ok("engine.mmx", "engine", "mmx 已认证", {"path": mmx, "model": model})
            else:
                checks.error("engine.mmx", "engine",
                             "mmx 未认证（mmx auth status 返回 rc=%s）" % rc,
                             {"output": text[:300]})
    elif kind == "ollama":
        host = (cfg.get("engine.ollamaHost") or "").rstrip("/")
        if not host:
            checks.error("engine.ollama", "engine", "engine.ollamaHost 为空")
        else:
            status, body, error = _http_get(host + "/api/tags", timeout=8)
            if status == 200:
                count = 0
                try:
                    count = len(json.loads(body).get("models") or [])
                except ValueError:
                    pass
                checks.ok("engine.ollama", "engine",
                          "Ollama 可达（%s，%d 个模型）" % (host, count),
                          {"model": model})
            else:
                checks.error("engine.ollama", "engine",
                             "Ollama 不可达：%s" % (error or status), {"host": host})
    elif kind == "dsh":
        provider = (cfg.get("engine.provider") or "").strip()
        if not provider:
            checks.error("engine.provider", "engine",
                         "dsh 引擎需要 engine.provider（DSH 侧的模型提供方）")
        else:
            checks.ok("engine.provider", "engine",
                      "提供方：%s" % provider, {"model": model})
        endpoint = _bridge_endpoint()
        if endpoint is None:
            checks.error("engine.bridge", "engine",
                         "读不到 DSH 中转端点文件（插件未运行，或引擎不是被插件拉起的）")
        else:
            base = str(endpoint.get("baseUrl") or "").rstrip("/")
            bridge_path = str(endpoint.get("bridgePath") or "/api/dsh-subtitle-studio/llm")
            token_path = str(endpoint.get("tokenPath") or "")
            if not os.path.exists(token_path):
                checks.error("engine.bridge", "engine",
                             "中转令牌文件不存在：%s" % token_path)
            else:
                # 不带令牌打一次：返回 401 说明宿主在线且中转路由已挂上
                status, body, error = _http_post(base + bridge_path, {}, timeout=8)
                if status == 401:
                    checks.ok("engine.bridge", "engine",
                              "DSH 中转可达：%s" % base, {"provider": provider, "model": model})
                else:
                    checks.error("engine.bridge", "engine",
                                 "DSH 中转不可用（HTTP %s）：%s" % (status, error or body))
    else:  # deepseek / openai-compatible
        if not (cfg.get("engine.baseUrl") or "").strip():
            checks.error("engine.baseUrl", "engine",
                         "引擎 %s 需要 engine.baseUrl" % kind)
        if not api_key:
            checks.error("engine.key", "engine",
                         "未取到 API key（apiKeyRef=%s，env/keychain 都没有）"
                         % (cfg.get("engine.apiKeyRef") or "(空)"))
        else:
            checks.ok("engine.key", "engine",
                      "API key 已取到（来源：%s，%s）" % (source, keys.mask(api_key)))
            base = (cfg.get("engine.baseUrl") or "").strip().rstrip("/")
            if base:
                status, body, error = _http_get(base + "/models",
                                                {"Authorization": "Bearer " + api_key},
                                                timeout=10)
                if status == 200:
                    checks.ok("engine.http", "engine", "兼容接口可达：%s" % base)
                else:
                    checks.error("engine.http", "engine",
                                 "兼容接口不可用：%s" % (error or status), {"baseUrl": base})


def check_endpoints(checks, cfg):
    """按目录逐个体检（多目录模式下每个 dirs[] 都查一遍）。"""
    entries = config_mod.dir_entries(cfg.data)
    for entry in entries:
        check_one_endpoint(checks, cfg, entry)
    return


def check_one_endpoint(checks, cfg, entry):
    """单个目录项的来源/输出体检。"""
    source_cfg = entry["source"]
    output_cfg = entry["output"]
    prefix = "" if len(config_mod.dir_entries(cfg.data)) <= 1 else ("dirs[%d]." % entry["index"])
    source = Endpoint(source_cfg.get("kind"), source_cfg.get("path"),
                      source_cfg.get("ssh"))
    output = Endpoint(output_cfg.get("kind"), output_cfg.get("path"),
                      output_cfg.get("ssh"))

    probe = source.probe()
    include = source_cfg.get("include") or ["*"]
    label = ("[%s] " % entry["name"]) if prefix else ""
    if probe["ok"]:
        checks.ok(prefix + "source", "io", "%s来源可达：%s（%s）"
                  % (label, source.describe(), "%.0fms" % probe["latencyMs"]
                     if probe.get("latencyMs") is not None else "local"))
        try:
            files = source.list_files(include, bool(source_cfg.get("recursive")))
            checks.ok(prefix + "source.files", "io", "%s匹配到 %d 个文件（include=%s）"
                      % (label, len(files), ",".join(include)))
        except EngineError as exc:
            checks.error(prefix + "source.files", "io", exc.message, exc.detail)
    else:
        checks.error(prefix + "source", "io", "%s来源不可达：%s" % (label, probe["detail"]),
                     {"path": source_cfg.get("path")})

    # SSH 端口：配置端口为准（error），22 仅报告（info）
    for role, endpoint in (("source", source), ("output", output)):
        if endpoint.is_local:
            continue
        conf = (source_cfg if role == "source" else output_cfg).get("ssh") or {}
        host = conf.get("host")
        port = int(conf.get("port") or 22)
        ok, latency, error = tcp_probe(host, port, timeout=8)
        if ok:
            checks.ok(prefix + "%s.ssh" % role, "io",
                      "%sSSH %s:%d 可达（%.0fms）" % (label, host, port, latency or 0))
        else:
            checks.error(prefix + "%s.ssh" % role, "io",
                         "%sSSH %s:%d 不可达：%s" % (label, host, port, error))
        for probe_port in (22,):
            if probe_port == port:
                continue
            p_ok, p_latency, p_error = tcp_probe(host, probe_port, timeout=5)
            checks.info(prefix + "%s.port.%d" % (role, probe_port), "io",
                        "%s:%d %s" % (host, probe_port,
                                      "开放（%.0fms）" % (p_latency or 0) if p_ok
                                      else "关闭/不可达"))

    output_probe = output.probe()
    if output_probe["ok"]:
        checks.ok(prefix + "output", "io", "%s输出目录可达：%s" % (label, output.describe()))
    else:
        checks.warn(prefix + "output", "io", "%s输出目录暂不可达（运行时会尝试创建）：%s"
                    % (label, output_probe["detail"]))

    # 可写性：本地写临时文件，SSH 远端 touch + rm
    test_name = ".subtitle-studio-write-test-%d" % os.getpid()
    output_path = output_cfg.get("path") or ""
    try:
        if output.is_local:
            target_dir = expand(output_path)
            ensure_dir(target_dir)
            test_path = os.path.join(target_dir, test_name)
            with open(test_path, "w", encoding="utf-8") as handle:
                handle.write("ok\n")
            os.unlink(test_path)
            checks.ok(prefix + "output.writable", "io", "%s输出目录可写：%s" % (label, target_dir))
        else:
            test_path = "%s/%s" % (output_path.rstrip("/"), test_name)
            cmd = "mkdir -p %s && printf ok > %s && rm -f %s && echo __ST_OK__" % (
                _q(output_path), _q(test_path), _q(test_path))
            rc, out, err = output.run_remote(cmd, timeout=25)
            if rc == 0 and "__ST_OK__" in (out or ""):
                checks.ok(prefix + "output.writable", "io", "%s输出目录可写（远端）" % label)
            else:
                checks.error(prefix + "output.writable", "io",
                             "%s输出目录不可写（rc=%s）：%s" % (label, rc, (err or out).strip()[-300:]))
    except Exception as exc:  # noqa: BLE001
        checks.error(prefix + "output.writable", "io", "%s输出目录写测试失败：%s" % (label, exc))


def _q(value):
    from lib.runtime import shquote
    return shquote(value)


def check_disk(checks, cfg):
    min_free = float(cfg.get("limits.minFreeGB") or 15) * (1024 ** 3)
    available = df_avail_bytes(cfg.data_dir)
    if available is None:
        checks.warn("disk.local", "disk", "无法读取本地磁盘余量")
    elif available < min_free:
        checks.error("disk.local", "disk",
                     "本地磁盘余量不足：%s < %s"
                     % (human_size(available), human_size(min_free)))
    else:
        checks.ok("disk.local", "disk", "本地磁盘余量 %s（阈值 %s）"
                  % (human_size(available), human_size(min_free)))

    output = Endpoint(cfg.get("output.kind"), cfg.get("output.path"),
                      cfg.get("output.ssh"))
    if output.is_local:
        return
    try:
        info = output.df()
    except EngineError as exc:
        checks.warn("disk.remote", "disk", "无法读取远端磁盘余量：%s" % exc.message)
        return
    if not info:
        checks.warn("disk.remote", "disk", "远端 df 输出无法解析")
        return
    level = "info"
    message = "远端磁盘可用 %s / %s" % (human_size(info.get("availBytes")),
                                       human_size(info.get("totalBytes")))
    if info.get("availBytes") is not None and info["availBytes"] < 2 * (1024 ** 3):
        level = "warn"
        message += "（低于 2GB，可能上传失败）"
    checks.add("disk.remote", "disk", level, message, info)


def check_data_dir(checks, cfg):
    try:
        ensure_dir(cfg.data_dir)
        for path in (cfg.work_dir, cfg.logs_dir, cfg.state_dir):
            ensure_dir(path)
        test_path = os.path.join(cfg.state_dir, ".write-test")
        with open(test_path, "w", encoding="utf-8") as handle:
            handle.write("ok\n")
        os.unlink(test_path)
        checks.ok("dataDir", "paths", "数据目录可写：%s" % cfg.data_dir)
    except OSError as exc:
        checks.error("dataDir", "paths", "数据目录不可写：%s" % exc, {"dir": cfg.data_dir})


def check_notify(checks, cfg):
    if not cfg.get("notify.enabled"):
        checks.info("notify", "notify", "通知已关闭（notify.enabled=false）")
        return
    channel = cfg.get("notify.channel") or "none"
    if channel == "none":
        checks.info("notify", "notify", "通知渠道为 none（不发送任何消息）")
        return
    if channel == "qqbot":
        app_id = (cfg.get("notify.qqAppId") or "").strip()
        open_id = (cfg.get("notify.qqOpenId") or "").strip()
        if not app_id:
            path = expand(cfg.get("notify.qqSendPath") or "")
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                checks.warn("notify", "notify",
                            "未配置 qqAppId，回退到自定义脚本：%s" % path)
            else:
                checks.error("notify", "notify",
                             "channel=qqbot 但既没有 appId 也没有可用的 qqSendPath",
                             {"hint": "在设置 → 字幕工坊 → 通知里填 appId / openid 并保存密钥"})
            return
        if not open_id:
            checks.error("notify", "notify", "channel=qqbot 缺少 notify.qqOpenId",
                         {"hint": "用你的 QQ 私聊机器人一次，从开放平台事件里复制 openid"})
            return
        secret, source = notify_mod.qq_secret(cfg.get("notify") or {})
        if secret:
            checks.ok("notify", "notify",
                      "QQ Bot 直连已就绪：appId=%s，密钥来自 %s" % (app_id, source))
        else:
            checks.error("notify", "notify",
                         "QQ Bot clientSecret 未找到（keychain 服务 %r）"
                         % (cfg.get("notify.qqSecretService") or "subtitle-studio-qqbot"),
                         {"hint": "在通知卡里粘贴 clientSecret 并点「保存密钥」"})
        return
    if channel == "command":
        checks.ok("notify", "notify", "自定义命令：%s" % (cfg.get("notify.command") or "（空）"))
        return
    if channel == "webhook":
        checks.ok("notify", "notify", "Webhook：%s" % (cfg.get("notify.webhookUrl") or "（空）"))
        return
    checks.warn("notify", "notify", "未知通知渠道：%s" % channel)


def check_prompts(checks, cfg):
    default_system = (cfg.get("prompts.default.system") or "").strip()
    if not default_system:
        checks.warn("prompts.default", "prompts",
                    "prompts.default.system 为空，将使用 vendor 内置提示词")
    else:
        checks.ok("prompts.default", "prompts",
                  "默认提示词 %d 字" % len(default_system))
    profiles = cfg.get("prompts.profiles") or []
    broken = [p.get("name") for p in profiles if not (p.get("match") or [])]
    if broken:
        checks.warn("prompts.profiles", "prompts",
                    "以下 profile 缺少 match（永不命中）：%s" % ", ".join(map(str, broken)))
    else:
        checks.ok("prompts.profiles", "prompts", "%d 个 prompt profile" % len(profiles))
    series = [p.get("series") for p in profiles if (p.get("series") or "").strip()]
    if series:
        checks.ok("prompts.series", "prompts", "引用术语库：%s" % ", ".join(series))


# ---------------------------------------------------------------- 入口

def run_doctor(cfg):
    checks = Checks()
    check_config(checks, cfg)
    check_python(checks)
    check_ffmpeg(checks, cfg)
    check_ffprobe(checks, cfg)
    check_whisper(checks, cfg)
    check_engine(checks, cfg)
    check_endpoints(checks, cfg)
    check_disk(checks, cfg)
    check_data_dir(checks, cfg)
    check_notify(checks, cfg)
    check_prompts(checks, cfg)
    counts, verdict = checks.summary()
    return {
        "version": ENGINE_VERSION,
        "generatedAt": now_iso(),
        "configPath": cfg.path,
        "dataDir": cfg.data_dir,
        "engine": {"kind": cfg.get("engine.kind"),
                   "model": pipeline.model_for_engine(cfg)},
        "checks": checks.items,
        "summary": counts,
        "verdict": verdict,
    }

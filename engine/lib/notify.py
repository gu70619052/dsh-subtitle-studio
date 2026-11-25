# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 通知。

四个渠道，都做成旁路：发送失败只写日志，绝不影响主流程。
  qqbot    QQ Bot 官方 API 直连（手填 appId / openid，clientSecret 存 keychain 或
           SUBTITLE_QQ_SECRET 环境变量）；若只填了 qqSendPath 则回退到自定义脚本
  command  调用户手填的命令，文本从 stdin 与 $SUBTITLE_NOTIFY_TEXT 同时提供，
           命令里可用 {text} / {title} 占位符（已做 shell 转义）
  webhook  POST JSON 到 notify.webhookUrl，body 用 notify.webhookBody 模板，
           默认 {"text": "{text}"}（企微/钉钉机器人可直接用；飞书把模板改成
           {"msg_type":"text","content":{"text":"{text}"}}）
  none     关闭
"""

import json
import os
import shlex
import subprocess
import time
import urllib.parse
import urllib.request

from lib.keys import get_keychain_password
from lib.runtime import expand, run_cmd

PREFIX = "【字幕工坊】"
MAX_CHARS = 1800


def should_notify(cfg, event):
    """event ∈ start / success / failure；默认 start=关、success/failure=开。"""
    notify = cfg.get("notify") or {}
    if not notify.get("enabled", True):
        return False
    key = {"start": "onStart", "success": "onSuccess", "failure": "onFailure"}.get(event)
    return bool(notify.get(key)) if key else False


def _compose(title, body):
    text = "%s%s" % (PREFIX, title)
    if body:
        text = "%s\n%s" % (text, body)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…（已截断）"
    return text


QQ_API_TOKEN = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_SEND = "https://api.sgroup.qq.com/v2/users/%s/messages"
QQ_CHUNK = 800


def qq_secret(notify):
    """返回 (secret, source)。环境变量优先，其次 keychain（服务名 + 账号=appId）。"""
    value = (os.environ.get("SUBTITLE_QQ_SECRET") or "").strip()
    if value:
        return value, "env:SUBTITLE_QQ_SECRET"
    service = (notify.get("qqSecretService") or "subtitle-studio-qqbot").strip()
    account = (notify.get("qqAppId") or "").strip() or None
    value = get_keychain_password(service, account=account) or ""
    return value, ("keychain:%s" % service if value else "")


def _write_token_cache(path, payload):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def qq_token(app_id, secret, state_dir, logger=None, force=False):
    """取 access_token，优先用 state/qq-token.json 缓存（提前 60s 视为过期）。"""
    cache_path = os.path.join(state_dir, "qq-token.json")
    if not force:
        try:
            with open(cache_path, "rb") as handle:
                cached = json.loads(handle.read().decode("utf-8"))
            if (cached.get("appId") == app_id
                    and float(cached.get("expiresAt") or 0) - 60 > time.time()):
                return cached.get("accessToken") or "", "cache"
        except (OSError, ValueError):
            pass
    body = json.dumps({"appId": app_id, "clientSecret": secret}).encode("utf-8")
    request = urllib.request.Request(QQ_API_TOKEN, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    token = payload.get("access_token") or ""
    if token:
        _write_token_cache(cache_path, {
            "appId": app_id,
            "accessToken": token,
            "expiresAt": time.time() + float(payload.get("expires_in") or 7200),
        })
        if logger:
            logger.info("QQ access_token 已刷新（ttl=%ss）" % (payload.get("expires_in") or "?"))
    return token, "fresh"


def split_text(text, limit=QQ_CHUNK):
    """按行切分，单行超长时硬切，保证每段 <= limit。"""
    chunks, current = [], ""
    for line in text.splitlines(True):
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()] or [text[:limit]]


def _post_qq_message(open_id, text, token, app_id, seq):
    body = json.dumps({"content": text, "msg_type": 0, "msg_seq": seq}).encode("utf-8")
    request = urllib.request.Request(
        QQ_API_SEND % urllib.parse.quote(open_id), data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "QQBot %s" % token,
                 "X-Union-Appid": app_id})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.getcode(), response.read(400).decode("utf-8", "replace")


def _send_qqbot_direct(cfg, notify, text, logger):
    app_id = (notify.get("qqAppId") or "").strip()
    open_id = (notify.get("qqOpenId") or "").strip()
    if not app_id or not open_id:
        if logger:
            logger.warn("qqbot 通道缺少 notify.qqAppId / notify.qqOpenId，跳过通知")
        return False, "qqbot 未配置 appId/openid", ""
    secret, source = qq_secret(notify)
    if not secret:
        if logger:
            logger.warn("qqbot 通道缺少 clientSecret（keychain 服务 %r 或 SUBTITLE_QQ_SECRET）"
                        % (notify.get("qqSecretService") or "subtitle-studio-qqbot"))
        return False, "qqbot clientSecret 未配置", app_id
    if logger:
        logger.info("qqbot 直连：appId=%s openid=%s 来源=%s" % (app_id, open_id, source))
    chunks = split_text(text)
    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        payload = chunk if total == 1 else "[%d/%d] %s" % (index, total, chunk)
        seq = int(time.time_ns() % 2147483647)
        for attempt in (1, 2):
            try:
                token, _ = qq_token(app_id, secret, cfg.state_dir, logger, force=(attempt == 2))
                code, body = _post_qq_message(open_id, payload, token, app_id, seq)
                if 200 <= code < 300:
                    break
                if attempt == 2 or not (code in (401, 403) or code >= 500):
                    if logger:
                        logger.warn("qqbot 发送失败 status=%s：%s" % (code, body[:160]))
                    return False, "status=%s" % code, app_id
            except Exception as exc:  # noqa: BLE001 - 通知是旁路
                if attempt == 2:
                    if logger:
                        logger.warn("qqbot 发送异常：%s" % exc)
                    return False, str(exc), app_id
        if total > 1:
            time.sleep(0.5)
    if logger:
        logger.info("通知已发送（qqbot 直连 %d 段）" % total)
    return True, "", "qqbot:%s" % open_id


def _send_qqbot(cfg, notify, text, logger):
    """优先官方 API 直连；只配了 qqSendPath 时回退到自定义脚本（兼容旧配置）。"""
    if (notify.get("qqAppId") or "").strip():
        return _send_qqbot_direct(cfg, notify, text, logger)
    path = (notify.get("qqSendPath") or "").strip()
    if not path:
        if logger:
            logger.warn("qqbot 通道未配置 appId，也未配置 qqSendPath，跳过通知")
        return False, "qqbot not configured", ""
    path = expand(path)
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        if logger:
            logger.warn("通知脚本不存在或不可执行（%s），跳过通知" % path)
        return False, "notify script not found", path
    rc, out, err = run_cmd([path, text], timeout=60)
    detail = (out or err or "").strip().splitlines()
    if logger:
        if rc == 0:
            logger.info("通知已发送（自定义脚本 rc=0）")
        else:
            logger.warn("通知发送失败 rc=%s：%s" % (rc, (detail[-1] if detail else "")[:200]))
    return rc == 0, "" if rc == 0 else "rc=%s" % rc, path


def _send_command(notify, text, title, logger):
    template = (notify.get("command") or "").strip()
    if not template:
        if logger:
            logger.warn("notify.channel=command 但没有填 notify.command，跳过通知")
        return False, "command not configured", ""
    command = (template
               .replace("{text}", shlex.quote(text))
               .replace("{title}", shlex.quote(title)))
    env = dict(os.environ)
    env["SUBTITLE_NOTIFY_TEXT"] = text
    env["SUBTITLE_NOTIFY_TITLE"] = title
    try:
        proc = subprocess.run(command, shell=True, input=text, text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except Exception as exc:  # noqa: BLE001 - 通知是旁路
        if logger:
            logger.warn("notify.command 执行异常：%s" % exc)
        return False, str(exc), command
    detail = (proc.stdout or proc.stderr or "").strip().splitlines()
    ok = proc.returncode == 0
    if logger:
        if ok:
            logger.info("通知已发送（command rc=0）")
        else:
            logger.warn("notify.command 失败 rc=%s：%s" % (proc.returncode, (detail[-1] if detail else "")[:200]))
    return ok, "" if ok else "rc=%s" % proc.returncode, command


def _send_webhook(notify, text, title, logger):
    url = (notify.get("webhookUrl") or "").strip()
    if not url.startswith(("http://", "https://")):
        if logger:
            logger.warn("notify.webhookUrl 无效（%r），跳过通知" % url)
        return False, "webhook url invalid", url
    template = (notify.get("webhookBody") or "").strip() or '{"text": "{text}"}'
    try:
        payload = template.replace("{text}", _json_escape(text)).replace("{title}", _json_escape(title))
        data = payload.encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            code = response.getcode()
            body = response.read(400).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warn("webhook 通知失败：%s" % exc)
        return False, str(exc), url
    ok = 200 <= code < 300
    if logger:
        if ok:
            logger.info("通知已发送（webhook %s）" % code)
        else:
            logger.warn("webhook 返回 %s：%s" % (code, body[:160]))
    return ok, "" if ok else "status=%s" % code, url


def _json_escape(text):
    """把文本转成可嵌进 JSON 字符串字面量的片段（去掉两端引号）。"""
    return json.dumps(text)[1:-1]


def send(cfg, title, body, logger=None):
    """发送一条通知，返回 {sent, reason, channel}；从不抛异常。"""
    notify = cfg.get("notify") or {}
    channel = notify.get("channel") or "none"
    if not notify.get("enabled", True):
        return {"sent": False, "reason": "notify.enabled=false", "channel": channel}
    if channel == "none":
        return {"sent": False, "reason": "notify.channel=none", "channel": channel}

    text = _compose(title, body)
    try:
        if channel == "qqbot":
            ok, reason, target = _send_qqbot(cfg, notify, text, logger)
        elif channel == "command":
            ok, reason, target = _send_command(notify, text, title, logger)
        elif channel == "webhook":
            ok, reason, target = _send_webhook(notify, text, title, logger)
        else:
            if logger:
                logger.warn("通知渠道 %r 未实现，已跳过" % channel)
            return {"sent": False, "reason": "channel %s not supported" % channel, "channel": channel}
    except Exception as exc:  # noqa: BLE001 - 通知是旁路，任何异常都不能影响主流程
        if logger:
            logger.warn("通知发送异常：%s" % exc)
        return {"sent": False, "reason": str(exc), "channel": channel}
    return {"sent": ok, "reason": reason, "channel": channel, "target": target}

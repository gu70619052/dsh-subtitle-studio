# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 密钥读取。

规则（DESIGN §3）：config.json 只保存 apiKeyRef，明文密钥一律来自
macOS keychain（security find-generic-password 三策略），或进程环境变量。
"""

import os
import subprocess

# 环境变量名映射：引擎类型 -> 可直接覆盖的环境变量（不进磁盘，便于 CI/临时测试）
ENV_OVERRIDES = {
    "minimax": ["MINIMAX_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "openai-compatible": ["SUBTITLE_API_KEY", "OPENAI_API_KEY"],
    "ollama": [],
    # dsh 引擎不需要 key：凭据由 DSH 宿主持有，引擎只负责发文本
    "dsh": [],
}


def _security(args):
    try:
        proc = subprocess.run(["security"] + args,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def get_keychain_password(name, account=None):
    """三策略依次尝试，命中即返回明文，全失败返回 ""。

    1) -l NAME        按“标签”找（GUI 里改名后仍可命中）
    2) -a ACCOUNT -s NAME  按账号 + 服务名找
    3) -s NAME        只按服务名找（最常见）
    """
    if not name:
        return ""
    for args in (
        ["find-generic-password", "-l", name, "-w"],
        ["find-generic-password", "-a", account or name, "-s", name, "-w"],
        ["find-generic-password", "-s", name, "-w"],
    ):
        value = _security(args)
        if value:
            return value
    return ""


def mask(secret):
    """日志里可安全展示的密钥指纹。"""
    if not secret:
        return "(未配置)"
    secret = str(secret)
    if len(secret) <= 8:
        return "*" * len(secret)
    return "%s...%s" % (secret[:4], secret[-4:])


def has_keychain_item(name):
    return bool(get_keychain_password(name))


def resolve_api_key(engine_kind, api_key_ref, logger=None):
    """返回 (key, source)。source ∈ env / keychain:<ref> / (空)。

    优先环境变量（不落盘），其次 keychain。
    """
    for env_name in ENV_OVERRIDES.get(engine_kind, []):
        value = os.environ.get(env_name, "")
        if value:
            if logger:
                logger.info("API key 来自环境变量 %s（%s）" % (env_name, mask(value)))
            return value, "env:%s" % env_name
    if api_key_ref:
        value = get_keychain_password(api_key_ref)
        if value:
            if logger:
                logger.info("API key 来自 keychain 条目 %s（%s）" % (api_key_ref, mask(value)))
            return value, "keychain:%s" % api_key_ref
    if logger and engine_kind != "dsh":
        logger.warn("未找到 API key（apiKeyRef=%s，checked env=%s）"
                    % (api_key_ref or "(空)", ENV_OVERRIDES.get(engine_kind, [])))
    return "", ""


def keychain_preflight(api_key_ref):
    """doctor 用：返回 {found, masked, source}，不泄露明文。"""
    if not api_key_ref:
        return {"found": False, "ref": "", "masked": "", "source": ""}
    value = get_keychain_password(api_key_ref)
    return {
        "found": bool(value),
        "ref": api_key_ref,
        "masked": mask(value) if value else "",
        "source": "keychain" if value else "",
    }

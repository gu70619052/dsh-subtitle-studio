# -*- coding: utf-8 -*-
"""字幕工坊引擎 - prompt profile 匹配与合并。

规则：
- 用文件名（basename）与完整路径分别做 glob 匹配，命中第一个 profile 即止；
- profile.system 为空则继承 default.system；profile.context 与 default.context 拼接；
- series 字段只是“可选引用”，引擎据此从用户词表目录加载同名 JSON 术语表
  （~/.dsh/data/subtitle-studio/series/<name>.json）；不命中就完全不带领域词表。
"""

import fnmatch
import os


def _matches(filename, full_path, patterns):
    candidates = [filename]
    if full_path:
        candidates.append(full_path)
    for pattern in patterns or []:
        for text in candidates:
            if fnmatch.fnmatch(text.lower(), str(pattern).lower()):
                return True
    return False


def match_profile(profiles, filename, full_path=None):
    """返回 (profile, index) 或 (None, None)。"""
    for index, profile in enumerate(profiles or []):
        if not isinstance(profile, dict):
            continue
        if _matches(filename, full_path, profile.get("match")):
            return profile, index
    return None, None


def build(cfg, filename, full_path=None):
    """合并 default + 命中的 profile，返回可直接喂给 vendor 的 prompt 集合。"""
    default = cfg.get("prompts.default") or {}
    profile, index = match_profile(cfg.get("prompts.profiles"), filename, full_path)
    profile = profile or {}

    system = (profile.get("system") or "").strip() or (default.get("system") or "").strip()
    parts = []
    for chunk in (default.get("context") or "", profile.get("context") or ""):
        chunk = (chunk or "").strip()
        if chunk:
            parts.append(chunk)
    context = "\n\n".join(parts)
    series = (profile.get("series") or "").strip()

    return {
        "matched": bool(profile),
        "profileName": profile.get("name") or "",
        "profileIndex": index,
        "system": system,
        "context": context,
        "series": series,
    }


def describe(prompt_set):
    """日志用的一行描述。"""
    bits = []
    if prompt_set.get("matched"):
        bits.append("profile=%s" % (prompt_set.get("profileName") or "#%s" % prompt_set.get("profileIndex")))
    else:
        bits.append("profile=default")
    if prompt_set.get("series"):
        bits.append("series=%s" % prompt_set["series"])
    if prompt_set.get("context"):
        bits.append("context=%d chars" % len(prompt_set["context"]))
    if prompt_set.get("system"):
        bits.append("system=%d chars" % len(prompt_set["system"]))
    return " ".join(bits)

# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 选片。

包含规则（source.include 已在列目录阶段过滤）、排除、体积/龄期、
跳过已翻译、跳过历史失败名单、排序、配额（maxPerRun 与今日已用），
以及 dry-run 可展示的跳过原因。
"""

import fnmatch
import os

GB = 1024.0 ** 3

REASON_TEXT = {
    "eligible": "候选",
    "excluded": "命中排除规则",
    "too-small": "体积小于下限",
    "too-large": "超过体积上限",
    "too-old": "超过最大龄期",
    "translated-exists": "输出目录已有译制成品",
    "skip-list": "历史失败名单（见 state/skipped.json）",
    "quota-exhausted": "今日配额已用完",
    "quota-limit": "超出本次 maxPerRun",
    "only-filter": "--only 过滤未命中",
    "not-include": "不匹配 include 规则",
    "selected": "已选中",
}


def _split_template_ext(template):
    """拆出模板里写死的扩展名。

    "{stem}_zh.mp4" -> ("{stem}_zh", "mp4")，扩展名写死在模板里；
    "{stem}_zh.{ext}" -> ("{stem}_zh.{ext}", None)，扩展名是占位符，
    格式化时再决定。
    """
    head, sep, tail = template.rpartition(".")
    if not sep or not head or "{" in tail or "}" in tail:
        return template, None
    return head, tail


def output_name(name_template, filename, ext=None):
    """按 output.nameTemplate 计算成品文件名（{stem}/{name}/{ext}）。

    ext 给定时用它当成品扩展名 —— SRT 模式产出的是 .srt，不能沿用视频
    模板里的 .mp4，否则「输出目录已有成品」的跳过判断会失效。
    模板里的扩展名两种写法都要能换：
      "{stem}_zh.mp4"（写死） -> "{stem}_zh.srt"
      "{stem}_zh.{ext}"（占位）-> "{stem}_zh.srt"
      "{name}"（带原扩展名） -> "movie.srt"
    """
    base = os.path.basename(filename)
    stem, dot_ext = os.path.splitext(base)
    source_ext = dot_ext.lstrip(".")
    template = name_template or "{stem}_zh.mp4"
    if not ext:
        try:
            return template.format(stem=stem, name=base, ext=source_ext)
        except (KeyError, IndexError, ValueError):
            # 模板非法时 config 校验已报错，这里兜底
            return "%s_zh.%s" % (stem, source_ext or "mp4")

    target_ext = ext
    head, literal_ext = _split_template_ext(template)
    if literal_ext is not None:
        try:
            return "%s.%s" % (head.format(stem=stem, name=base, ext=target_ext), target_ext)
        except (KeyError, IndexError, ValueError):
            pass
    try:
        result = template.format(stem=stem, name=base, ext=target_ext)
    except (KeyError, IndexError, ValueError):
        return "%s_zh.%s" % (stem, target_ext or "mp4")
    # 模板用的是 {name} 之类，扩展名还留在结果里 —— 换成目标扩展名
    result_stem, result_ext = os.path.splitext(result)
    if result_ext.lstrip(".").lower() != target_ext.lower():
        result = result_stem + "." + target_ext
    return result


def _match_any(name, patterns):
    low = name.lower()
    for pattern in patterns or []:
        if fnmatch.fnmatch(low, str(pattern).lower()):
            return pattern
    return None


def make_candidate(entry):
    size = int(entry.get("size") or 0)
    return {
        "name": entry.get("name"),
        "path": entry.get("path"),
        "sizeBytes": size,
        "sizeGB": round(size / GB, 3),
        "mtime": entry.get("mtime"),
        "mtimeISO": entry.get("mtimeISO"),
        "ageDays": entry.get("ageDays"),
        "expectedOutput": None,
        "selected": False,
        "reason": None,
        "reasonText": None,
    }


def sort_candidates(candidates, order):
    """按 order 对候选排序（已跳过的也参与排序，输出顺序稳定）。"""
    order = order or "mtime-asc"
    if order.split("-")[0] == "name":
        reverse = order.endswith("desc")
        candidates.sort(key=lambda c: (c["name"] or "").lower(), reverse=reverse)
    elif order.split("-")[0] == "size":
        reverse = order.endswith("desc")
        candidates.sort(key=lambda c: (c["sizeBytes"], (c["name"] or "").lower()), reverse=reverse)
    else:
        reverse = order.endswith("desc")
        candidates.sort(key=lambda c: (float(c["mtime"] or 0), (c["name"] or "").lower()),
                        reverse=reverse)
    return candidates


def _in_skip_list(skip_list, name, prefix=""):
    """是否命中历史失败名单。

    多目录模式下优先匹配带目录前缀的键；无前缀的旧条目对所有目录生效
    （保持旧名单的既有语义，不做静默失效）。
    """
    if not skip_list:
        return False
    if prefix:
        if (prefix + name) in skip_list:
            return True
    return name in skip_list


def select(files, cfg, existing_outputs=None, used_today=0,
           only=None, skip_list=None, force=False, name_template=None,
           skip_key_prefix="", output_ext=None):
    """返回 {candidates, selected, counts, quota, effectiveLimit, order}。

    files: remote.Endpoint.list_files() 的条目
    existing_outputs: 输出目录已有文件名集合
    used_today: 今日已成功处理的条数（state/usage-YYYYMMDD.json）
    only: 显式指定单个文件时（force=True）绕过配额与“已翻译/失败名单”跳过
    skip_key_prefix: 多目录模式下的名单键前缀（不同目录的同名文件互不牵连）
    output_ext: 成品扩展名覆盖（SRT 模式产出 .srt，不是模板里的 .mp4）
    """
    sel = cfg.get("selection") or {}
    existing_outputs = set(existing_outputs or [])
    skip_list = skip_list or {}
    template = name_template or (cfg.get("output.nameTemplate") or "{stem}_zh.mp4")
    max_per_run = int(sel.get("maxPerRun") or 0)
    skip_translated = bool(sel.get("skipTranslated", True))
    respect_skip_list = bool(sel.get("respectSkipList", True))
    min_size = float(sel.get("minSizeGB") or 0) * GB
    max_size = float(sel.get("maxSizeGB") or 0) * GB
    max_age = float(sel.get("maxAgeDays") or 0)
    excludes = sel.get("excludePatterns") or []
    order = sel.get("order") or "mtime-asc"

    candidates = []
    for entry in files:
        cand = make_candidate(entry)
        name = cand["name"] or ""
        cand["expectedOutput"] = output_name(template, name, ext=output_ext)
        reason = None

        if only:
            needle = only.lower()
            if name.lower() != needle and needle not in name.lower():
                reason = "only-filter"

        if reason is None:
            hit = _match_any(name, excludes)
            if hit:
                reason = "excluded"

        if reason is None and min_size > 0 and cand["sizeBytes"] < min_size:
            reason = "too-small"
        if reason is None and max_size > 0 and cand["sizeBytes"] > max_size:
            reason = "too-large"
        if reason is None and max_age > 0 and float(cand["ageDays"] or 0) > max_age:
            reason = "too-old"
        if reason is None and skip_translated and cand["expectedOutput"] in existing_outputs:
            reason = "translated-exists"
        if reason is None and respect_skip_list and _in_skip_list(skip_list, name, skip_key_prefix):
            reason = "skip-list"
        if force and reason in ("translated-exists", "skip-list"):
            reason = None

        cand["reason"] = reason
        cand["reasonText"] = REASON_TEXT.get(reason or "eligible", reason)
        candidates.append(cand)
    # 排序（已跳过的也参与排序，输出顺序稳定、便于人看）
    sort_candidates(candidates, order)

    if max_per_run > 0 and not force:
        remaining = max(0, max_per_run - int(used_today or 0))
    else:
        remaining = -1  # 不限额（maxPerRun=0 或 --only 显式指定）

    selected = []
    slots = 0
    for cand in candidates:
        if cand["reason"] is not None:
            continue
        if remaining >= 0 and slots >= remaining:
            cand["reason"] = "quota-limit" if remaining > 0 else "quota-exhausted"
            cand["reasonText"] = REASON_TEXT[cand["reason"]]
            continue
        cand["selected"] = True
        cand["reason"] = "selected"
        cand["reasonText"] = REASON_TEXT["selected"]
        selected.append(cand)
        slots += 1

    counts = {"total": len(candidates), "selected": len(selected)}
    counts["skipped"] = counts["total"] - counts["selected"]
    by_reason = {}
    for cand in candidates:
        by_reason[cand["reason"]] = by_reason.get(cand["reason"], 0) + 1
    counts["byReason"] = by_reason
    quota = {
        "maxPerRun": max_per_run,
        "usedToday": int(used_today or 0),
        "remaining": remaining,
        "unlimited": max_per_run <= 0,
    }
    return {
        "candidates": candidates,
        "selected": selected,
        "counts": counts,
        "quota": quota,
        "order": order,
        "nameTemplate": template,
    }

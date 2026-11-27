# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 选片。

包含规则（source.include 已在列目录阶段过滤）、排除、体积/龄期、
跳过已翻译、跳过历史失败名单、排序、配额（maxPerRun 与今日已用），
以及 dry-run 可展示的跳过原因。
"""

import fnmatch
import os
import re

GB = 1024.0 ** 3

REASON_TEXT = {
    "eligible": "候选",
    "excluded": "命中排除规则",
    "too-small": "体积小于下限",
    "too-large": "超过体积上限",
    "too-old": "超过最大龄期",
    "translated-exists": "输出目录已有译制成品",
    "translated-ledger": "来源片已译过（成品台账命中，成品仍在）",
    "own-output": "看起来是本插件自己的成品（按命名模板反推）",
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


def template_matcher(name_template):
    """把命名模板变成一个「这个名字像是模板产出的」匹配器。

    `{stem}` / `{name}` / `{ext}` 都当通配，其余按字面。模板里没有字面
    内容时（比如就一个 `{name}`）返回 None —— 那种模板下任何文件都可能是
    成品，不能拿来排除，否则会把整个片源库排空。
    """
    template = (name_template or "").strip()
    if not template:
        return None
    head, sep, tail = template.rpartition(".")
    stem_part = head if sep else template
    literal = stem_part
    for token in ("{stem}", "{name}", "{ext}"):
        literal = literal.replace(token, "")
    # 字面部分只剩标点/扩展名时不足以认成品（`{name}` 或 `{stem}.{ext}`），
    # 那种模板下任何文件都可能是产出，拿来排除会排空片源。
    if len(re.sub(r"[^0-9A-Za-z一-鿿_-]", "", literal)) < 2:
        return None
    pattern = re.escape(stem_part)
    for token in ("{stem}", "{name}", "{ext}"):
        pattern = pattern.replace(re.escape(token), "[^/]*")
    if "{" in literal:
        # 模板里有我们不认识的占位符（`{stem.lower()}`、`{title}`…）：
        # 那样拼出来的正则要求文件名里有字面花括号，永远匹配不上，
        # 与其静默失效不如不启用这条规则。
        return None
    if sep:
        # 扩展名放开口子：换过输出方式后旧成品是 _zh.mp4、新成品是 _zh.srt，
        # 两种都该认出来，否则切一次模式就把历史成品全当成新片。
        # `{stem}_zh.{ext}` 这种把扩展名写成占位符的模板同样走这里（R3）。
        pattern = pattern + r"\.[A-Za-z0-9]+"
    pattern = pattern + "$"
    try:
        return re.compile("^" + pattern)
    except re.error:
        return None


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
           skip_key_prefix="", output_ext=None, translated_ledger=None,
           skip_own_outputs=True, output_dir=None):
    """返回 {candidates, selected, counts, quota, effectiveLimit, order}。

    files: remote.Endpoint.list_files() 的条目
    existing_outputs: 输出目录已有文件名集合
    used_today: 今日已成功处理的条数（state/usage-YYYYMMDD.json）
    only: 显式指定单个文件时（force=True）绕过配额与“已翻译/失败名单”跳过
    skip_key_prefix: 多目录模式下的名单键前缀（不同目录的同名文件互不牵连）
    output_ext: 成品扩展名覆盖（SRT 模式产出 .srt，不是模板里的 .mp4）
    translated_ledger: {源片名: 成品路径}，来自 state/translated.json 且已校验
        成品仍在。命中即跳过 —— 这是「标签打在原视频上」的那条路，不依赖
        当前输出目录与输出方式（REQ-59）。
    skip_own_outputs: 名单里若有文件正好等于另一个文件的成品名，就当成自家
        成品排掉，避免输出目录==来源目录时 `X_zh.mp4` 被再翻成 `X_zh_zh.mp4`
        无限套（REQ-60）。
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
    ledger = translated_ledger or {}
    skip_own = bool(sel.get("skipOwnOutputs", True)) and bool(skip_own_outputs)
    own_outputs = set()
    own_re = template_matcher(template) if skip_own else None
    output_dir_abs = os.path.normpath(os.path.expanduser(str(output_dir or ""))) if output_dir else ""
    if skip_own:
        # 一个文件是自家成品 ⇔ 它等于同一批里某个文件的成品名。
        # 这样 `X_zh.mp4` 会被认出、而它自己的成品 `X_zh_zh.mp4` 就不会再被当成片源。
        for entry in files:
            sibling = os.path.basename(entry.get("name") or "")
            if not sibling:
                continue
            own_outputs.add(output_name(template, sibling, ext=output_ext))

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

        named = bool(only) and name.lower() == str(only).lower()
        if reason is None and skip_own and not named and name != cand["expectedOutput"]:
            # 两条认法：① 它正好是同批某个文件的成品名；② 它长得像模板产出的
            # 名字、且就躺在输出目录里。②覆盖「库里的成品被单独当片源」这种
            # 情况（片源与输出同目录、或直接把成品目录当来源），而①不需要
            # 输出目录也能命中。规则只在字面部分够长时启用，`{name}` 那种
            # 模板不会排掉任何东西。
            parent = os.path.normpath(os.path.dirname(entry.get("path") or ""))
            looks_own = (name in own_outputs) or (
                own_re is not None and output_dir_abs != "" and parent == output_dir_abs
                and own_re.match(name) is not None)
            if looks_own:
                reason = "own-output"

        if reason is None and min_size > 0 and cand["sizeBytes"] < min_size:
            reason = "too-small"
        if reason is None and max_size > 0 and cand["sizeBytes"] > max_size:
            reason = "too-large"
        if reason is None and max_age > 0 and float(cand["ageDays"] or 0) > max_age:
            reason = "too-old"
        if reason is None and skip_translated and cand["expectedOutput"] in existing_outputs:
            reason = "translated-exists"
            cand["translatedEvidence"] = os.path.join(
                str(cfg.get("output.path") or ""), cand["expectedOutput"])
        if reason is None and skip_translated and name in ledger:
            reason = "translated-ledger"
            cand["translatedEvidence"] = ledger[name]
        if reason is None and respect_skip_list and _in_skip_list(skip_list, name, skip_key_prefix):
            reason = "skip-list"
        # force / --only 是用户点名要处理这一条，自家成品的启发式判定要让路（R3）
        if force and reason in ("translated-exists", "translated-ledger", "skip-list",
                                "own-output"):
            reason = None
        if named and reason == "own-output":
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

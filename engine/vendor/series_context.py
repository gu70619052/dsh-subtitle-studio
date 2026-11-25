#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
series_context.py - 可选的「系列术语表」加载器

字幕翻译里最难缠的不是句子，而是专有名词：ASR 经常把一个圈子里的人名、
团体名、节目名听成音近的另一个词。这个模块负责把这些「听错 → 正确」的对照
交给模型，让它主动纠错。

包内**不预置任何具体作品的词表**（那是使用者自己的领域内容）。词表放在
用户数据目录里，按名字取：

    ~/.dsh/data/subtitle-studio/series/<name>.json

环境变量 SUBTITLE_STUDIO_SERIES_DIR 可覆盖这个目录。面板里在某个 prompt
profile 上填 `series: <name>`，引擎就会带上 `--series <name>` 调用本模块。

JSON 结构（见包内 vendor/series.example.json）：

    {
      "name": "示例系列",
      "title": "这段素材的背景说明（一行）",
      "mission": "对模型的核心任务补充说明",
      "sections": [
        {"when": ["*"], "heading": "人名（保留原文，不音译）", "items": ["…", "…"]}
      ],
      "asrFixes": [["听错的写法", "正确的写法", "default|context"]],
      "rules": ["- 专有名词保留原文", "…"],
      "postFix": {"常见错写": "正确写法"}
    }

`sections[].when` 用来做子集选择：`--series A` 时，`when` 含 `"*"` 或 `"A"`
的分段才会被带上，方便一个文件里放多个子系列。缺省视为 `["*"]`。

设计约束：任何异常（文件不存在、JSON 写坏、字段缺失）都只降级为「不带词表」
并给一行提示，绝不抛异常打断翻译流程。
"""

import json
import os
import sys

DEFAULT_SERIES_DIR = "~/.dsh/data/subtitle-studio/series"

Example = {
    "说明": "把本文件复制到 ~/.dsh/data/subtitle-studio/series/<名字>.json 后按需填写",
    "title": "这段素材属于某个特定圈子，专有名词多，ASR 容易听错",
    "mission": "识别 ASR 错位术语并纠错，时间戳严格保留，中文行去掉所有标点",
    "sections": [
        {
            "when": ["*"],
            "heading": "人名（必须保留原文，不音译）",
            "items": ["示例一号", "示例二号", "示例三号"],
        },
        {
            "when": ["sub"],
            "heading": "只在子系列 sub 里需要带的词",
            "items": ["子系列专有名词"],
        },
    ],
    "asrFixes": [
        ["示范错写", "示范正写", "default"],
        ["需要看上下文的错写", "正确的写法", "context"],
    ],
    "rules": [
        "- 人名/团体名/节目名：保留原文，不音译",
        "- 中英混排时中英文之间加一个英文空格",
    ],
    "postFix": {"示范错写": "示范正写"},
}


def series_dir():
    return os.path.expanduser(
        os.environ.get("SUBTITLE_STUDIO_SERIES_DIR") or DEFAULT_SERIES_DIR
    )


def available_series(directory=None):
    directory = directory or series_dir()
    names = []
    try:
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".json") and not entry.startswith("."):
                names.append(entry[:-5])
    except OSError:
        pass
    return names


def _safe_name(series):
    """只允许字母数字 . _ -，防止把路径分隔符当名字用。"""
    cleaned = "".join(ch for ch in str(series or "").strip() if ch.isalnum() or ch in "._-")
    return cleaned


def _load(series):
    name = _safe_name(series)
    if not name:
        return None
    path = os.path.join(series_dir(), name + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        print("术语表 %s 解析失败：%s" % (path, exc), file=sys.stderr, flush=True)
        return None
    return data if isinstance(data, dict) else None


def _as_list(value):
    """A field the JSON author got wrong must degrade, not raise."""
    return value if isinstance(value, (list, tuple)) else []


def _selection_keys(series):
    """一个 series 名字可能对应文件里的多个子集（'All' 代表全部）。"""
    raw = str(series or "").strip()
    keys = {raw, "*"}
    if raw.lower() in ("all", "*"):
        keys.add("")
    return keys


def get_context(series="All"):
    """把词表渲染成一段可以拼进 prompt 的文本；取不到就返回空串。"""
    if not series:
        return ""
    data = _load(series)
    if data is None:
        found = available_series()
        hint = ("可用词表：%s" % "、".join(found)) if found else (
            "目录里还没有任何词表（可把 vendor/series.example.json 复制为 "
            "%s/<名字>.json）" % series_dir())
        print("未找到术语表 %r（%s），本次不带词表。%s"
              % (series, _safe_name(series) + ".json", hint), file=sys.stderr, flush=True)
        return ""

    parts = []
    title = (data.get("title") or "").strip()
    mission = (data.get("mission") or "").strip()
    header = title or "这段素材包含容易听错的专有名词。"
    if mission:
        header += "\n**核心任务**：%s" % mission
    parts.append(header)
    parts.append("")

    keys = _selection_keys(series)
    for section in _as_list(data.get("sections")):
        if not isinstance(section, dict):
            continue
        when = _as_list(section.get("when")) or ["*"]
        if isinstance(when, str):
            when = [when]
        if not (set(str(w) for w in when) & keys):
            continue
        items = [str(i) for i in _as_list(section.get("items")) if str(i).strip()]
        if not items:
            continue
        parts.append("## %s" % (section.get("heading") or "专有名词"))
        parts.append("、".join(items))
        parts.append("")

    fixes = _as_list(data.get("asrFixes"))
    rendered = []
    seen = set()
    for row in fixes:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        wrong, correct = str(row[0]), str(row[1])
        mode = str(row[2]) if len(row) > 2 else "default"
        if not wrong or not correct or (wrong, correct) in seen:
            continue
        seen.add((wrong, correct))
        if mode == "context":
            rendered.append("- `%s` → `%s`（结合上下文判断）" % (wrong, correct))
        else:
            rendered.append("- `%s` → `%s`（基本就是这个）" % (wrong, correct))
    if rendered:
        parts.append("## ⚠️ ASR 错位纠错表（重点）")
        parts.append("ASR 经常把以下词听错，请**主动纠错**：\n")
        parts.extend(rendered)
        parts.append("")

    rules = [str(r) for r in _as_list(data.get("rules")) if str(r).strip()]
    if rules:
        parts.append("## 专有名词处理规范")
        parts.extend(rules)

    return "\n".join(parts).strip()


def fix_common_errors(text, series=None):
    """译文后处理：按词表里的 postFix 对照做一轮替换（只在确信时用）。"""
    if not text:
        return text
    data = _load(series) if series else None
    if data is None:
        return text
    fixes = data.get("postFix") or {}
    if not isinstance(fixes, dict):
        return text
    for wrong, correct in fixes.items():
        if wrong and correct:
            text = text.replace(str(wrong), str(correct))
    return text


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print("用法: python3 series_context.py [--list | <series 名字>]")
        print("词表目录: %s" % series_dir())
        sys.exit(0)
    if args[0] == "--list":
        names = available_series()
        print("\n".join(names) if names else "(目录为空: %s)" % series_dir())
        sys.exit(0)
    print(get_context(args[0]))

#!/usr/bin/env python3
"""字幕样式模板库（REQ-27）。

以前要改字幕外观，只能手写 ASS Style 行或 ffmpeg force_style —— 门槛高、
参数还分散在两个地方。现在把常见外观收成几个静态预设，配置里选一个即可。

预设只管「长什么样」（颜色、描边、阴影、位置、边距），不管字号：字号仍按
视频分辨率走 bilingual_ass.py 的 SIZE_TABLE，用户显式填了 subtitles.fontSize
时以用户为准。

颜色用 ASS 的 &HAABBGGRR 写法（AA 透明、BB 蓝、GG 绿、RR 红）：
    &H00FFFFFF = 白     &H0000FFFF = 黄     &H64000000 = 半透明黑（描边）
    &H00000000 = 纯黑   &H80000000 = 50% 透明黑（底框）

对齐用 ASS 的数字对齐（1-9，2 = 底部居中，8 = 顶部居中）。
"""

DEFAULT_STYLE_ID = "default"

STYLE_PRESETS = {
    "default": {
        "label": {"zh": "白字黑边（默认）", "en": "White with black outline (default)"},
        "primary": "&H00FFFFFF",
        "secondary": "&H000000FF",
        "outline_colour": "&H64000000",
        "back": "&H00000000",
        "bold": 1,
        "outline": 1.2,
        "shadow": 0.0,
        "border_style": 1,
        "alignment": 2,
        "margin_l": 20,
        "margin_r": 20,
        "margin_v": 16,
    },
    "bold-yellow": {
        "label": {"zh": "醒目黄字", "en": "Bold yellow"},
        "primary": "&H0000FFFF",
        "secondary": "&H000000FF",
        "outline_colour": "&H00000000",
        "back": "&H00000000",
        "bold": 1,
        "outline": 2.0,
        "shadow": 0.6,
        "border_style": 1,
        "alignment": 2,
        "margin_l": 24,
        "margin_r": 24,
        "margin_v": 24,
    },
    "top-white": {
        "label": {"zh": "顶部白字（纪录片）", "en": "Top white (documentary)"},
        "primary": "&H00FFFFFF",
        "secondary": "&H000000FF",
        "outline_colour": "&H00000000",
        "back": "&H00000000",
        "bold": 0,
        "outline": 1.6,
        "shadow": 0.0,
        "border_style": 1,
        "alignment": 8,
        "margin_l": 20,
        "margin_r": 20,
        "margin_v": 48,
    },
    "boxed": {
        "label": {"zh": "黑底白字（带底框）", "en": "White on black box"},
        "primary": "&H00FFFFFF",
        "secondary": "&H000000FF",
        "outline_colour": "&H00000000",
        "back": "&H00000000",
        "bold": 1,
        "outline": 0.0,
        "shadow": 0.0,
        "border_style": 3,
        "alignment": 2,
        "margin_l": 16,
        "margin_r": 16,
        "margin_v": 20,
    },
    "cinema": {
        "label": {"zh": "影院黑条白字", "en": "Cinema bar"},
        "primary": "&H00FFFFFF",
        "secondary": "&H000000FF",
        "outline_colour": "&H80000000",
        "back": "&H80000000",
        "bold": 0,
        "outline": 0.0,
        "shadow": 0.0,
        "border_style": 3,
        "alignment": 2,
        "margin_l": 24,
        "margin_r": 24,
        "margin_v": 30,
    },
}


def style_ids():
    """预设 id 列表（面板下拉的取值范围）。"""
    return list(STYLE_PRESETS.keys())


def get_preset(style_id):
    """按 id 取预设；未知 id 回落默认预设（永远不抛异常）。"""
    if not isinstance(style_id, str):
        return STYLE_PRESETS[DEFAULT_STYLE_ID]
    preset = STYLE_PRESETS.get(style_id.strip())
    if preset is None:
        return STYLE_PRESETS[DEFAULT_STYLE_ID]
    return preset


def preset_label(style_id, lang="zh"):
    """预设的界面文案。"""
    preset = get_preset(style_id)
    labels = preset.get("label") or {}
    return labels.get(lang) or labels.get("zh") or style_id


def _fmt_num(value):
    """整数就写整数（0 不写成 0.0），让默认预设的 Style 行与旧版逐字一致。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return ("%g" % number)


def ass_style_line(preset, font="PingFang SC", cn_size=20, margin_v=None):
    """生成 ASS 的 Style 行（双语压制走这条路，行内字号才生效）。"""
    if margin_v is None:
        margin_v = preset["margin_v"]
    return (
        "Style: Default,{font},{cn_size},{primary},{secondary},{outline_colour},{back},"
        "{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},"
        "{margin_l},{margin_r},{margin_v},1"
    ).format(
        font=font,
        cn_size=cn_size,
        primary=preset["primary"],
        secondary=preset["secondary"],
        outline_colour=preset["outline_colour"],
        back=preset["back"],
        bold=preset["bold"],
        border_style=preset["border_style"],
        outline=_fmt_num(preset["outline"]),
        shadow=_fmt_num(preset["shadow"]),
        alignment=preset["alignment"],
        margin_l=preset["margin_l"],
        margin_r=preset["margin_r"],
        margin_v=margin_v,
    )


def force_style(preset, font=None, font_size=None):
    """生成 ffmpeg subtitles= 滤镜的 force_style 参数（单行字幕走这条路）。

    libass 的 force_style 用逗号分隔键值；值里不能出现冒号或逗号，
    这里的取值都是颜色/数字，不会触发转义问题。
    """
    parts = []
    if font:
        parts.append("FontName=%s" % font)
    if font_size and int(font_size) > 0:
        parts.append("FontSize=%d" % int(font_size))
    parts.append("PrimaryColour=%s" % preset["primary"])
    parts.append("OutlineColour=%s" % preset["outline_colour"])
    parts.append("BackColour=%s" % preset["back"])
    parts.append("Bold=%d" % preset["bold"])
    parts.append("BorderStyle=%d" % preset["border_style"])
    parts.append("Outline=%s" % _fmt_num(preset["outline"]))
    parts.append("Shadow=%s" % _fmt_num(preset["shadow"]))
    parts.append("Alignment=%d" % preset["alignment"])
    parts.append("MarginL=%d" % preset["margin_l"])
    parts.append("MarginR=%d" % preset["margin_r"])
    parts.append("MarginV=%d" % preset["margin_v"])
    return ",".join(parts)

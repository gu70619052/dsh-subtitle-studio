"""翻译质量判定（REQ-61）。

只有一份实现。`lib/pipeline.py` 与 `vendor/translate_srt.py` 都从这里取，
因为两层曾经各算各的：引擎脚本按「第一行有没有汉字」数未译，流水线按
「有没有假名」数未译 —— 同一份产物一个报 2.5%、一个报 100%，日志与通知
互相打脸，而「含汉字」那条在整片没译时恰恰是最会骗人的（日文本就大量
使用汉字：舞台 / 鼓動 / 朝焼）。

标定用的真实样本（2026-11-27）：
- 正常译出的两集共 2404 块：假名残留 463 块（19.3%），抽看全是没译的日文；
  其中「正确中文里保留了一个日文台标名」只有 1 条。
- 一份全日文回落产物 118 块：假名占比 p10=0.59、中位 0.76。
- 一份正常译文 118 块：假名 0 条。
所以「含假名即未译」在真机数据上误报 1/2404，漏报 0 —— 用它当硬判定，
纯英文/数字那种可疑但不算失败的另列 suspect。
"""

import re

# 平假名 + 片假名 + 片假名音标扩展。写成码位范围是为了避开夹在中间的
# 谚文字母（U+3131–316F 属于「3040–33FF」这个连续区段，直接写首尾字符
# 会把韩文也当成日文假名）。
KANA_RE = re.compile(
    "[\\u3041-\\u309f\\u30a1-\\u30fa\\u30fc-\\u30ff\\u31f0-\\u31ff\\uff66-\\uff9f]"
)
# 平假名 U+3041- U+309F、片假名 U+30A1-U+30FA、长音符与假名修饰符
# U+30FC-U+30FF、片假名音标扩展 U+31F0-U+31FF、半角片假名 U+FF66-U+FF9F。
# 两处刻意不包含：**谚文字母 U+3131-U+318E**（夹在这些区段中间，写成
# U+3040-U+30FF 这样的整段会把韩文也算成日文）与 **中黒 U+30FB**（中文
# 也拿它当间隔号，「哈利・波特」不该被判未译）。ASR 偶发半角假名，早先
# 漏了 U+FF66-U+FF9F 会让那些行退化成 suspect 而少计未译。
# 中日共用汉字（只用来区分「有中文」与「谁都没写」）
IDEOGRAPH_RE = re.compile(r"[一-鿿]")
# 预过滤阶段的占位符，原样回传是正确行为，不算未译
PLACEHOLDER_RE = re.compile(r"^\[(听不清|广告宣番|♪ 歌词 ♪)\]$")

# classify() 的取值
EMPTY = "empty"
PLACEHOLDER = "placeholder"
UNTRANSLATED = "untranslated"
TRANSLATED = "translated"
SUSPECT = "suspect"


def classify(text):
    """把一条字幕判成 empty / placeholder / untranslated / translated / suspect。

    untranslated = 含假名（原文没被换掉）；suspect = 既没假名也没汉字，
    通常是专名、数字或符号行，报出来但不算失败。
    """
    body = (text or "").strip()
    if not body:
        return EMPTY
    if PLACEHOLDER_RE.match(body):
        return PLACEHOLDER
    if KANA_RE.search(body):
        return UNTRANSLATED
    if IDEOGRAPH_RE.search(body):
        return TRANSLATED
    return SUSPECT


def stats(lines, id_key=None):
    """统计一串字幕文本。

    lines: 字符串序列，或 (标识, 字符串) 二元组序列 —— 两种都自动识别，
    不再靠 id_key 决定形态（那份参数留着只为兼容旧调用）。
    返回 {"total","untranslated","suspect","untranslatedPct","suspectPct",
          "untranslatedIds","suspectIds"}；比例分母是 total（含空行与占位符），
    与旧口径一致，免得阈值含义在版本之间悄悄漂移。
    """
    total = 0
    untranslated = 0
    suspect = 0
    untranslated_ids = []
    suspect_ids = []
    for item in lines:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            ident, text = item[0], item[1]
        else:
            text, ident = item, total
        total += 1
        kind = classify(text)
        if kind == UNTRANSLATED:
            untranslated += 1
            untranslated_ids.append(ident)
        elif kind == SUSPECT:
            suspect += 1
            suspect_ids.append(ident)
    denom = total or 1
    return {
        "total": total,
        "untranslated": untranslated,
        "suspect": suspect,
        "untranslatedPct": round(untranslated * 100.0 / denom, 2),
        "suspectPct": round(suspect * 100.0 / denom, 2),
        "untranslatedIds": untranslated_ids[:80],
        "suspectIds": suspect_ids[:80],
    }


def verdict(counts, fail_pct, fail_count):
    """未译到什么程度算失败。

    比例与条数任一超标即判失败 —— 只看比例会让长片吞下几十条日文，只看
    条数会让短片随便漏一条就整片重跑。阈值来自
    `translation.rerunThresholdPct` / `translation.rerunThresholdCount`。
    """
    return (counts["untranslatedPct"] > fail_pct
            or counts["untranslated"] > fail_count)

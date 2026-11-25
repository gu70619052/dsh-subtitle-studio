#!/usr/bin/env python3
"""
生成精确时间戳的 SRT 字幕文件。
默认使用 MLX Whisper（Metal GPU 加速），兜底 faster-whisper（CPU）。

用法：
    python3 transcribe_srt.py <音频文件> [--output <输出SRT路径>] [--engine <mlx|faster>] [--language <语言>]

参数：
    音频文件        mp3/wav/m4a 等音频文件路径
    --output       输出 SRT 文件路径（默认：与音频同名 .srt）
    --engine       转写引擎（默认 mlx，可选 faster）
    --language     语言代码（默认自动检测，可指定 en/zh/ja 等）
    --max-line-ms  单条字幕最大时长毫秒数（默认 4000，超过则按词拆分。v2.0 从 6000 降为 4000 提升时间戳精度）
"""

import argparse
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# 反幻觉（REQ-02 / REQ-03）
#
# Whisper 在静音/噪声段容易吐出三种东西：复读、致谢片尾语、低置信度碎片。
# 这里做三件事：按置信度丢段、压掉复读、丢掉致谢片尾语。
# ---------------------------------------------------------------------------

ASR_OPTION_DEFAULTS = {
    # 不接上文：上一句的幻觉会传染给下一句（Whisper 默认 True）
    "condition_on_previous_text": False,
    # 温度回退上限：0 = 只用最可能结果；>0 时给一条递增阶梯，低置信片段自动升温重试
    "temperature": 0.0,
    # 静音超过这个秒数就认为解码不可靠（OpenAI 的 hallucination_silence_threshold）
    "hallucination_silence_threshold": None,
    # 低于该平均对数概率的片段直接丢（0 表示不过滤）
    "min_avg_logprob": None,
    # 高于该无语音概率的片段直接丢（0 表示不过滤）
    "max_no_speech_prob": None,
    # 丢掉「ご視聴ありがとうございました」这类致谢片尾语
    "drop_thanks": True,
    # 压掉复读
    "collapse_repeats": True,
}

# 整句匹配：这些是 Whisper 在无音频处最常吐的整句
# 一律忽略大小写 —— Whisper 常把句首字母大写（"Thanks for watching"），
# 大小写敏感会让这条片尾语漏网。
THANKS_PATTERNS = [
    re.compile(r"^ご視聴ありがとうございました", re.IGNORECASE),
    re.compile(r"^ご視聴ありがとうございます", re.IGNORECASE),
    re.compile(r"^ご視聴ありがとう", re.IGNORECASE),
    re.compile(r"^字幕(制作|提供|翻译|由|组)", re.IGNORECASE),
    re.compile(r"^subs?\s*by\b", re.IGNORECASE),
    re.compile(r"amara\.org", re.IGNORECASE),
    re.compile(r"^please\s+subscribe", re.IGNORECASE),
    re.compile(r"^thank you for watching", re.IGNORECASE),
    re.compile(r"^thanks for watching", re.IGNORECASE),
    re.compile(r"^you\s*tube", re.IGNORECASE),
]

# 子串匹配：只在整段很短时才用，避免误伤正文里出现的致谢
THANKS_SUBSTRINGS = (
    "ありがとうございました",
    "ありがとうございま",
    "thank you for watching",
    "thanks for watching",
    "请不吝点赞",
    "点赞订阅",
    "订阅点赞",
    "点击订阅",
    "欢迎订阅",
)

THANKS_SUBSTRING_MAX_LEN = 24


def build_options(**kwargs):
    """把配置里的 ASR 参数合成一份选项字典（缺的用默认值）。"""
    options = dict(ASR_OPTION_DEFAULTS)
    for key, value in kwargs.items():
        if key in options and value is not None:
            options[key] = value
    return options


def _temperature_ladder(temperature):
    """温度回退阶梯。

    0（或未设）→ 单个 0.0：只用最可能的结果，不重试，最快也最稳定。
    >0 → [0.0, 0.2, …, 上限]：解码看起来不可靠时自动升温重试。
    mlx-whisper 与 faster-whisper 都接受温度列表。
    """
    if not temperature or float(temperature) <= 0:
        return 0.0
    top = float(temperature)
    ladder = [0.0]
    for step in (0.2, 0.4, 0.6, 0.8, 1.0):
        if step < top:
            ladder.append(round(step, 2))
    ladder.append(top)
    return tuple(ladder)


def _normalize_text(text):
    """去掉首尾空白与常见装饰符号，便于整句匹配。"""
    s = (text or "").strip()
    s = re.sub(r"^[\s\-–—=~「」『』\[\]()（）·・]+", "", s)
    s = re.sub(r"[\s\-–—=~「」『』\[\]()（）·・]+$", "", s)
    return s


def _is_thanks_text(text):
    """是不是 Whisper 常吐的致谢/片尾语。"""
    s = _normalize_text(text)
    if not s:
        return False
    for pattern in THANKS_PATTERNS:
        if pattern.search(s):
            return True
    low = s.lower()
    if len(s) <= THANKS_SUBSTRING_MAX_LEN:
        for needle in THANKS_SUBSTRINGS:
            if needle in low:
                return True
    return False


def _collapse_repeat_text(text):
    """把复读压成一份。

    'ありがとうありがとうありがとう' → 'ありがとう'
    'subscri subscri subscri' → 'subscri'
    至少重复三次才动手，单次重复不动（真人确实会说两遍）。
    """
    s = (text or "").strip()
    if len(s) < 6:
        return text
    # 1) 分隔符隔开的整词重复
    parts = [p for p in re.split(r"[\s、,，]+", s) if p]
    if len(parts) >= 3:
        unit = parts[0]
        if len(unit) >= 2 and all(p == unit for p in parts):
            return unit
    # 2) 紧凑重复
    n = len(s)
    for period in range(2, n // 3 + 1):
        if n % period:
            continue
        unit = s[:period]
        if s == unit * (n // period):
            return unit
    return text


def _filter_low_confidence(segments, options):
    """按置信度丢弃不可靠片段（avg_logprob / no_speech_prob）。"""
    min_logprob = options.get("min_avg_logprob")
    max_nospeech = options.get("max_no_speech_prob")
    if not min_logprob and not max_nospeech:
        return segments
    kept = []
    dropped = 0
    for seg in segments:
        logprob = getattr(seg, "avg_logprob", None)
        nospeech = getattr(seg, "no_speech_prob", None)
        bad = False
        if min_logprob and isinstance(logprob, (int, float)) and logprob < float(min_logprob):
            bad = True
        if max_nospeech and isinstance(nospeech, (int, float)) and nospeech > float(max_nospeech):
            bad = True
        if bad:
            dropped += 1
            continue
        kept.append(seg)
    if dropped:
        print(f"置信度过滤：丢弃 {dropped} 段（avg_logprob < {min_logprob}，"
              f"no_speech_prob > {max_nospeech}）", file=sys.stderr)
    return kept


def _clean_hallucinations(segments, options):
    """压复读 + 丢致谢片尾语（作用在合并后的最终字幕段上）。"""
    collapse = options.get("collapse_repeats", True)
    drop_thanks = options.get("drop_thanks", True)
    result = []
    repeats = 0
    thanks = 0
    for item in segments:
        text = item["text"]
        if collapse:
            squeezed = _collapse_repeat_text(text)
            if squeezed != text:
                repeats += 1
                text = squeezed
        if drop_thanks and _is_thanks_text(text):
            thanks += 1
            continue
        if result and _normalize_text(result[-1]["text"]) == _normalize_text(text):
            # 与上一条内容相同（复读留下的残影）→ 并进上一条
            result[-1]["end"] = max(result[-1]["end"], item["end"])
            continue
        item = dict(item)
        item["text"] = text
        result.append(item)
    if repeats or thanks:
        print(f"反幻觉清洗：压掉 {repeats} 处复读，丢弃 {thanks} 条致谢片尾语", file=sys.stderr)
    return result


def format_timestamp(seconds: float) -> str:
    """秒数转 SRT 时间戳格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# 句末标点（强边界：一句话结束）
SENTENCE_END = ".?!。？！…"
# 次级标点（长句兜底时的软切点）
SOFT_BREAK = ",;:，；：、"


class _PseudoWord:
    """没有词级时间戳的 segment 退化成单个伪 word"""
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.word = text


def _flush(words):
    """把一串 word 收成一个字幕段；空文本返回 None"""
    text = "".join(w.word for w in words).strip()
    if not text:
        return None
    return {"start": words[0].start, "end": words[-1].end, "text": text}


def _find_soft_cut(words):
    """从后往前找最靠后的次级标点位置（返回该词索引），找不到返回 None"""
    for j in range(len(words) - 1, -1, -1):
        t = words[j].word.strip()
        if t and t[-1] in SOFT_BREAK:
            return j
    return None


def _find_pause_cut(words, min_gap=0.2):
    """长句无标点时的兜底：在句子后 2/3 段内找最大词间停顿处切（返回停顿前的词索引）。
    避免切出过短的头，也保证切点落在换气停顿而非词中间。找不到返回 None。"""
    best_gap, best_j = min_gap, None
    start = max(1, len(words) // 3)
    for j in range(start, len(words) - 1):
        gap = words[j + 1].start - words[j].end
        if gap > best_gap:
            best_gap, best_j = gap, j
    return best_j


def _postprocess(result):
    """清洗：丢弃无效/重复段、合并碎片、消除时间重叠"""
    cleaned = []
    for item in result:
        if item["end"] <= item["start"]:
            continue  # 丢弃无效时间戳（end <= start）
        if cleaned and item["text"] == cleaned[-1]["text"]:
            # 与前一段文本完全重复 → 延长前段，丢弃重复（Whisper 复读 artifact）
            cleaned[-1]["end"] = max(cleaned[-1]["end"], item["end"])
            continue
        cleaned.append(item)

    # 合并过短碎片（< 400ms 且少于 3 词）到前一段
    merged = []
    for item in cleaned:
        duration = item["end"] - item["start"]
        word_count = len(item["text"].split())
        if merged and duration < 0.4 and word_count < 3:
            merged[-1]["end"] = item["end"]
            merged[-1]["text"] += " " + item["text"]
        else:
            merged.append(item)

    # 消除相邻时间重叠（下一段 start 不早于上一段 end）
    for k in range(1, len(merged)):
        if merged[k]["start"] < merged[k - 1]["end"]:
            merged[k]["start"] = merged[k - 1]["end"]
        if merged[k]["end"] <= merged[k]["start"]:
            merged[k]["end"] = merged[k]["start"] + 0.3

    return merged


def merge_words_to_segments(segments, max_line_ms=4000, pause_ms=500, max_chars=80):
    """
    按「句子 + 停顿」切分字幕，让每条字幕尽量对应一个完整句子，
    起止时间 = 该句首词开口到末词收尾（精确对齐语音，说完正好换条）。

    切分信号（优先级从高到低）：
    1. 句末标点（. ? ! 。？！）→ 一句结束，立即切
    2. 词间停顿 >= pause_ms → 说话人换气/停顿，切
    3. 兜底：累计时长 >= max_line_ms 或字符数 >= max_chars
       → 在最近的次级标点处软切，没有则强制切（避免一条过长一行放不下）

    旧逻辑（照搬 Whisper segment）会把好几句挤一条、半句甩到下一条、
    超长时按时间在词中间硬剁；新逻辑以人话的句子为单位。
    """
    # 1) 把所有 segment 的 word 拉平成一个连续列表
    flat = []
    for seg in segments:
        words = seg.words if seg.words else []
        if not words:
            flat.append(_PseudoWord(seg.start, seg.end, seg.text))
        else:
            flat.extend(words)

    if not flat:
        return []

    # 2) 遍历 word，按句子边界 / 停顿 / 长度兜底切分
    result = []
    cur = []
    n = len(flat)
    for i, w in enumerate(flat):
        cur.append(w)
        wtext = w.word.strip()
        cur_text = "".join(x.word for x in cur).strip()
        cur_dur_ms = (w.end - cur[0].start) * 1000

        gap_ms = ((flat[i + 1].start - w.end) * 1000) if i + 1 < n else 0

        end_sentence = bool(wtext) and wtext[-1] in SENTENCE_END
        big_pause = gap_ms >= pause_ms
        too_long = cur_dur_ms >= max_line_ms or len(cur_text) >= max_chars

        if end_sentence or big_pause:
            seg_obj = _flush(cur)
            if seg_obj:
                result.append(seg_obj)
            cur = []
        elif too_long:
            cut = _find_soft_cut(cur)
            if cut is None or cut >= len(cur) - 1:
                cut = _find_pause_cut(cur)  # 无次级标点 → 退而求其次，在内部最大停顿处切
            if cut is not None and cut < len(cur) - 1:
                head, cur = cur[:cut + 1], cur[cut + 1:]
                seg_obj = _flush(head)
                if seg_obj:
                    result.append(seg_obj)
            else:
                seg_obj = _flush(cur)
                if seg_obj:
                    result.append(seg_obj)
                cur = []

    if cur:
        seg_obj = _flush(cur)
        if seg_obj:
            result.append(seg_obj)

    # 3) 清洗后处理
    return _postprocess(result)


def _transcribe_mlx(audio_path: str, language: str = None, options=None):
    """MLX Whisper 转写（Metal GPU 加速，默认引擎）"""
    try:
        import mlx_whisper
    except ImportError:
        print("错误：mlx-whisper 未安装，请运行：pip3 install --break-system-packages mlx-whisper",
              file=sys.stderr)
        return None

    options = options or build_options()
    print("转写引擎：MLX Whisper (Metal GPU)", file=sys.stderr)
    print(f"转写中：{audio_path}", file=sys.stderr)

    kwargs = {
        "path_or_hf_repo": os.environ.get("SUBTITLE_WHISPER_MODEL")
                          or "mlx-community/whisper-large-v3-turbo",
        "word_timestamps": True,
        # 不接上文：上一段的幻觉不会传染给下一段
        "condition_on_previous_text": bool(options.get("condition_on_previous_text")),
        "temperature": _temperature_ladder(options.get("temperature")),
    }
    if language:
        kwargs["language"] = language
    silence_threshold = options.get("hallucination_silence_threshold")
    if silence_threshold:
        kwargs["hallucination_silence_threshold"] = float(silence_threshold)

    result = mlx_whisper.transcribe(audio_path, **kwargs)

    lang = result.get("language", "unknown")
    print(f"检测语言：{lang}", file=sys.stderr)

    # 转换为与 faster-whisper 兼容的 segment 对象
    class Seg:
        def __init__(self, d):
            self.start = d["start"]
            self.end = d["end"]
            self.text = d.get("text", "")
            self.words = [Word(w) for w in d.get("words", [])]
            # 置信度：mlx 的 segment 带 avg_logprob / no_speech_prob
            self.avg_logprob = d.get("avg_logprob")
            self.no_speech_prob = d.get("no_speech_prob")

    class Word:
        def __init__(self, d):
            self.start = d["start"]
            self.end = d["end"]
            self.word = d.get("word", "")

    return [Seg(s) for s in result.get("segments", [])]


def _transcribe_faster(audio_path: str, model_name: str, language: str = None,
                       options=None):
    """faster-whisper 转写（CPU，兜底引擎）"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("错误：faster-whisper 未安装，请运行：pip3 install --break-system-packages faster-whisper",
              file=sys.stderr)
        return None

    options = options or build_options()
    print(f"转写引擎：faster-whisper (CPU), 模型：{model_name}", file=sys.stderr)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    print(f"转写中：{audio_path}", file=sys.stderr)
    transcribe_kwargs = {
        "word_timestamps": True,
        "condition_on_previous_text": bool(options.get("condition_on_previous_text")),
        "temperature": _temperature_ladder(options.get("temperature")),
    }
    if language:
        transcribe_kwargs["language"] = language
    silence_threshold = options.get("hallucination_silence_threshold")
    if silence_threshold:
        transcribe_kwargs["hallucination_silence_threshold"] = float(silence_threshold)

    segments_iter, info = model.transcribe(audio_path, **transcribe_kwargs)
    print(f"检测语言：{info.language}（置信度 {info.language_probability:.0%}）", file=sys.stderr)

    return list(segments_iter)


def transcribe_to_srt(audio_path: str, output_path: str, engine: str = "mlx",
                       model_name: str = "large-v3-turbo",
                       language: str = None, max_line_ms: int = 4000,
                       pause_ms: int = 500, options=None):
    """主函数：转写音频并输出 SRT。默认 MLX Whisper，失败自动降级 faster-whisper。"""

    options = options or build_options()
    segments = None

    if engine == "mlx":
        segments = _transcribe_mlx(audio_path, language, options)
        if segments is None:
            print("MLX Whisper 不可用，降级到 faster-whisper...", file=sys.stderr)
            segments = _transcribe_faster(audio_path, model_name, language, options)
    else:
        segments = _transcribe_faster(audio_path, model_name, language, options)
        if segments is None:
            print("faster-whisper 不可用，降级到 MLX Whisper...", file=sys.stderr)
            segments = _transcribe_mlx(audio_path, language, options)

    if segments is None:
        print("错误：没有可用的转写引擎", file=sys.stderr)
        sys.exit(1)

    # 置信度过滤（在切句之前丢，丢掉的是整段幻觉）
    segments = _filter_low_confidence(segments, options)

    # 合并为合理的字幕段
    srt_segments = merge_words_to_segments(segments, max_line_ms=max_line_ms, pause_ms=pause_ms)

    # 反幻觉清洗（压复读、丢致谢片尾语）
    srt_segments = _clean_hallucinations(srt_segments, options)

    # 写入 SRT 文件
    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(srt_segments, 1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n")
            f.write(f"{seg['text']}\n\n")

    print(f"完成！共 {len(srt_segments)} 条字幕 → {output_path}", file=sys.stderr)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="生成精确时间戳 SRT（默认 MLX Whisper）")
    parser.add_argument("audio", help="音频文件路径")
    parser.add_argument("--output", "-o", help="输出 SRT 路径（默认与音频同名）")
    parser.add_argument("--engine", "-e", default="mlx", choices=["mlx", "faster"],
                       help="转写引擎（默认 mlx，可选 faster）")
    parser.add_argument("--model", "-m", default="large-v3-turbo",
                       help="faster-whisper 模型（默认 large-v3-turbo）")
    parser.add_argument("--language", "-l", default=None,
                       help="语言代码（默认自动检测）")
    parser.add_argument("--max-line-ms", type=int, default=4000,
                       help="单条字幕最大时长毫秒数（默认 4000，v2.0 从 6000 降为 4000 提升时间戳精度）")
    parser.add_argument("--pause-ms", type=int, default=500,
                       help="词间停顿超过此毫秒数则断句（默认 500）")
    parser.add_argument("--condition-on-previous-text", action="store_true",
                       help="让转写接上文（Whisper 默认会接，但幻觉会传染，默认关闭）")
    parser.add_argument("--temperature", type=float, default=0.0,
                       help="温度回退上限：0=只用最可能结果；>0 时低置信片段自动升温重试")
    parser.add_argument("--hallucination-silence-threshold", type=float, default=None,
                       help="静音超过这个秒数就认为解码不可靠（如 2.0）")
    parser.add_argument("--min-avg-logprob", type=float, default=None,
                       help="低于该平均对数概率的片段直接丢弃（如 -1.0）")
    parser.add_argument("--max-no-speech-prob", type=float, default=None,
                       help="高于该无语音概率的片段直接丢弃（如 0.8）")
    parser.add_argument("--keep-thanks", action="store_true",
                       help="保留「ご視聴ありがとうございました」这类致谢片尾语")
    parser.add_argument("--keep-repeats", action="store_true",
                       help="保留复读内容（默认压掉）")

    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"错误：文件不存在 {audio_path}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or str(audio_path.with_suffix(".srt"))

    options = build_options(
        condition_on_previous_text=bool(args.condition_on_previous_text),
        temperature=float(args.temperature or 0.0),
        hallucination_silence_threshold=args.hallucination_silence_threshold,
        min_avg_logprob=args.min_avg_logprob,
        max_no_speech_prob=args.max_no_speech_prob,
        drop_thanks=not args.keep_thanks,
        collapse_repeats=not args.keep_repeats,
    )

    transcribe_to_srt(
        str(audio_path),
        output_path,
        engine=args.engine,
        model_name=args.model,
        language=args.language,
        max_line_ms=args.max_line_ms,
        pause_ms=args.pause_ms,
        options=options,
    )


if __name__ == "__main__":
    main()

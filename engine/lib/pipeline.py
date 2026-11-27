# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 单片流水线。

阶段：下载 → 抽音频 → 分段 → 转写 → 合并 → 预过滤 → 翻译 → 重试
      → 质量门 → 静音修剪 → 双语 ASS → 压制 → 上传。

设计要点：
- 断点续跑：work/<stem>/ 下已存在的阶段产物直接复用（source.srt / source.zh.srt / out mp4）；
- 原子性：所有产物先写 .partial 再改名；上传同样先写 .partial 再 mv；
- 失败不污染输出目录：只有完整校验过的文件才会上传；
- 日志/进度只走 stderr 与 logs/，stdout 留给调用方（--json 时只输出 JSON）。
"""

import json
import os
import re
import shutil
import sys
import time

from lib import config as config_mod
from lib.subs import wrap_text
from lib import keys, prompts
from lib import select as selection
from lib import style_templates
from lib.remote import Endpoint
from lib.runtime import (EngineError, Log, age_days, atomic_write_json, atomic_write_text,
                     ensure_dir, expand, file_size, human_duration, human_size,
                     iso_from_epoch, now_iso, read_json, run_cmd, sanitize_stem,
                     shquote, tail_text, today_str)

VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
PYTHON = sys.executable or "python3"

PLACEHOLDER_RE = re.compile(r"^\[(听不清|广告宣番|♪ 歌词 ♪)\]$")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

MAX_SRT_BLOCK_SECONDS = 30.0


class NoSpeech(Exception):
    """整片没有可用语音（转写结果为空）。"""


# ==================================================================== 工具函数

# Homebrew 的 `ffmpeg` 公式不带 libass（依赖里没有 libass，`--with-libass` 也已移除），
# 带 ass/subtitles 的是 `ffmpeg-full`，而它是 keg-only：只落在 <brew --prefix>/opt/
# ffmpeg-full/bin，不进 PATH。所以「which ffmpeg 命中不带 ass 的那个」是常态。
FFMPEG_KEG_DIRS = (
    "/opt/homebrew/opt/ffmpeg-full/bin",   # Apple Silicon 默认前缀
    "/usr/local/opt/ffmpeg-full/bin",      # Intel 默认前缀
)

_FFMPEG_FILTER_CACHE = {}
_BREW_KEG_LOOKUP_DONE = [False]
_BREW_KEG_PATH = [None]
_FFMPEG_FALLBACK_WARNED = [False]


def _run_cmd_or_default(cmd, timeout):
    """探测失败（含被测试替身接管）一律按「不确定」处理，不影响主流程。"""
    try:
        rc, out, _err = run_cmd(cmd, timeout=timeout)
    except Exception:
        return None
    return (rc, out or "")


def _brew_keg_ffmpeg():
    """`brew --prefix ffmpeg-full` —— 兼容自定义 Homebrew 前缀。只查一次。"""
    if _BREW_KEG_LOOKUP_DONE[0]:
        return _BREW_KEG_PATH[0]
    _BREW_KEG_LOOKUP_DONE[0] = True
    probe = _run_cmd_or_default(["brew", "--prefix", "ffmpeg-full"], 15)
    if probe is None or probe[0] != 0:
        return None
    prefix = (probe[1] or "").strip()
    if not prefix or not os.path.isdir(prefix):
        return None
    candidate = os.path.join(prefix, "bin", "ffmpeg")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        _BREW_KEG_PATH[0] = candidate
    return _BREW_KEG_PATH[0]


def ffmpeg_has_subtitle_filters(binary):
    """这个 ffmpeg 能不能烧字幕（ass + subtitles）。按路径缓存，只跑一次 -filters。"""
    cached = _FFMPEG_FILTER_CACHE.get(binary)
    if cached is not None:
        return cached
    probe = _run_cmd_or_default([binary, "-hide_banner", "-filters"], 60)
    verdict = bool(probe) and probe[0] == 0 and \
        re.search(r"\bass\b", probe[1]) is not None and \
        re.search(r"\bsubtitles\b", probe[1]) is not None
    _FFMPEG_FILTER_CACHE[binary] = verdict
    return verdict


def ffmpeg_candidates(cfg):
    """按优先级给出可用的 ffmpeg 绝对路径（已过滤为存在且可执行）。"""
    configured = expand(cfg.get("burn.ffmpeg") or "")
    if configured:
        return [configured]
    ordered = []
    seen = set()

    def add(path):
        if not path or path in seen:
            return
        if os.path.isfile(path) and os.access(path, os.X_OK):
            seen.add(path)
            ordered.append(path)

    add(_brew_keg_ffmpeg())
    add(shutil.which("ffmpeg"))
    for directory in FFMPEG_KEG_DIRS:
        add(os.path.join(directory, "ffmpeg"))
    return ordered


def ffmpeg_bin(cfg):
    configured = expand(cfg.get("burn.ffmpeg") or "")
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise EngineError(3, "config 指定的 ffmpeg 不存在或不可执行：%s" % configured,
                          detail="请检查 burn.ffmpeg（brew install ffmpeg-full）")
    candidates = ffmpeg_candidates(cfg)
    if not candidates:
        raise EngineError(3, "找不到 ffmpeg（PATH 与 config.burn.ffmpeg 都没有）",
                          detail="安装 ffmpeg-full 或在 config.json 写绝对路径")
    for binary in candidates:
        if ffmpeg_has_subtitle_filters(binary):
            return binary
    if len(candidates) > 1 and not _FFMPEG_FALLBACK_WARNED[0]:
        _FFMPEG_FALLBACK_WARNED[0] = True
        sys.stderr.write("[warn] 本机没有带 ass/subtitles 滤镜的 ffmpeg（压制会失败）："
                         "brew install ffmpeg-full，或把 burn.ffmpeg 指向带该滤镜的二进制\n")
    return candidates[0]


def ffprobe_bin(cfg):
    ff = ffmpeg_bin(cfg)
    candidate = os.path.join(os.path.dirname(ff), "ffprobe")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("ffprobe")
    if not found:
        raise EngineError(3, "找不到 ffprobe（应与 ffmpeg 同目录或在 PATH）")
    return found


def python_bin():
    return PYTHON


def probe_media(cfg, path, timeout=180):
    rc, out, err = run_cmd([ffprobe_bin(cfg), "-v", "error", "-print_format", "json",
                            "-show_format", "-show_streams", path], timeout=timeout)
    if rc != 0:
        raise EngineError(6, "ffprobe 无法读取媒体文件：%s" % os.path.basename(path),
                          detail=(err or out).strip()[-500:])
    try:
        raw = json.loads(out)
    except ValueError:
        raise EngineError(6, "ffprobe 输出不是合法 JSON")
    return raw


def probe_summary(cfg, path):
    raw = probe_media(cfg, path)
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = video.get("avg_frame_rate") if video else None
    try:
        num, den = (fps or "0/1").split("/")
        fps_val = round(float(num) / float(den), 3) if float(den) else None
    except (ValueError, ZeroDivisionError):
        fps_val = None
    return {
        "path": os.path.abspath(path),
        "sizeBytes": int(fmt.get("size") or file_size(path) or 0),
        "durationSec": round(float(fmt.get("duration") or 0), 2),
        "bitRate": int(fmt.get("bit_rate") or 0),
        "formatName": fmt.get("format_name"),
        "video": None if not video else {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": fps_val,
        },
        "audio": None if not audio else {
            "codec": audio.get("codec_name"),
            "sampleRate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
        },
    }


def probe_duration(cfg, path):
    try:
        info = probe_media(cfg, path, timeout=120)
        return float((info.get("format") or {}).get("duration") or 0)
    except EngineError:
        return 0.0


def probe_height(cfg, path):
    try:
        info = probe_media(cfg, path, timeout=120)
        video = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"), None)
        return int(video.get("height")) if video and video.get("height") else 0
    except EngineError:
        return 0


def _to_float(value):
    try:
        if value is None or value == "" or value == "N/A":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_stream_timing(cfg, path):
    """探测视频轨与音频轨的 start_time / duration（秒）。

    返回 {"audioStart", "audioDuration", "videoStart"}；没有音轨或探测失败时
    返回 None（调用方按「不需要对齐」处理）。
    """
    try:
        info = probe_media(cfg, path, timeout=120)
    except EngineError:
        return None
    streams = info.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return None
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_duration = _to_float(audio.get("duration"))
    if audio_duration is None:
        # 有些容器把时长放在 format 里
        audio_duration = _to_float((info.get("format") or {}).get("duration"))
    return {
        "audioStart": _to_float(audio.get("start_time")),
        "audioDuration": audio_duration,
        "videoStart": _to_float((video or {}).get("start_time")),
    }


def parse_srt_text(text):
    """宽容解析 SRT，返回 [{idx, start, end, text}]。"""
    blocks = []
    for raw in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [line for line in raw.split("\n") if line.strip()]
        if len(lines) < 2:
            continue
        ts_line = None
        for line in lines:
            if "-->" in line:
                ts_line = line
                break
        if not ts_line:
            continue
        match = re.search(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})", ts_line)
        if not match:
            continue
        idx = len(blocks) + 1
        try:
            idx = int(lines[0].strip())
        except ValueError:
            pass
        text_start = lines.index(ts_line) + 1
        body = "\n".join(lines[text_start:]).strip()
        blocks.append({"idx": idx, "start": match.group(1), "end": match.group(2), "text": body})
    return blocks


def parse_srt_file(path):
    try:
        return parse_srt_text(open(path, "r", encoding="utf-8", errors="replace").read())
    except OSError:
        return []


def srt_time_to_seconds(value):
    match = re.match(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})", value.strip())
    if not match:
        return 0.0
    h, m, s, ms = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def format_srt_time(seconds):
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt_blocks(blocks, path):
    lines = []
    for index, block in enumerate(blocks, 1):
        lines.append(str(index))
        lines.append("%s --> %s" % (block["start"], block["end"]))
        lines.append(block.get("text") or "")
        lines.append("")
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def partial_path(path):
    """<name>.partial<ext>：保留扩展名，避免 ffmpeg 无法判断输出格式。"""
    stem, ext = os.path.splitext(path)
    return stem + ".partial" + ext


def escape_filter_path(path):
    """ffmpeg filtergraph 值转义（单引号包裹）。"""
    normalized = os.path.abspath(path)
    normalized = normalized.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + normalized + "'"


def translation_stats(zh_path):
    blocks = parse_srt_file(zh_path)
    untranslated = 0
    for block in blocks:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        if KANA_RE.search(text) and not PLACEHOLDER_RE.match(text):
            untranslated += 1
    total = len(blocks) or 1
    stats = {"blocks": len(blocks), "untranslated": untranslated,
             "untranslatedPct": round(untranslated * 100.0 / total, 2)}
    # translate_srt 会在产物旁边留一份自救账本；有就并进来，没有就保持旧形状。
    try:
        with open(zh_path + ".report.json", "r", encoding="utf-8") as handle:
            detail = json.load(handle)
    except Exception:  # noqa: BLE001 - 账本是加分项，缺了不影响翻译本身
        detail = None
    if isinstance(detail, dict):
        stats["calls"] = detail.get("calls") or {}
        stats["truncatedBatches"] = detail.get("truncatedBatches") or 0
        stats["failedBatches"] = detail.get("failedBatches") or 0
        stats["tokens"] = detail.get("tokens") or {}
        if detail.get("reasoningEffort"):
            stats["reasoningEffort"] = detail["reasoningEffort"]
        ids = detail.get("untranslatedIds") or []
        stats["untranslatedIds"] = ["#%s@%s(%s)" % (
            entry.get("idx"), entry.get("start"), entry.get("reason"))
            for entry in ids if isinstance(entry, dict)][:20]
    return stats


def engine_env(cfg):
    """根据引擎类型构造子进程环境（密钥只放 env，不落盘）。"""
    env = os.environ.copy()
    kind = cfg.get("engine.kind")
    key, source = keys.resolve_api_key(kind, cfg.get("engine.apiKeyRef"))
    if key:
        env["SUBTITLE_API_KEY"] = key
        if kind == "minimax":
            env["MINIMAX_API_KEY"] = key
        elif kind == "deepseek":
            env["DEEPSEEK_API_KEY"] = key
        else:
            env["OPENAI_API_KEY"] = key
    base_url = (cfg.get("engine.baseUrl") or "").strip()
    if base_url:
        env["SUBTITLE_BASE_URL"] = base_url
    env["SUBTITLE_ENGINE_KEY_SOURCE"] = source
    return env


def engine_kind_for_vendor(cfg):
    kind = cfg.get("engine.kind")
    if kind == "openai-compatible":
        return "deepseek"  # vendor 的 deepseek 分支即 OpenAI 兼容 HTTP
    return kind


def model_for_engine(cfg):
    """模型名以面板选择的 engine.model 为准。

    `engine.ollamaModel` 是早期的高级覆盖项：它曾优先于 model，于是面板里
    选了别的模型也不会生效。现在只在 model 为空时兜底，覆盖不再是静默的。
    """
    kind = cfg.get("engine.kind")
    model = (cfg.get("engine.model") or "").strip()
    legacy = (cfg.get("engine.ollamaModel") or "").strip()
    if kind == "ollama" and model == "" and legacy:
        return legacy
    return model


def provider_for_engine(cfg):
    """dsh 引擎用的提供方（其它引擎为空）。"""
    return (cfg.get("engine.provider") or "").strip()


def output_mode(cfg):
    """产出方式：burn = 压制进视频，srt = 只生成字幕文件。

    output.mode 是唯一的用户开关；老配置没有这个键时回落到 burn.enabled，
    所以升级不会改变既有行为。
    """
    mode = (cfg.get("output.mode") or "").strip().lower()
    if mode in ("burn", "srt"):
        return mode
    return "burn" if cfg.get("burn.enabled") else "srt"


def count_batches(blocks, batch_size):
    if not blocks:
        return 1
    return max(1, (blocks + batch_size - 1) // batch_size)


# ==================================================================== 各阶段

def extract_audio(cfg, log, video_path, wav_path):
    if file_size(wav_path):
        log.info("复用已抽取音频：%s" % os.path.basename(wav_path))
        return wav_path
    ensure_dir(os.path.dirname(wav_path))
    partial = partial_path(wav_path)
    ffmpeg = ffmpeg_bin(cfg)
    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
                            "-c:a", "pcm_s16le", partial], timeout=3600)
    if rc != 0 or not file_size(partial):
        if os.path.exists(partial):
            os.unlink(partial)
        raise EngineError(6, "抽取音频失败（源文件可能没有音轨）",
                          detail=(err or out).strip()[-800:])
    os.replace(partial, wav_path)
    return wav_path


# 时间轴对齐的最小阈值：小于 0.2 秒不值得补静音（多半是探测误差）
MIN_TIMELINE_SHIFT = 0.2


def align_audio_timeline(cfg, log, video_path, wav_path):
    """让音频时间轴与画面时间轴对齐（REQ-01）。

    容器里音轨常常比画面晚几秒开始（音轨 start_time > 0）。ffmpeg 抽音频时
    会把这段容器级的空隙丢掉，于是转写出来的时间轴整体偏早，字幕就比人声
    早了那几秒 —— 看起来就是「字幕对不上嘴」。

    这里实测四个数再决定要不要补静音，而不是假设：
        A  = 音轨 start_time        V0 = 视频轨 start_time
        D_a = 音轨时长               W  = 抽出来的 wav 时长
    ffmpeg 把音轨内容放在 wav 的 [P, W]，P = max(0, W - D_a)；
    音轨内容在容器里从 A 开始，所以要补的静音 = (A - V0) - P。

    补静音而不是平移字幕：后面静音修剪、双语对齐都按音频时间轴算，
    平移字幕会让这些环节错位。返回实际补上的秒数（0 表示没补）。
    """
    timing = probe_stream_timing(cfg, video_path)
    if not timing:
        return 0.0
    audio_start = timing.get("audioStart")
    if audio_start is None:
        return 0.0
    wav_duration = probe_duration(cfg, wav_path)
    if not wav_duration:
        return 0.0
    video_start = timing.get("videoStart")
    if video_start is None:
        video_start = 0.0
    audio_duration = timing.get("audioDuration") or 0.0
    preserved = max(0.0, wav_duration - audio_duration)
    shift = (audio_start - video_start) - preserved
    if shift < MIN_TIMELINE_SHIFT:
        if shift <= -MIN_TIMELINE_SHIFT:
            log.warn("音频比画面早 %.2fs 开始，未做对齐（字幕可能偏晚）" % (-shift))
        return 0.0

    ffmpeg = ffmpeg_bin(cfg)
    padded = wav_path + ".padded"
    ms = int(round(shift * 1000))
    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", wav_path, "-af", "adelay=%d:all=1" % ms,
                            "-c:a", "pcm_s16le", "-f", "wav", padded], timeout=3600)
    if rc != 0 or not file_size(padded):
        if os.path.exists(padded):
            os.unlink(padded)
        log.warn("补静音失败（rc=%s），字幕时间轴可能偏早：%s"
                 % (rc, (err or out).strip()[-200:]))
        return 0.0
    os.replace(padded, wav_path)
    log.info("音轨比画面晚 %.2fs 开始，已在音频开头补静音对齐字幕" % shift)
    return shift


def build_chunks(cfg, log, wav_path, chunks_dir):
    chunk_seconds = int(cfg.get("transcribe.chunkSeconds") or 60)
    existing = sorted(f for f in os.listdir(chunks_dir)) if os.path.isdir(chunks_dir) else []
    existing = [f for f in existing if re.match(r"^chunk_\d+\.wav$", f)]
    if existing:
        log.info("复用已分段音频：%d 个 chunk" % len(existing))
        return [os.path.join(chunks_dir, f) for f in existing]
    ensure_dir(chunks_dir)
    ffmpeg = ffmpeg_bin(cfg)
    pattern = os.path.join(chunks_dir, "chunk_%03d.wav")
    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-i", wav_path, "-f", "segment",
                            "-segment_time", str(chunk_seconds),
                            "-reset_timestamps", "1",
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                            pattern], timeout=3600)
    if rc != 0:
        raise EngineError(6, "音频分段失败", detail=(err or out).strip()[-800:])
    chunks = sorted(os.path.join(chunks_dir, f) for f in os.listdir(chunks_dir)
                    if re.match(r"^chunk_\d+\.wav$", f))
    if not chunks:
        raise EngineError(6, "音频分段结果为空")
    return chunks


def _chunk_max_volume(cfg, chunk_path):
    """返回 volumedetect 的 max_volume(dB)；解析失败返回 None。"""
    ffmpeg = ffmpeg_bin(cfg)
    rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-nostats",
                            "-i", chunk_path, "-af", "volumedetect", "-f", "null", "-"],
                           timeout=300)
    text = (err or "") + (out or "")
    values = re.findall(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    if not values:
        return None
    return float(values[-1])


def transcribe_chunks(cfg, log, chunks, engine_env_vars=None):
    """批量转写；静音 chunk 跳过，返回 [{chunk, srt, silent}]。"""
    threshold = float(cfg.get("transcribe.silenceDb") or -50)
    language = (cfg.get("transcribe.language") or "").strip()
    results = []
    pending = []
    for chunk in chunks:
        srt_path = os.path.splitext(chunk)[0] + ".srt"
        marker = os.path.splitext(chunk)[0] + ".silent"
        if os.path.exists(marker):
            results.append({"chunk": chunk, "srt": srt_path, "silent": True})
            continue
        if file_size(srt_path) is not None:
            results.append({"chunk": chunk, "srt": srt_path, "silent": False})
            continue
        max_db = _chunk_max_volume(cfg, chunk)
        if max_db is not None and max_db < threshold:
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("max_volume=%s dB < %s dB\n" % (max_db, threshold))
            results.append({"chunk": chunk, "srt": srt_path, "silent": True})
            continue
        pending.append(chunk)
        results.append({"chunk": chunk, "srt": srt_path, "silent": None})

    if pending:
        log.info("转写 %d 个 chunk（共 %d，静音/已转写跳过）" % (len(pending), len(chunks)))
        env = (engine_env_vars or os.environ.copy()).copy()
        model = (cfg.get("transcribe.whisperModel") or "").strip()
        if model:
            env["SUBTITLE_WHISPER_MODEL"] = model
        env["SUBTITLE_WHISPER_ENGINE"] = cfg.get("transcribe.engine") or "mlx"
        cmd = [python_bin(), os.path.join(VENDOR_DIR, "transcribe_batch.py")]
        cmd += pending
        cmd += ["--output-dir", os.path.dirname(pending[0]),
                "--max-line-ms", str(cfg.get("transcribe.maxLineMs") or 4000),
                "--pause-ms", str(cfg.get("transcribe.pauseMs") or 500)]
        if language:
            cmd += ["--language", language]
        # 反幻觉参数（REQ-02 / REQ-03）：0 表示关闭对应过滤
        if bool(cfg.get("transcribe.conditionOnPreviousText")):
            cmd += ["--condition-on-previous-text"]
        try:
            temperature = float(cfg.get("transcribe.temperature") or 0)
        except (TypeError, ValueError):
            temperature = 0.0
        if temperature > 0:
            cmd += ["--temperature", str(temperature)]
        try:
            silence_threshold = float(cfg.get("transcribe.hallucinationSilenceThreshold") or 0)
        except (TypeError, ValueError):
            silence_threshold = 0.0
        if silence_threshold > 0:
            cmd += ["--hallucination-silence-threshold", str(silence_threshold)]
        try:
            min_logprob = float(cfg.get("transcribe.minAvgLogprob") or 0)
        except (TypeError, ValueError):
            min_logprob = 0.0
        if min_logprob < 0:
            cmd += ["--min-avg-logprob", str(min_logprob)]
        try:
            max_nospeech = float(cfg.get("transcribe.maxNoSpeechProb") or 0)
        except (TypeError, ValueError):
            max_nospeech = 0.0
        if max_nospeech > 0:
            cmd += ["--max-no-speech-prob", str(max_nospeech)]
        if bool(cfg.get("transcribe.keepThanksPhrases")):
            cmd += ["--keep-thanks"]
        if bool(cfg.get("transcribe.keepRepeats")):
            cmd += ["--keep-repeats"]
        timeout = 90 * len(pending) + 180
        rc, out, err = run_cmd(cmd, timeout=timeout, env=env)
        if rc != 0:
            produced = sum(1 for item in results
                           if item["silent"] is None and file_size(item["srt"]))
            if not produced:
                raise EngineError(5, "Whisper 转写失败",
                                  detail=(err or out).strip()[-1200:])
            log.warn("部分 chunk 转写失败（rc=%s），继续用已产出的 %d 个" % (rc, produced))
        for item in results:
            if item["silent"] is None:
                item["silent"] = False
    return results


def merge_chunk_srts(cfg, log, results, source_srt, chunk_seconds=60):
    """按 ffprobe 实测时长累加偏移合并，避免 60s 整数假设造成的时间漂移。"""
    blocks = []
    offset = 0.0
    for item in results:
        duration = probe_duration(cfg, item["chunk"]) or float(chunk_seconds)
        if item["silent"] or not file_size(item["srt"]):
            offset += duration
            continue
        chunk_blocks = parse_srt_file(item["srt"])
        for block in chunk_blocks:
            start = srt_time_to_seconds(block["start"]) + offset
            end = srt_time_to_seconds(block["end"]) + offset
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if end - start > MAX_SRT_BLOCK_SECONDS:
                log.debug("丢弃超长块：%.1f-%.1fs" % (start, end))
                continue
            blocks.append({"start": format_srt_time(start), "end": format_srt_time(end),
                           "text": text})
        offset += duration
    if not blocks:
        raise NoSpeech("整片没有可用语音（所有 chunk 静音或转写为空）")
    write_srt_blocks(blocks, source_srt)
    log.info("合并字幕：%d 条 → %s" % (len(blocks), os.path.basename(source_srt)))
    return len(blocks)


def prefilter(cfg, log, source_srt):
    """预过滤（BGM/广告/短假名 → 占位符），产出 source.srt.prefilter。"""
    target = source_srt + ".prefilter"
    if file_size(target):
        return target
    cmd = [python_bin(), os.path.join(VENDOR_DIR, "prefilter_and_rerun.py"),
           source_srt, source_srt, "/dev/null", "--no-rerun"]
    rc, out, err = run_cmd(cmd, timeout=300, env=engine_env(cfg))
    if rc == 0 and file_size(target):
        return target
    # 预过滤失败不致命：直接拿原文去翻译
    log.warn("预过滤失败（rc=%s），改用原文：%s" % (rc, (err or "").strip()[-200:]))
    shutil.copyfile(source_srt, target)
    return target


def translate(cfg, log, source_srt, zh_srt, prompt_set):
    """调用 vendor/translate_srt.py；system/context/series 全部来自 config。"""
    if file_size(zh_srt) and os.stat(zh_srt).st_mtime >= os.stat(source_srt).st_mtime:
        log.info("复用已有译文：%s" % os.path.basename(zh_srt))
        return zh_srt

    work_dir = os.path.dirname(zh_srt)
    system_file = os.path.join(work_dir, "system_prompt.txt")
    if prompt_set.get("system"):
        atomic_write_text(system_file, prompt_set["system"])
    else:
        system_file = None

    build = cfg.get("engine.batchSize") or 30
    block_count = len(parse_srt_file(source_srt))
    batches = count_batches(block_count, int(build))
    timeout = max(600, int(cfg.get("engine.timeoutSeconds") or 300) * batches + 300)

    cmd = [python_bin(), os.path.join(VENDOR_DIR, "translate_srt.py"),
           source_srt, zh_srt,
           "--engine", engine_kind_for_vendor(cfg),
           "--model", model_for_engine(cfg),
           "--temperature", str(cfg.get("engine.temperature")),
           "--seed", str(cfg.get("engine.seed")),
           "--max-tokens", str(cfg.get("engine.maxTokens")),
           "--batch-size", str(build)]
    if cfg.get("engine.kind") == "dsh":
        cmd += ["--provider", provider_for_engine(cfg)]
    # 只有 translate_srt 吃这个参数；重跑层（REQ-55）还没接，别在这里瞎传。
    effort = (cfg.get("engine.reasoningEffort") or "").strip()
    if effort:
        cmd += ["--reasoning-effort", effort]
    if cfg.get("engine.kind") == "ollama":
        cmd += ["--ollama-host", cfg.get("engine.ollamaHost") or ""]
        if (cfg.get("engine.ollamaModel") or "").strip():
            cmd += ["--ollama-model", cfg.get("engine.ollamaModel")]
    if system_file:
        cmd += ["--system-file", system_file]
    if prompt_set.get("context"):
        cmd += ["--video-context", prompt_set["context"]]
    if prompt_set.get("series"):
        cmd += ["--series", prompt_set["series"]]

    log.info("翻译 %d 条（%d 批 × %d，引擎 %s/%s，超时 %ss）"
             % (block_count, batches, build, cfg.get("engine.kind"),
                model_for_engine(cfg), timeout))
    rc, out, err = run_cmd(cmd, timeout=timeout, env=engine_env(cfg))
    if rc != 0:
        raise EngineError(5, "翻译引擎调用失败（rc=%s）" % rc,
                          detail=(err or out).strip()[-1200:])
    if not file_size(zh_srt):
        raise EngineError(5, "翻译输出为空：%s" % os.path.basename(zh_srt),
                          detail=(err or "").strip()[-500:])
    stats = translation_stats(zh_srt)
    log.info("翻译完成：%d 条，仍为日文 %d 条（%.1f%%）"
             % (stats["blocks"], stats["untranslated"], stats["untranslatedPct"]))
    return zh_srt


def retry_translate(cfg, log, zh_srt):
    """对漏翻条目小批量重试；失败只告警。"""
    tmp = zh_srt + ".retry"
    cmd = [python_bin(), os.path.join(VENDOR_DIR, "retry_translate.py"), zh_srt, tmp,
           "--engine", engine_kind_for_vendor(cfg),
           "--model", model_for_engine(cfg)]
    if cfg.get("engine.kind") == "dsh":
        cmd += ["--provider", provider_for_engine(cfg)]
    rc, out, err = run_cmd(cmd, timeout=1800, env=engine_env(cfg))
    if rc == 0 and file_size(tmp):
        os.replace(tmp, zh_srt)
        log.info("重试翻译完成")
    else:
        if os.path.exists(tmp):
            os.unlink(tmp)
        log.warn("重试翻译失败（rc=%s），保留首轮译文：%s" % (rc, (err or out).strip()[-200:]))


def quality_gate(cfg, log, source_srt, zh_srt, series=""):
    """未译率超阈值 → 逐条重跑（vendor prefilter_and_rerun.py 的 rerun 模式）。"""
    stats = translation_stats(zh_srt)
    pct_threshold = float(cfg.get("translation.rerunThresholdPct") or 5)
    count_threshold = int(cfg.get("translation.rerunThresholdCount") or 50)
    if not stats["untranslated"]:
        return stats
    if stats["untranslatedPct"] <= pct_threshold and stats["untranslated"] <= count_threshold:
        return stats
    log.warn("未译率 %.1f%%（%d 条）超过阈值，开始逐条重跑"
             % (stats["untranslatedPct"], stats["untranslated"]))
    cmd = [python_bin(), os.path.join(VENDOR_DIR, "prefilter_and_rerun.py"),
           source_srt, zh_srt, zh_srt,
           "--engine", engine_kind_for_vendor(cfg),
           "--model", model_for_engine(cfg),
           "--batch-size", str(cfg.get("engine.batchSize") or 30)]
    if cfg.get("engine.kind") == "dsh":
        cmd += ["--provider", provider_for_engine(cfg)]
    if series:
        cmd += ["--series", series]
    rc, out, err = run_cmd(cmd, timeout=7200, env=engine_env(cfg))
    if rc != 0:
        log.warn("逐条重跑未完全成功（rc=%s）：%s" % (rc, (err or out).strip()[-300:]))
    final = translation_stats(zh_srt)
    log.info("重跑后未译率 %.1f%%（%d 条）"
             % (final["untranslatedPct"], final["untranslated"]))
    return final


def trim_silence(cfg, log, zh_srt, wav_path):
    """用音频静音检测去掉整段无人声的字幕。"""
    if not cfg.get("subtitles.trimSilence"):
        return zh_srt
    if not file_size(wav_path):
        return zh_srt
    target = zh_srt + ".trim"
    cmd = [python_bin(), os.path.join(VENDOR_DIR, "trim_silence.py"),
           zh_srt, wav_path, "--output", target,
           "--hall-thr-db", "-50", "--hall-pct", "0.95", "--hall-min-dur", "5.0"]
    rc, out, err = run_cmd(cmd, timeout=1800)
    if rc == 0 and file_size(target):
        os.replace(target, zh_srt)
        log.info("静音修剪完成")
    else:
        if os.path.exists(target):
            os.unlink(target)
        log.warn("静音修剪失败（rc=%s），保留原时间轴：%s" % (rc, (err or out).strip()[-200:]))
    return zh_srt


def build_bilingual_srt(cfg, log, source_srt, zh_srt):
    """中文在上、原文在下，按 start 时间戳对齐。"""
    source_blocks = {block["start"]: block for block in parse_srt_file(source_srt)}
    merged = []
    for block in parse_srt_file(zh_srt):
        original = source_blocks.get(block["start"])
        text = (block.get("text") or "").strip()
        if original and (original.get("text") or "").strip() \
                and not PLACEHOLDER_RE.match((original.get("text") or "").strip()):
            text = text + "\n" + original["text"].strip()
        merged.append({"start": block["start"], "end": block["end"], "text": text})
    target = os.path.join(os.path.dirname(zh_srt), "source.zh.bilingual.srt")
    write_srt_blocks(merged, target)
    log.info("生成双语 SRT：%d 条" % len(merged))
    return target


def build_ass(cfg, log, bilingual_srt, ass_path, height):
    cmd = [python_bin(), os.path.join(VENDOR_DIR, "bilingual_ass.py"), bilingual_srt,
           "--output", ass_path, "--height", str(height or 1080)]
    font = (cfg.get("subtitles.font") or "").strip()
    if font:
        cmd += ["--font", font]
    if int(cfg.get("subtitles.fontSize") or 0) > 0:
        cmd += ["--cn-size", str(int(cfg.get("subtitles.fontSize")))]
    # 样式模板（REQ-27）：白字黑边之外还能选黄字/顶部/底框等预设
    cmd += ["--style", (cfg.get("subtitles.styleTemplate") or style_templates.DEFAULT_STYLE_ID)]
    try:
        limit = int(cfg.get("subtitles.maxLineChars") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        cmd += ["--max-chars", str(limit)]
    rc, out, err = run_cmd(cmd, timeout=600)
    if rc != 0 or not file_size(ass_path):
        raise EngineError(6, "生成双语 ASS 失败", detail=(err or out).strip()[-500:])
    log.info("生成双语 ASS：%s" % os.path.basename(ass_path))
    return ass_path


def prepare_burn_srt(cfg, log, zh_srt):
    """按 subtitles.maxLineChars 折行，产出一份用于压制的 SRT。

    非双语走 subtitles= 滤镜，libass 的自动折行按字体度量切，经常把词从中间
    切开；用户在这个字段里写的字数因此必须显式生效，否则它就是个摆设。
    上限为 0（或留空）时不折，返回原文件。
    """
    try:
        limit = int(cfg.get("subtitles.maxLineChars") or 0)
    except (TypeError, ValueError):
        return zh_srt
    if limit <= 0:
        return zh_srt
    blocks = parse_srt_file(zh_srt)
    if not blocks:
        return zh_srt
    wrapped = []
    for block in blocks:
        text = block.get("text") or ""
        block = dict(block)
        block["text"] = "\n".join(wrap_text(line, limit) for line in text.split("\n"))
        wrapped.append(block)
    target = os.path.join(os.path.dirname(zh_srt), "source.zh.wrapped.srt")
    write_srt_blocks(wrapped, target)
    folded = sum(1 for a, b in zip(blocks, wrapped) if a["text"] != b["text"])
    if folded:
        log.info("按每行 %d 字折行：%d/%d 条被折" % (limit, folded, len(blocks)))
    return target


def burn_video(cfg, log, source_video, subtitle_path, target_path, ass_mode=False, height=0):
    """压片：ASS 用 ass= 滤镜（支持行内字号），SRT 用 subtitles= 滤镜。

    先写 .partial，校验通过后再改名；源文件不动。
    """
    ffmpeg = ffmpeg_bin(cfg)
    if output_mode(cfg) != "burn":
        raise EngineError(6, "输出方式不是压制进视频，不执行压制")
    video_height = height or probe_height(cfg, source_video)

    if ass_mode:
        filter_expr = "ass=%s" % escape_filter_path(subtitle_path)
    else:
        filter_expr = "subtitles=%s" % escape_filter_path(subtitle_path)
        font_size = int(cfg.get("burn.fontSize") or cfg.get("subtitles.fontSize") or 0)
        font = (cfg.get("subtitles.font") or "").strip()
        # 样式模板决定颜色/描边/位置（REQ-27）；字号仍由用户字段覆盖
        preset = style_templates.get_preset(cfg.get("subtitles.styleTemplate"))
        style = style_templates.force_style(preset, font=font or None, font_size=font_size)
        filter_expr += ":force_style='%s'" % style

    encoder = cfg.get("burn.encoder") or "libx264"
    if encoder == "h264_videotoolbox":
        video_args = ["-c:v", "h264_videotoolbox",
                      "-b:v", str(cfg.get("burn.videoBitrate") or "6000k"),
                      "-pix_fmt", "yuv420p"]
    else:
        video_args = ["-c:v", "libx264",
                      "-crf", str(cfg.get("burn.crf") or 20),
                      "-preset", str(cfg.get("burn.preset") or "medium"),
                      "-pix_fmt", "yuv420p"]
    audio = (cfg.get("burn.audio") or "copy")
    if audio == "copy":
        audio_args = ["-c:a", "copy"]
    elif audio == "opus":
        audio_args = ["-c:a", "libopus", "-b:a", "192k"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", "192k"]

    ensure_dir(os.path.dirname(target_path))
    partial = partial_path(target_path)
    src_duration = probe_duration(cfg, source_video) or 0
    timeout = max(1800, int(src_duration * 8) + 1800)

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", source_video,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", filter_expr] + video_args + audio_args + \
          ["-movflags", "+faststart", partial]
    log.info("压制中（%s，源 %s，%s）" % (encoder, human_duration(src_duration),
                                       human_size(file_size(source_video) or 0)))
    rc, out, err = run_cmd(cmd, timeout=timeout)
    if rc != 0 or not file_size(partial):
        if os.path.exists(partial):
            os.unlink(partial)
        raise EngineError(6, "ffmpeg 压制失败（rc=%s）" % rc,
                          detail=(err or out).strip()[-1200:])

    out_duration = probe_duration(cfg, partial)
    if src_duration > 5 and out_duration and out_duration < src_duration * 0.95:
        os.unlink(partial)
        raise EngineError(6, "压制产物时长异常（源 %.1fs，产出 %.1fs）"
                          % (src_duration, out_duration))
    os.replace(partial, target_path)
    log.info("压制完成：%s（%s）" % (os.path.basename(target_path),
                                   human_size(file_size(target_path))))
    return target_path


# ==================================================================== 状态

def usage_path(cfg, date_str=None):
    return os.path.join(cfg.state_dir, "usage-%s.json" % (date_str or today_str()))


def load_usage(cfg, date_str=None):
    data = read_json(usage_path(cfg, date_str), None)
    if not isinstance(data, dict):
        data = {"date": date_str or today_str(), "items": []}
    data.setdefault("items", [])
    data["count"] = len([i for i in data["items"]
                         if i.get("status") in ("ok", "no-speech")])
    return data


def bump_usage(cfg, record):
    data = load_usage(cfg)
    data["items"].append(record)
    data["count"] = len([i for i in data["items"]
                         if i.get("status") in ("ok", "no-speech")])
    atomic_write_json(usage_path(cfg), data)
    return data


def load_skip_list(cfg):
    data = read_json(os.path.join(cfg.state_dir, "skipped.json"), {})
    return data if isinstance(data, dict) else {}


def record_skip(cfg, name, reason, detail="", dir_prefix="", dir_name="", source_dir=""):
    """写入历史失败名单。

    多目录模式下键带目录前缀，避免不同目录的同名文件互相牵连；
    旧的无前缀条目仍然有效（向后兼容）。
    """
    data = load_skip_list(cfg)
    key = "%s%s" % (dir_prefix, name) if dir_prefix else name
    data[key] = {"reason": reason, "detail": detail, "at": now_iso(),
                 "dirName": dir_name, "sourceDir": source_dir}
    atomic_write_json(os.path.join(cfg.state_dir, "skipped.json"), data, mode=0o600)


TRANSLATED_FILE = "translated.json"
# 台账条目上限：老条目按 at 时间淘汰，避免文件无限长大。
TRANSLATED_MAX = 5000


def translated_path(cfg):
    return os.path.join(cfg.state_dir, TRANSLATED_FILE)


def load_translated(cfg):
    """成品台账：{键: {output, mode, at, sourceDir}}。

    键与失败名单同一套约定：多目录时带 dir_tag 前缀，单目录就是文件名。
    这台账回答的是「这个来源文件译过没有」，与当前输出目录、输出方式无关
    —— 换目录或从压制切到只出字幕都不会让它失效（REQ-59）。
    """
    data = read_json(translated_path(cfg), {})
    return data if isinstance(data, dict) else {}


def record_translated(cfg, key, output, mode, source_dir=""):
    """成功产出一部就记一笔。output 是成品绝对路径（远端是远端路径）。"""
    if not key or not output:
        return
    data = load_translated(cfg)
    data[key] = {"output": output, "mode": mode or "", "at": now_iso(),
                 "sourceDir": source_dir or ""}
    if len(data) > TRANSLATED_MAX:
        ordered = sorted(data.items(),
                         key=lambda kv: ((kv[1] or {}).get("at") or ""))
        data = dict(ordered[-TRANSLATED_MAX:])
    atomic_write_json(translated_path(cfg), data, mode=0o600)


def _ledger_hits(cfg, entry, ledger, names, prefix):
    """挑出本目录这张名单在台账里的命中：{源片名: 成品路径}。

    值必须是个 dict：这文件用户可以手改，写成 {"x": "y"} 也不能崩整轮（R2）。
    """
    hits = {}
    for name in names:
        info = ledger.get((prefix + name) if prefix else name)
        if not isinstance(info, dict):
            continue
        output = info.get("output") or ""
        if output:
            hits[name] = output
    return hits


def verify_ledger_hits(entry, hits, log, max_dirs=8):
    """确认台账里的成品还在，返回 (确认还在的命中, 没能核实的条数)。

    成品可能落在当前输出目录之外（换过输出目录），所以按成品所在目录分组，
    每组建一个端点列一次目录。

    关键是区分两种「没看到」（R2）：
      - **看清了、成品不在** —— 判未译，会重译。这正是台账要修的假阳性。
      - **没法看** —— 端点连不上、列目录报错、目录多过 max_dirs 没去列。
        这类命中一律保留：一次核不到就重译，等于网络抖一下白烧几小时额度，
        比暂时漏一个「成品已被手动删掉」的检出危险得多。条数会回给
        build_plan，写进 plan 与运行日志，让用户看得见这笔账没核。
    """
    if not hits:
        return {}, 0
    by_dir = {}
    for name, output in hits.items():
        by_dir.setdefault(os.path.dirname(output), []).append((name, output))
    current = os.path.abspath(os.path.expanduser(
        ((entry.get("output") or {}).get("path") or "")))
    verified = {}
    unchecked = 0

    def keep_unchecked(pairs, why):
        nonlocal unchecked
        unchecked += len(pairs)
        for name, output in pairs:
            verified[name] = output
        log.warn("成品台账 %d 条未能核实（%s），暂按已译跳过——"
                 "确认成品真不在了就删掉 state/translated.json 里的对应条目"
                 % (len(pairs), why))

    for directory, pairs in by_dir.items():
        if len(by_dir) > max_dirs and os.path.abspath(directory) != current:
            keep_unchecked(pairs, "成品目录多过 %d 个，没有去列" % max_dirs)
            continue
        output_cfg = entry.get("output") or {}
        try:
            endpoint = Endpoint(output_cfg.get("kind"), directory,
                                output_cfg.get("ssh"), logger=log)
            if not endpoint.probe().get("ok"):
                keep_unchecked(pairs, "输出目录连不上：%s" % directory)
                continue
            present = set(item["name"] for item in endpoint.list_files(["*"], False))
        except EngineError as exc:
            keep_unchecked(pairs, "列目录失败：%s" % exc.message)
            continue
        for name, output in pairs:
            if os.path.basename(output) in present:
                verified[name] = output
    return verified, unchecked


def last_run_path(cfg):
    return os.path.join(cfg.state_dir, "last-run.json")


def write_last_run(cfg, payload):
    ensure_dir(cfg.state_dir)
    atomic_write_json(last_run_path(cfg), payload)


# ==================================================================== 选片 / 计划

def make_endpoints(cfg, log):
    """顶层 source / output（单目录模式或新建目录时的默认值）。"""
    source = Endpoint(cfg.get("source.kind"), cfg.get("source.path"),
                      cfg.get("source.ssh"), logger=log)
    output = Endpoint(cfg.get("output.kind"), cfg.get("output.path"),
                      cfg.get("output.ssh"), logger=log)
    return source, output


def endpoints_for(entry, log):
    """某个目录项的来源/输出端点。"""
    source_cfg = entry["source"]
    output_cfg = entry["output"]
    source = Endpoint(source_cfg.get("kind"), source_cfg.get("path"),
                      source_cfg.get("ssh"), logger=log)
    output = Endpoint(output_cfg.get("kind"), output_cfg.get("path"),
                      output_cfg.get("ssh"), logger=log)
    return source, output


def build_plan(cfg, log, max_override=None, only=None, write_state=True):
    """列目录 + 选片，返回 JSON 友好的 plan 字典（只读，不建目录）。

    多目录模式：dirs 里的每一项都参与选片，maxPerRun 是本轮全局上限。

    write_state=False 给 dry-run / plan 用：这两个入口声称「无副作用」，
    而历史上它们仍会触发失败名单的一次性键迁移写入 —— 与持锁真跑并发时
    是 last-writer-wins，能吞掉刚记下的失败条目（R2）。
    """
    started = now_iso()
    entries = config_mod.dir_entries(cfg.data)
    log.info("目录模式：%d 个来源目录%s"
             % (len(entries), "" if len(entries) > 1 else "（单目录配置）"))

    skip_list = migrate_skip_keys(cfg, config_mod.dir_entries(cfg.data), log,
                                  write=write_state) or load_skip_list(cfg)
    ledger = load_translated(cfg)
    usage = load_usage(cfg)
    used_today = usage["count"]

    force = bool(only)
    max_per_run = int(cfg.get("selection.maxPerRun") or 0)
    if max_override is not None:
        cfg.data.setdefault("selection", {})["maxPerRun"] = int(max_override)
        max_per_run = int(max_override)

    all_candidates = []
    all_selected = []
    dir_reports = []
    quota_used = used_today
    order = cfg.get("selection.order") or "mtime-asc"
    unreachable = []

    for entry in entries:
        source, output = endpoints_for(entry, log)
        source_cfg = entry["source"]
        output_cfg = entry["output"]
        probe = source.probe()
        if not probe["ok"]:
            # 多目录：单个来源掉线只跳过该目录，不拖垮其他目录；
            # 全部来源都不可达时才算全局故障（与单目录行为一致）。
            log.warn("来源不可达，跳过该目录（%s）：%s" % (entry["name"], probe["detail"]))
            unreachable.append(entry["name"])
            dir_reports.append({
                "index": entry["index"],
                "name": entry["name"],
                "reachable": False,
                "source": {
                    "kind": source_cfg.get("kind"),
                    "path": source_cfg.get("path"),
                    "display": source.describe(),
                    "reachable": False,
                    "probe": probe,
                    "include": source_cfg.get("include") or ["*"],
                    "recursive": bool(source_cfg.get("recursive")),
                    "fileCount": 0,
                },
                "output": {
                    "kind": output_cfg.get("kind"),
                    "path": output_cfg.get("path"),
                    "display": output.describe(),
                    "exists": False,
                    "existingCount": 0,
                    "nameTemplate": output_cfg.get("nameTemplate"),
                },
                "counts": {"total": 0, "selected": 0, "skipped": 0, "byReason": {}},
                "selected": [],
            })
            continue

        include = source_cfg.get("include") or ["*"]
        recursive = bool(source_cfg.get("recursive"))
        files = source.list_files(include, recursive)
        log.info("来源 %s（%s）：匹配 %d 个文件"
                 % (source.describe(), entry["name"], len(files)))

        existing = []
        output_probe = output.probe()
        if output_probe["ok"]:
            try:
                existing = [item["name"] for item in output.list_files(["*"], False)]
            except EngineError as exc:
                log.warn("读取输出目录失败（按空处理）：%s" % exc.message)

        # 多目录时失败名单按目录隔离，避免不同目录的同名文件互相牵连
        skip_prefix = dir_tag(entry) if len(entries) > 1 else ""
        # 台账：先按名单挑命中，再确认成品还在（换过输出目录也能认出来）
        raw_hits = _ledger_hits(cfg, entry, ledger, [item["name"] for item in files],
                               skip_prefix)
        verified_hits, ledger_unchecked = verify_ledger_hits(entry, raw_hits, log)
        missing_hits = len(raw_hits) - len(verified_hits)
        if missing_hits > 0:
            log.warn("成品台账命中 %d 个，其中 %d 个成品已不在，按未译处理"
                     % (len(raw_hits), missing_hits))
        picked = selection.select(files, cfg, existing_outputs=existing,
                                  # 用端点解析后的路径（Endpoint.path 已经过 expand）：
                                  # 配置里写 $MEDIA/out 或 ~ 时，文件列表里是展开后的
                                  # 绝对路径，比 describe() 那种 "local:/x" 标签比得上（R3）
                                  output_dir=output.path,
                                  used_today=quota_used, only=only,
                                  skip_list=skip_list, force=force,
                                  name_template=output_cfg.get("nameTemplate"),
                                  skip_key_prefix=skip_prefix,
                                  output_ext="srt" if output_mode(cfg) == "srt" else None,
                                  translated_ledger=verified_hits)
        quota_used += len(picked["selected"])
        for candidate in picked["candidates"]:
            candidate["dirIndex"] = entry["index"]
            candidate["dirName"] = entry["name"]
            candidate["sourceDir"] = source_cfg.get("path")
        all_candidates.extend(picked["candidates"])
        all_selected.extend(picked["selected"])
        dir_reports.append({
            "index": entry["index"],
            "name": entry["name"],
            "reachable": True,
            "ledger": {
                "hits": len(verified_hits),
                "missing": missing_hits,
                "unchecked": ledger_unchecked,
            },
            "source": {
                "kind": source_cfg.get("kind"),
                "path": source_cfg.get("path"),
                "display": source.describe(),
                "reachable": probe["ok"],
                "probe": probe,
                "include": include,
                "recursive": recursive,
                "fileCount": len(files),
            },
            "output": {
                "kind": output_cfg.get("kind"),
                "path": output_cfg.get("path"),
                "display": output.describe(),
                "exists": output_probe["ok"],
                "existingCount": len(existing),
                "nameTemplate": output_cfg.get("nameTemplate"),
            },
            "counts": picked["counts"],
            "selected": [c["name"] for c in picked["selected"]],
        })

    # 全局排序：各目录内部已排序，合并后按同一规则再排一次，顺序稳定可预期
    selection.sort_candidates(all_candidates, order)
    all_selected = [c for c in all_candidates if c.get("selected")]

    # 全部来源都不可达 = 全局故障（与单目录行为一致，报错退出而不是空跑）
    if unreachable and len(unreachable) == len(entries):
        raise EngineError(4, "全部来源都不可达：%s" % "、".join(unreachable),
                          detail={"dirs": dir_reports})

    for candidate in all_selected:
        prompt_set = prompts.build(cfg, candidate["name"], candidate["path"])
        candidate["promptProfile"] = prompt_set["profileName"] or "default"
        candidate["promptMatched"] = prompt_set["matched"]

    counts = {"total": len(all_candidates), "selected": len(all_selected)}
    counts["skipped"] = counts["total"] - counts["selected"]
    by_reason = {}
    for candidate in all_candidates:
        by_reason[candidate["reason"]] = by_reason.get(candidate["reason"], 0) + 1
    counts["byReason"] = by_reason
    remaining = -1
    if max_per_run > 0 and not force:
        remaining = max(0, max_per_run - used_today)
    quota = {
        "maxPerRun": max_per_run,
        "usedToday": used_today,
        "remaining": remaining,
        "unlimited": max_per_run <= 0,
    }

    first = {}
    for report in dir_reports:
        if report.get("reachable"):
            first = report
            break
    if not first:
        first = dir_reports[0] if dir_reports else {}
    payload = {
        "generatedAt": started,
        "configPath": cfg.path,
        "multiDir": len(entries) > 1,
        "dirs": dir_reports,
        "unreachableDirs": unreachable,
        "source": first.get("source") or {},
        "output": first.get("output") or {},
        "selection": {
            "order": order,
            "maxPerRun": max_per_run,
            "skipTranslated": cfg.get("selection.skipTranslated"),
            "usedToday": used_today,
            "remaining": remaining,
            "counts": counts,
        },
        "quota": quota,
        "candidates": all_candidates,
        "selected": [c["name"] for c in all_selected],
        "selectedDetail": all_selected,
        "skipListSize": len(skip_list),
        "dataDir": cfg.data_dir,
    }
    return payload


def dry_run_plan(cfg, log, plan):
    """dry-run 输出：只描述将要发生的动作，不产生任何副作用。"""
    actions = []
    dir_outputs = {d.get("index"): d.get("output") or {} for d in (plan.get("dirs") or [])}
    entries = config_mod.dir_entries(cfg.data)
    multi = len(entries) > 1
    dir_salts = {e["index"]: _dir_salt(e) for e in entries} if multi else {}
    entries_by_index = {e["index"]: e for e in entries}
    for candidate in plan["selectedDetail"]:
        preview = prompts.build(cfg, candidate["name"], candidate["path"])
        dir_output = dir_outputs.get(candidate.get("dirIndex")) or {}
        dir_kind = (entries_by_index.get(candidate.get("dirIndex")) or {}).get("source", {}).get("kind")
        if not dir_kind:
            dir_kind = plan["source"]["kind"]
        stages = ["download" if dir_kind == "ssh" else "use-local-file",
                  "extract-audio", "segment", "transcribe", "prefilter", "translate",
                  "retry", "quality-gate"]
        if cfg.get("subtitles.trimSilence"):
            stages.append("trim-silence")
        if cfg.get("subtitles.bilingual"):
            stages.append("bilingual-ass")
        # 预演与实际执行必须用同一个判定（output.mode 优先，burn.enabled 兜底）
        if output_mode(cfg) == "burn":
            stages.append("burn")
        else:
            stages.append("srt-only")
        stages.append("upload")
        actions.append({
            "name": candidate["name"],
            "sizeGB": candidate["sizeGB"],
            "expectedOutput": candidate["expectedOutput"],
            "workDir": os.path.join(cfg.work_dir,
                                    _work_dir_name(candidate["name"],
                                                   dir_salts.get(candidate.get("dirIndex"), ""))),
            "stages": stages,
            "promptProfile": preview["profileName"] or "default",
            "engine": {"kind": cfg.get("engine.kind"), "model": model_for_engine(cfg)},
            "overwrite": bool(dir_output.get("overwrite", cfg.get("output.overwrite"))),
            "dirIndex": candidate.get("dirIndex"),
            "dirName": candidate.get("dirName"),
        })
    return {
        "dryRun": True,
        "generatedAt": now_iso(),
        "configPath": cfg.path,
        "source": plan["source"],
        "output": plan["output"],
        "selection": plan["selection"],
        "quota": plan["quota"],
        "candidates": plan["candidates"],
        "selected": plan["selected"],
        "actions": actions,
        "note": "dry-run：未下载、未转写、未翻译、未压制、未上传、未写状态",
    }


def _dir_salt(entry):
    """目录项的来源标识：用于隔离不同目录下的同名文件（工作目录 / 状态记录）。"""
    if not entry:
        return ""
    source = entry.get("source") or {}
    path = (source.get("path") or "").strip()
    if not path:
        return ""
    return "%s#%s" % (source.get("kind") or "local", path)


def dir_tag(entry):
    """名单里用的目录标签：由来源 kind+path 派生，面板增删或重排目录都不会错位。

    旧版本用的是目录在列表里的位置（`[0] `、`[1] `），只要目录顺序或数量变
    动，键就指向了别的目录 —— 见 migrate_skip_keys()。
    """
    salt = _dir_salt(entry)
    if not salt:
        return ""
    import hashlib
    return "[%s] " % hashlib.md5(salt.encode("utf-8")).hexdigest()[:8]


_LEGACY_DIR_KEY = re.compile(r"^\[(\d+)\] (.+)$")


def migrate_skip_keys(cfg, entries, log=None, write=True):
    """把历史名单里按目录序号写入的键换成稳定标签（一次性，幂等）。

    序号键按「当时第 N 个目录」解释；这里只能按「现在第 N 个目录」近似。
    对没有增删过目录的配置来说两者一致；对不一致的情况，宁可少跳过一条
    （多花一次转写），也不能把文件错认成另一个目录的失败记录。
    """
    path = os.path.join(cfg.state_dir, "skipped.json")
    data = read_json(path, None)
    if not isinstance(data, dict) or not data:
        return data
    by_index = {entry.get("index"): entry for entry in entries}
    out = {}
    changed = False
    for key, value in data.items():
        match = _LEGACY_DIR_KEY.match(key) if isinstance(key, str) else None
        if match is None:
            out[key] = value
            continue
        entry = by_index.get(int(match.group(1)))
        name = match.group(2)
        tag = dir_tag(entry) if entry else ""
        new_key = tag + name if tag else name
        if new_key != key:
            changed = True
        if new_key not in out:
            out[new_key] = value
    if changed and not write:
        if log is not None:
            log.info("失败名单需要迁移，但本次是只读运行（dry-run / plan），未写入")
    elif changed:
        try:
            atomic_write_json(path, out)
            if log is not None:
                log.info("失败名单已迁移到稳定目录标签（%d 条）" % len(out))
        except OSError as exc:
            if log is not None:
                log.warn("失败名单迁移未写入：%s" % exc)
    return out


def _work_dir_name(name, salt=""):
    """工作目录名。salt 非空时混入来源，避免不同目录的同名文件互相覆盖中间产物。"""
    import hashlib
    base = sanitize_stem(name)
    raw = "%s|%s" % (name, salt) if salt else name
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (base, digest)


def work_dir_for(cfg, name, salt=""):
    return os.path.join(cfg.work_dir, _work_dir_name(name, salt))


# ==================================================================== 单片执行

class Pipeline(object):
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.entries = config_mod.dir_entries(cfg.data)
        self.by_index = {}
        for entry in self.entries:
            source, output = endpoints_for(entry, log)
            self.by_index[entry["index"]] = {
                "entry": entry, "source": source, "output": output,
            }
        self.source, self.output = make_endpoints(cfg, log)

    def ctx_for(self, candidate):
        """候选所属目录的上下文（来源/输出端点与输出配置）。"""
        index = candidate.get("dirIndex")
        if index is None:
            index = 0
        return self.by_index.get(index) or self.by_index.get(0)

    def skip_prefix(self, ctx):
        """多目录时的失败名单键前缀（单目录不加前缀，保持旧名单语义）。"""
        if len(self.entries) <= 1:
            return ""
        return dir_tag((ctx or {}).get("entry"))

    # -- 小工具
    def _cleanup_partials(self, work_dir):
        for dirpath, _dirs, files in os.walk(work_dir):
            for name in files:
                if name.endswith(".partial") or name.endswith(".tmp"):
                    try:
                        os.unlink(os.path.join(dirpath, name))
                    except OSError:
                        pass

    def _download(self, candidate, work_dir, ctx):
        source = ctx["source"]
        target = os.path.join(work_dir, "input" + (os.path.splitext(candidate["name"])[1] or ".mp4"))
        if source.is_local:
            self.log.info("本地来源，直接使用原文件（不复制）")
            return candidate["path"]
        expected = candidate["sizeBytes"]
        if file_size(target) == expected:
            self.log.info("复用已下载文件：%s" % os.path.basename(target))
            return target
        self.log.stage("下载 %s（%s）" % (candidate["name"], human_size(expected)))
        return source.fetch(candidate["path"], target)

    def _upload(self, local_path, candidate, ctx):
        output_cfg = ctx["entry"]["output"]
        output = ctx["output"]
        name = selection.output_name(output_cfg.get("nameTemplate"), candidate["name"])
        target = os.path.join(output_cfg.get("path") or "", name)
        overwrite = bool(output_cfg.get("overwrite"))
        self.log.stage("上传 %s（%s）" % (name, human_size(file_size(local_path) or 0)))
        result = output.put(local_path, target, overwrite=overwrite)
        if result.get("uploaded"):
            self.log.info("上传完成：%s" % target)
        elif result.get("reason") == "exists":
            self.log.warn("输出已存在，跳过上传（overwrite=false）：%s" % target)
        return result

    # -- 主流程
    def process_one(self, candidate):
        started = time.time()
        name = candidate["name"]
        ctx = self.ctx_for(candidate)
        output_cfg = ctx["entry"]["output"]
        # 单目录沿用旧版（未加盐）的工作目录名，升级后已有的中间产物仍可续跑
        salt = _dir_salt(ctx["entry"]) if len(self.entries) > 1 else ""
        work_dir = work_dir_for(self.cfg, name, salt)
        ensure_dir(work_dir)
        self._cleanup_partials(work_dir)
        result = {
            "name": name,
            "status": "failed",
            "stage": "init",
            "message": "",
            "code": None,
            "output": None,
            "outputBytes": None,
            "elapsedSec": 0,
            "workDir": work_dir,
        }
        prompt_set = prompts.build(self.cfg, name, candidate["path"])
        self.log.info("=== 处理：%s ===" % name)
        self.log.info("prompt：%s" % prompts.describe(prompt_set))
        try:
            # 1) 下载
            result["stage"] = "download"
            video = self._download(candidate, work_dir, ctx)

            # 2) 抽音频 / 3) 分段 / 4) 转写 / 5) 合并
            wav = os.path.join(work_dir, "audio.wav")
            source_srt = os.path.join(work_dir, "source.srt")
            result["stage"] = "transcribe"
            if file_size(source_srt):
                self.log.info("复用已转写字幕：source.srt")
                self._srt_blocks = len(parse_srt_file(source_srt))
            else:
                self.log.stage("抽取音频")
                extract_audio(self.cfg, self.log, video, wav)
                # 音轨比画面晚开始时，在音频开头补静音，让时间轴对齐（REQ-01）
                align_audio_timeline(self.cfg, self.log, video, wav)
                chunks = build_chunks(self.cfg, self.log, wav, os.path.join(work_dir, "chunks"))
                transcribed = transcribe_chunks(self.cfg, self.log, chunks, engine_env(self.cfg))
                self._srt_blocks = merge_chunk_srts(
                    self.cfg, self.log, transcribed, source_srt,
                    int(self.cfg.get("transcribe.chunkSeconds") or 60))

            # 6) 预过滤 / 翻译 / 重试 / 质量门
            result["stage"] = "translate"
            pre = prefilter(self.cfg, self.log, source_srt)
            zh_srt = os.path.join(work_dir, "source.zh.srt")
            translate(self.cfg, self.log, pre, zh_srt, prompt_set)
            retry_translate(self.cfg, self.log, zh_srt)
            stats = quality_gate(self.cfg, self.log, source_srt, zh_srt,
                                 prompt_set.get("series") or "")

            # 7) 静音修剪
            result["stage"] = "trim"
            trim_silence(self.cfg, self.log, zh_srt, wav)

            # 8) 双语 ASS / 压制
            height = probe_height(self.cfg, video)
            ass_path = None
            bilingual = None
            mode = output_mode(self.cfg)
            if self.cfg.get("subtitles.bilingual"):
                # 双语文件两条路都要用：压片时喂给 ASS，只出字幕时它就是成品
                result["stage"] = "bilingual"
                bilingual = build_bilingual_srt(self.cfg, self.log, source_srt, zh_srt)
                if mode == "burn":
                    ass_path = os.path.join(work_dir, "source.zh.ass")
                    build_ass(self.cfg, self.log, bilingual, ass_path, height)

            if mode == "burn":
                result["stage"] = "burn"
                out_name = selection.output_name(output_cfg.get("nameTemplate"), name)
                out_path = os.path.join(work_dir, "out", out_name)
                if ass_path:
                    burn_video(self.cfg, self.log, video, ass_path, out_path,
                               ass_mode=True, height=height)
                else:
                    burn_video(self.cfg, self.log, video,
                               prepare_burn_srt(self.cfg, self.log, zh_srt), out_path,
                               ass_mode=False, height=height)
                upload_arg = out_path
            else:
                self.log.info("输出方式：只生成字幕文件，不压片")
                result["stage"] = "srt-only"
                # 勾了双语就交付双语那份，否则交付单语译文
                upload_arg = bilingual or zh_srt

            # 9) 上传
            result["stage"] = "upload"
            if mode == "burn":
                upload = self._upload(upload_arg, candidate, ctx)
            else:
                # 成品名跟视频模板一致，只换扩展名（跳过判定也用同一个函数）
                target_name = selection.output_name(
                    output_cfg.get("nameTemplate"), name, ext="srt")
                target = os.path.join(output_cfg.get("path") or "", target_name)
                self.log.stage("上传字幕 %s" % target_name)
                upload = ctx["output"].put(upload_arg, target,
                                           overwrite=bool(output_cfg.get("overwrite")))
            result["output"] = upload.get("path")
            result["outputBytes"] = upload.get("size")
            result["dirIndex"] = candidate.get("dirIndex")
            result["dirName"] = candidate.get("dirName")
            result["status"] = "ok"
            result["stage"] = "done"
            result["translation"] = stats
            result["promptProfile"] = prompt_set["profileName"] or "default"
            # 成品台账：记「这部来源片译过了」，键带目录前缀，值与当前输出
            # 目录/输出方式无关 —— 换目录、换输出方式下次仍认得出（REQ-59）。
            record_translated(self.cfg, self.skip_prefix(ctx) + name,
                              result["output"], mode,
                              (ctx["entry"].get("source") or {}).get("path") or "")

            # 10) 清理中间产物
            self._cleanup(work_dir, video, uploaded=bool(upload.get("uploaded")))
        except NoSpeech as exc:
            result["status"] = "skipped"
            result["stage"] = "transcribe"
            result["message"] = str(exc)
            record_skip(self.cfg, name, "no-speech", str(exc),
                        dir_prefix=self.skip_prefix(ctx),
                        dir_name=result.get("dirName") or "",
                        source_dir=(ctx["entry"].get("source") or {}).get("path") or "")
            self.log.warn("跳过（无语音）：%s" % name)
        except EngineError as exc:
            result["status"] = "failed"
            result["code"] = exc.code
            result["message"] = exc.message
            result["detail"] = exc.detail
            self.log.error("失败于 %s：%s（code=%s）" % (result["stage"], exc.message, exc.code))
        except Exception as exc:  # 兜底：任何未预期异常都不能让进程崩掉
            result["status"] = "failed"
            result["code"] = 1
            result["message"] = "%s: %s" % (type(exc).__name__, exc)
            self.log.error("未预期异常：%s" % result["message"])
        result["elapsedSec"] = round(time.time() - started, 1)
        return result

    def _cleanup(self, work_dir, video, uploaded):
        keep_artifacts = bool(self.cfg.get("paths.keepWorkArtifacts"))
        keep_audio = bool(self.cfg.get("transcribe.keepAudio"))
        shutil.rmtree(os.path.join(work_dir, "chunks"), ignore_errors=True)
        if not keep_audio:
            for name in ("audio.wav", "audio.wav.partial"):
                path = os.path.join(work_dir, name)
                if os.path.exists(path):
                    os.unlink(path)
        if not keep_artifacts:
            # 删除大体积中间产物：下载的视频副本与压制成品（已上传）
            for name in ("input.mp4", "input.mkv", "input.webm"):
                path = os.path.join(work_dir, name)
                if os.path.exists(path):
                    os.unlink(path)
            if uploaded:
                shutil.rmtree(os.path.join(work_dir, "out"), ignore_errors=True)
        if not self.cfg.get("subtitles.keepOriginalAsSrt"):
            source_srt = os.path.join(work_dir, "source.srt")
            if os.path.exists(source_srt):
                os.unlink(source_srt)


# ==================================================================== 批量执行

def execute(cfg, log, plan, dry_run=False, notify_module=None):
    """执行 plan 里选中的文件，返回 run 结果字典（含 results 与退出码依据）。"""
    if dry_run:
        return dry_run_plan(cfg, log, plan)

    started_at = now_iso()
    ensure_dir(cfg.data_dir)
    ensure_dir(cfg.work_dir)
    ensure_dir(cfg.logs_dir)
    ensure_dir(cfg.state_dir)

    run_id = time.strftime("%Y%m%d-%H%M%S")
    selected = plan.get("selectedDetail") or []
    results = []
    dir_displays = ["%s → %s" % ((d.get("source") or {}).get("display", ""),
                                 (d.get("output") or {}).get("display", ""))
                    for d in (plan.get("dirs") or [])]
    source_display = "；".join(dir_displays) if dir_displays \
        else plan["source"].get("display", "")
    payload = {
        "runId": run_id,
        "mode": "run",
        "startedAt": started_at,
        "finishedAt": None,
        "configPath": cfg.path,
        "source": source_display,
        "output": plan["output"].get("display", ""),
        "multiDir": bool(plan.get("multiDir")),
        "dirs": [{"name": d.get("name"), "source": (d.get("source") or {}).get("display", ""),
                  "output": (d.get("output") or {}).get("display", ""),
                  "reachable": d.get("reachable", True)}
                 for d in (plan.get("dirs") or [])],
        "selected": [c["name"] for c in selected],
        "results": results,
        "totals": {"selected": len(selected), "ok": 0, "skipped": 0, "failed": 0},
    }
    write_last_run(cfg, payload)

    if notify_module and selected:
        from lib.notify import should_notify
        if should_notify(cfg, "start"):
            notify_module.send(cfg, "开始处理",
                               "%d 个文件（%s）" % (len(selected), source_display or "本地目录"), log)

    pipeline = Pipeline(cfg, log)
    abort = None
    dead_dirs = set()
    multi_dir = bool(plan.get("multiDir"))
    for index, candidate in enumerate(selected, 1):
        if candidate.get("dirIndex") in dead_dirs:
            log.warn("来源已不可达，跳过该目录剩余文件：%s" % candidate.get("dirName"))
            # 仍然记账，否则「选中 N」与「成功+跳过+失败」对不上
            results.append({
                "name": candidate.get("name"),
                "status": "skipped",
                "message": "来源目录不可达",
                "code": None,
                "dirIndex": candidate.get("dirIndex"),
                "dirName": candidate.get("dirName"),
                "elapsedSec": 0,
            })
            payload["totals"]["skipped"] += 1
            continue
        log.info("---------- [%d/%d] %s ----------" % (index, len(selected), candidate["name"]))
        result = pipeline.process_one(candidate)
        results.append(result)
        totals = payload["totals"]
        if result["status"] == "ok":
            totals["ok"] += 1
        elif result["status"] == "skipped":
            totals["skipped"] += 1
        else:
            totals["failed"] += 1
        if result["status"] != "failed":
            bump_usage(cfg, {"name": result["name"], "at": now_iso(),
                             "status": "ok" if result["status"] == "ok" else "no-speech",
                             "output": result.get("output"),
                             "dirName": result.get("dirName"),
                             "dirIndex": result.get("dirIndex"),
                             "elapsedSec": result.get("elapsedSec")})
        write_last_run(cfg, payload)
        code = result.get("code")
        if code == 4 and multi_dir:
            # 多目录：某个来源掉线只废掉该目录，不拖垮其他目录
            dead_dirs.add(candidate.get("dirIndex"))
            log.error("来源不可达（code=4），跳过该目录剩余文件：%s"
                      % (candidate.get("dirName") or "目录 %s" % candidate.get("dirIndex")))
            continue
        if code in (3, 4, 8):  # 依赖缺失 / 来源不可达 / 磁盘不足：全局性问题，停止后续
            abort = code
            log.error("全局性错误（code=%s），中止本轮剩余任务" % code)
            break

    payload["finishedAt"] = now_iso()
    payload["abort"] = abort
    totals = payload["totals"]
    # 中止时账要平：选中数里剩下的那些不是失败，是「没轮到」
    totals["notRun"] = max(0, totals["selected"] - totals["ok"]
                           - totals["skipped"] - totals["failed"])
    write_last_run(cfg, payload)
    summary = "选中 %d，成功 %d，跳过 %d，失败 %d" % (
        totals["selected"], totals["ok"], totals["skipped"], totals["failed"])
    if totals["notRun"]:
        summary += "，未处理 %d（本轮中止）" % totals["notRun"]
    log.info("本轮结束：%s" % summary)

    if notify_module:
        from lib.notify import should_notify, resolve_events
        # 成功与失败各自独立判定（REQ-46）：
        #   run        = 旧语义，整轮零失败才算成功（空跑也算成功）
        #   anyOutput  = 至少有产出一部就算成功；空跑不报成功也不报失败
        # 失败永远照报 —— 部分成功时用户既该知道「成了几部」也该知道「差在哪」。
        events = resolve_events(cfg, totals, results, abort)
        for event in events:
            if not should_notify(cfg, event):
                continue
            lines = ["%s" % summary]
            for item in results:
                if item["status"] == "failed" and event != "success":
                    lines.append("失败：%s（%s）" % (item["name"], item["message"][:80]))
                elif item["status"] == "ok" and item.get("output"):
                    lines.append("产出：%s" % os.path.basename(item["output"]))
                    detail = item.get("translation") or {}
                    # 成品缺一半时通知也要说清楚是几条、卡在哪，否则「成功」
                    # 这两个字会让人以为整片都翻好了。
                    if detail.get("untranslated"):
                        sample = "、".join(detail.get("untranslatedIds") or [])
                        lines.append("未译：%d 条%s" % (
                            detail["untranslated"], ("（%s）" % sample) if sample else ""))
                    if detail.get("truncatedBatches"):
                        lines.append("截断：%d 批在预算处被切断" % detail["truncatedBatches"])
            # 两条一起发时正文不互相抄：成功那条列产出，失败那条列失败原因，
            # 只在确实有产出时补一句计数，免得「处理有失败」里塞满文件名（R3）。
            if event == "failure":
                failures = [line for line in lines[1:] if line.startswith("失败：")]
                produced_lines = [line for line in lines[1:] if line.startswith("产出：")]
                details = [line for line in lines[1:]
                           if not line.startswith("失败：") and not line.startswith("产出：")]
                lines = ["%s" % summary] + failures + details
                if produced_lines:
                    lines.append("另有 %d 部已产出（明细见成功通知）" % len(produced_lines))
            title = {"success": "处理完成", "failure": "处理有失败"}.get(event, "处理完成")
            if event == "success" and totals["selected"] == 0:
                title = "本轮无新片"
            notify_module.send(cfg, title, "\n".join(lines), log)
    return payload


def compute_exit_code(payload):
    """按 DESIGN §4 的退出码汇总：优先第一个失败的具体码。"""
    if payload.get("dryRun"):
        return 0
    for result in payload.get("results") or []:
        if result.get("status") == "failed":
            code = result.get("code") or 1
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = 1
            if code in (2, 3, 4, 5, 6, 7, 8, 9):
                return code
            return 1
    return 0


# ==================================================================== status

def status(cfg):
    from lib.runtime import pid_alive
    lock_path = os.path.join(cfg.state_dir, "run.lock")
    holder = read_json(lock_path, None)
    running = bool(holder and pid_alive(holder.get("pid")))
    last = read_json(last_run_path(cfg), None)
    usage = load_usage(cfg)
    skips = load_skip_list(cfg)
    log_path = os.path.join(cfg.logs_dir, "run.log")
    return {
        "running": running,
        "lock": {"path": lock_path, "holder": holder if running else None},
        "lastRun": last,
        "today": {
            "date": usage.get("date") or today_str(),
            "count": usage.get("count", 0),
            "maxPerRun": int(cfg.get("selection.maxPerRun") or 0),
            "items": usage.get("items") or [],
        },
        "skipList": {"count": len(skips), "entries": skips},
        "paths": cfg.data_dirs(),
        "logs": {
            "runLog": log_path,
            "sizeBytes": file_size(log_path),
            "workerLogSizeBytes": file_size(os.path.join(cfg.logs_dir, "doctor.log")),
        },
        "engine": {"kind": cfg.get("engine.kind"), "model": model_for_engine(cfg)},
        "generatedAt": now_iso(),
    }


# ==================================================================== selftest

SELFTEST_SRT = """1
00:00:00,500 --> 00:00:02,200
こんにちは、字幕工坊のテストです

2
00:00:02,500 --> 00:00:04,500
字幕が正しく焼けていますか
"""


def selftest(cfg, log, skip_translate=False, keep=False):
    """5 秒合成片走通：合成 → 固定 SRT → 翻译 2 条 → 压制 → ffprobe。"""
    ffmpeg = ffmpeg_bin(cfg)
    root = os.path.join(cfg.work_dir, "selftest")
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    ensure_dir(root)

    steps = []

    def step(name, detail=""):
        steps.append({"name": name, "detail": detail, "at": now_iso()})
        log.stage("selftest[%d]: %s %s" % (len(steps), name, detail))

    # 1) 合成 5 秒视频（testsrc2 + sine），不依赖网络
    src = os.path.join(root, "selftest_src.mp4")
    if not file_size(src):
        rc, out, err = run_cmd([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=5",
                                "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-shortest", src], timeout=300)
        if rc != 0 or not file_size(src):
            raise EngineError(6, "合成测试视频失败", detail=(err or out).strip()[-500:])
    step("合成测试视频", human_size(file_size(src)))

    # 2) 固定两行日文 SRT（跳过 whisper，专测翻译+压制链路）
    src_srt = os.path.join(root, "source.srt")
    atomic_write_text(src_srt, SELFTEST_SRT)
    step("写入固定字幕", "2 条")

    # 3) 翻译 2 条（真调配置里的引擎）
    zh_srt = os.path.join(root, "source.zh.srt")
    translate_info = {"skipped": True, "engine": None, "model": None}
    if not skip_translate:
        prompt_set = prompts.build(cfg, "selftest.mp4", src)
        system_file = os.path.join(root, "system_prompt.txt")
        if prompt_set.get("system"):
            atomic_write_text(system_file, prompt_set["system"])
        cmd = [python_bin(), os.path.join(VENDOR_DIR, "translate_srt.py"),
               src_srt, zh_srt,
               "--engine", engine_kind_for_vendor(cfg),
               "--model", model_for_engine(cfg),
               "--temperature", str(cfg.get("engine.temperature")),
               "--seed", str(cfg.get("engine.seed")),
               "--max-tokens", str(cfg.get("engine.maxTokens")),
               "--batch-size", "2"]
        if cfg.get("engine.kind") == "dsh":
            cmd += ["--provider", provider_for_engine(cfg)]
        if prompt_set.get("system"):
            cmd += ["--system-file", system_file]
        if prompt_set.get("context"):
            cmd += ["--video-context", prompt_set["context"]]
        if prompt_set.get("series"):
            cmd += ["--series", prompt_set["series"]]
        if cfg.get("engine.kind") == "ollama":
            cmd += ["--ollama-host", cfg.get("engine.ollamaHost") or ""]
            if (cfg.get("engine.ollamaModel") or "").strip():
                cmd += ["--ollama-model", cfg.get("engine.ollamaModel")]
        rc, out, err = run_cmd(cmd, timeout=int(cfg.get("engine.timeoutSeconds") or 300) + 180,
                               env=engine_env(cfg))
        if rc != 0:
            raise EngineError(5, "自检翻译失败（引擎 %s/%s，rc=%s）：%s"
                              % (cfg.get("engine.kind"), model_for_engine(cfg), rc,
                                 (err or out).strip()[-500:]),
                              detail="可用 --skip-translate 只测压制链路")
        if not file_size(zh_srt):
            raise EngineError(5, "自检翻译输出为空（引擎 %s）" % cfg.get("engine.kind"))
        zh_blocks = parse_srt_file(zh_srt)
        has_chinese = any(CJK_RE.search(b.get("text") or "") for b in zh_blocks)
        translate_info = {
            "skipped": False,
            "engine": cfg.get("engine.kind"),
            "model": model_for_engine(cfg),
            "blocks": len(zh_blocks),
            "hasChinese": has_chinese,
        }
        if not has_chinese:
            log.warn("翻译结果未见中文（可能引擎降级输出原文），继续测压制")
        step("翻译 2 条", "引擎=%s 输出=%d 条 中文=%s"
             % (cfg.get("engine.kind"), len(zh_blocks), has_chinese))
    else:
        shutil.copyfile(src_srt, zh_srt)
        translate_info = {"skipped": True, "reason": "--skip-translate"}
        step("跳过翻译", "使用固定 SRT")

    # 4) 压制
    out = os.path.join(root, "selftest_zh.mp4")
    burn_video(cfg, log, src, zh_srt, out, ass_mode=False, height=360)
    step("压制成品", human_size(file_size(out)))

    # 5) 校验
    summary = probe_summary(cfg, out)
    src_summary = probe_summary(cfg, src)
    if not summary["durationSec"] or abs(summary["durationSec"] - src_summary["durationSec"]) > 1.0:
        raise EngineError(6, "自检成品时长异常（源 %.2fs，产出 %.2fs）"
                          % (src_summary["durationSec"], summary["durationSec"]))
    if not summary.get("video"):
        raise EngineError(6, "自检成品没有视频轨")
    step("ffprobe 校验", "%s %.2fs %dx%d"
         % (summary["video"]["codec"], summary["durationSec"],
            summary["video"]["width"], summary["video"]["height"]))

    return {
        "ok": True,
        "artifact": out,
        "artifactBytes": file_size(out),
        "workDir": root,
        "translate": translate_info,
        "ffprobe": summary,
        "sourceProbe": src_summary,
        "steps": steps,
        "generatedAt": now_iso(),
    }

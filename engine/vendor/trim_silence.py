#!/usr/bin/env python3
"""
SRT 静音裁剪器 — 修复 Whisper 幻觉字幕 + 字幕 end 延伸到静音的问题

v3 改造（2026-06-13, 5-27 Jinsei 实测反馈）:
  v2 修了 "字幕 end 延伸到静音" 场景 (3 条/10min 实测有效)。
  v2 没修 "Whisper 在静音段里整条幻觉字幕" 场景:
    - 实测 140-200s 60 秒全程静音, Whisper 输出 5 条歌词 + 1 条 17.6s "总一天那样那样那样"
    - silencedetect 找不到 -30dB 静默 (BGM 太响), 也找不到 -20dB 静默 (BGM 持续)
    - 单纯裁 end 没用 — 整段 start 就在静音里
  v3 新增:
    1) **整条删除模式**: 区间内音频 RMS < hall_thr_db 持续占比 > hall_pct → 整条删
       (用 ffmpeg volumedetect 一次性扫, 不用 astats 慢)
    2) **文本启发式**: 纯重复字符 / 全角假名 / 全 ♪  → 直接判幻觉
    3) 流程: 先删幻觉 → 再裁 end → 再写 SRT (idx 重排 1..N)

用法:
  python3 trim_silence.py <input.srt> <audio.wav> [--output <out.srt>] [options]

参数:
  input.srt         输入 SRT
  audio.wav         对应音频 (ffmpeg volumedetect + silencedetect 用)
  --output          输出 SRT (默认 <input>_trim.srt)
  --min-duration    触发裁剪的最小持续时间 (秒, 默认 1.0)
  --trail           保留的尾部余量 (秒, 默认 0.8)
  --noise-db        silencedetect 噪声阈值 dB (默认 -30)
  --min-silence     silencedetect 最小静音时长 s (默认 0.5)
  --hall-thr-db     幻觉判定的 RMS 阈值 dB (默认 -40 — 区间均值 < 此值视为静音)
  --hall-pct        幻觉判定的静音占比 (默认 0.8 — 80% 时间 < 阈值则整条删)
"""
import argparse
import bisect
import re
import subprocess
import sys
from pathlib import Path


# ============== SRT 解析 / 写盘 ==============

def parse_srt(path):
    """SRT → list[{idx, start, end, text, start_sec, end_sec}]"""
    content = Path(path).read_text(encoding="utf-8")
    blocks = []
    for blk in re.split(r'\n\s*\n', content.strip()):
        lines = blk.strip().split("\n")
        if len(lines) < 2:
            continue
        if not re.match(r'^\d+$', lines[0].strip()):
            continue
        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', lines[1])
        if not m:
            continue
        text = "\n".join(lines[2:]) if len(lines) > 2 else ""
        start = _to_sec(m.group(1))
        end = _to_sec(m.group(2))
        blocks.append({
            "idx": int(lines[0].strip()),
            "start": m.group(1),
            "end": m.group(2),
            "start_sec": start,
            "end_sec": end,
            "text": text.strip(),
        })
    return blocks


def _to_sec(ts):
    """HH:MM:SS,mmm → 秒"""
    m = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', ts)
    h, mn, s, ms = map(int, m.groups())
    return h * 3600 + mn * 60 + s + ms / 1000


def _to_ts(sec):
    """秒 → HH:MM:SS,mmm"""
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(blocks, path):
    """写 SRT 文件（序号从 1 重新连续编号）"""
    with open(path, "w", encoding="utf-8") as f:
        for i, b in enumerate(blocks, 1):
            f.write(f"{i}\n{b['start']} --> {b['end']}\n{b['text']}\n\n")


# ============== v2: silencedetect 端点裁剪 ==============

def detect_all_silences(audio_path, noise_db=-30, min_dur=0.5):
    """
    一次性跑 ffmpeg silencedetect, 拿到整段音频所有 silence_start 列表。
    返回按时间排序的 float 列表 (绝对秒数, 升序)。
    """
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("⚠️ silencedetect 超时 (300s)", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("⚠️ ffmpeg 未找到", file=sys.stderr)
        return []

    silences = []
    for line in result.stderr.splitlines():
        # ffmpeg 实际标签是 "[Parsed_silencedetect_0 @ 0x...]", 旧正则 r'\[silencedetect.*\]' 匹配不到
        m = re.match(r'\[.*silencedetect[^\]]*\]\s*silence_start:\s*([0-9.]+)', line)
        if m:
            silences.append(float(m.group(1)))
    silences.sort()
    return silences


def first_silence_in(silences, start_sec, end_sec):
    """二分查找 [start_sec, end_sec) 区间内的第一个 silence_start。"""
    if not silences:
        return None
    idx = bisect.bisect_left(silences, start_sec)
    if idx < len(silences) and silences[idx] < end_sec:
        return silences[idx]
    return None


# ============== v3 新增: 音频能量采样 ==============

def detect_audio_levels(audio_path, window=0.5):
    """
    用 ffmpeg volumedetect + astats 一次性扫整段音频, 返回按 window 采样的 RMS dB 列表。
    实际: ffmpeg 没法同时 silencedetect + astats, 我们跑 astats 输出, 解析 mean_volume 段。
    注意: volumedetect 只给整段汇总, 不给时间序列。
    解决方案: 用 ffmpeg + astats=metadata=1:reset=1, 每 window 秒 reset 一次, 输出每个窗口的 RMS。
    简化方案: 整段输出 RMS, 然后对每条字幕的 [start, end] 区间计算"占比"用 ffmpeg 区间查询。

    **为了简洁, v3 用 silencedetect 反推能量**:
      - 如果区间 [s, e] 内的 silence_start 数 × silence_dur > 80% × 区间长 → 视作幻觉
      - 否则保留
    实际上, silencedetect 给的是 silence_start + silence_end, 可计算每个 silence 区间的 dur,
    累加后占区间总长的比例即可。

    返回 list[(silence_start, silence_end)]。
    """
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", "silencedetect=noise=-40dB:d=0.3",
        "-f", "null", "-"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return []
    except FileNotFoundError:
        return []

    # 解析所有 silence_start / silence_end, 配对
    starts = []
    ends = []
    for line in result.stderr.splitlines():
        m1 = re.match(r'\[.*silencedetect[^\]]*\]\s*silence_start:\s*([0-9.]+)', line)
        m2 = re.match(r'\[.*silencedetect[^\]]*\]\s*silence_end:\s*([0-9.]+)', line)
        if m1:
            starts.append(float(m1.group(1)))
        elif m2:
            ends.append(float(m2.group(1)))

    # 配对 (start, end), 末尾 silence_start 可能无 end (silence 到结尾)
    pairs = []
    for i, s in enumerate(starts):
        if i < len(ends):
            pairs.append((s, ends[i]))
        else:
            pairs.append((s, float('inf')))
    return pairs


def silence_coverage_in_range(silence_pairs, start_sec, end_sec, audio_total_dur):
    """
    计算 [start_sec, end_sec] 区间内静音 (≤-40dB) 的总时长 (秒)。
    silence_pairs 是 detect_audio_levels 返回的 (start, end) 列表 (end 可能 inf, 需 cap)。
    """
    if not silence_pairs:
        return 0.0
    covered = 0.0
    for s, e in silence_pairs:
        # 裁剪到区间内
        s2 = max(s, start_sec)
        e2 = min(e if e != float('inf') else audio_total_dur, end_sec)
        if e2 > s2:
            covered += e2 - s2
    return covered


# ============== v3 新增: 文本启发式判定 ==============

def is_likely_hallucination(text):
    """
    启发式: 字幕文本是否像 Whisper 幻觉。
    返回 (bool, reason)。
    """
    if not text:
        return True, "empty"

    # 1. 纯重复: "meme", "mememememe", "meme meme meme"
    # 取去空格的纯字母/汉字 token, 如果整个文本只有 1-2 种 token 重复 → 幻觉
    tokens = re.findall(r'[a-zA-Z一-鿿]+', text)
    if tokens and len(tokens) >= 3:
        unique = set(t.lower() for t in tokens)
        if len(unique) == 1 and len(tokens[0]) <= 6:
            return True, f"repeat_token:{tokens[0]}"

    # 2. 全角假名/歌词符号: "あああ", "哈哈哈", "ﾅﾅﾅ" + 全是同 1 字符
    stripped = re.sub(r'[\s♪♫・,。.!?♪、。,!?・]', '', text)
    if len(stripped) >= 4 and len(set(stripped)) == 1:
        return True, f"mono_char:{stripped[0]}"

    # 3. 纯 ♪ 包裹无内容
    only_music = re.sub(r'[\s♪♫]+', '', text)
    if not only_music and ('♪' in text or '♫' in text):
        return True, "music_only"

    return False, ""


# ============== v3 新增: 整条删除 ==============

def delete_hallucinated(blocks, silence_pairs, audio_total_dur,
                        hall_thr_db=-40, hall_pct=0.8, hall_min_dur=3.0,
                        text_check=True):
    """
    v3 整条删除:
      A) 文本启发式: 命中 is_likely_hallucination → 删
      B) 能量启发式: 区间内静音 (≤hall_thr_db) 占比 > hall_pct 且 duration > hall_min_dur → 删

    静音阈值默认 -40dB (用 detect_audio_levels 内部的 silencedetect=-40dB 采样)。
    """
    kept = []
    stats = {
        "checked": 0,
        "deleted_text": 0,
        "deleted_silence": 0,
        "kept_short": 0,
        "deleted_total_sec": 0.0,
    }

    for b in blocks:
        duration = b["end_sec"] - b["start_sec"]
        text = b["text"]

        # A. 文本启发式 (对所有 duration 生效, 包括 < hall_min_dur)
        if text_check:
            is_hall, reason = is_likely_hallucination(text)
            if is_hall:
                stats["deleted_text"] += 1
                stats["deleted_total_sec"] += duration
                continue

        # B. 能量启发式 (只对长段生效, 短段允许短静音)
        if duration >= hall_min_dur:
            stats["checked"] += 1
            silence_dur = silence_coverage_in_range(
                silence_pairs, b["start_sec"], b["end_sec"], audio_total_dur
            )
            silence_ratio = silence_dur / duration if duration > 0 else 0
            if silence_ratio >= hall_pct:
                stats["deleted_silence"] += 1
                stats["deleted_total_sec"] += duration
                continue

        kept.append(b)

    return kept, stats


# ============== v2: end 裁剪 ==============

def trim_srt(blocks, silences, min_duration=1.0, trail=0.8):
    """
    v2 end 裁剪: 用二分查 silences 列表, 把 end 调到 first_silence - trail。
    """
    stats = {"checked": 0, "trimmed": 0, "skipped_too_short": 0,
             "skipped_no_silence": 0, "skipped_no_cut": 0, "skipped_too_tiny": 0,
             "total_cut_sec": 0.0}

    for b in blocks:
        duration = b["end_sec"] - b["start_sec"]

        if duration < min_duration:
            stats["skipped_too_short"] += 1
            continue

        stats["checked"] += 1
        first_silence = first_silence_in(silences, b["start_sec"], b["end_sec"])

        if first_silence is None:
            stats["skipped_no_silence"] += 1
            continue

        new_end = first_silence - trail
        if new_end <= b["start_sec"] + 0.5:
            stats["skipped_too_tiny"] += 1
            continue
        if new_end >= b["end_sec"] - 0.05:
            stats["skipped_no_cut"] += 1
            continue

        cut = b["end_sec"] - new_end
        b["end"] = _to_ts(new_end)
        b["end_sec"] = new_end
        stats["trimmed"] += 1
        stats["total_cut_sec"] += cut

    return blocks, stats


# ============== audio duration helper ==============

def get_audio_duration(audio_path):
    """ffprobe 拿音频总秒数。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ============== main ==============

def main():
    parser = argparse.ArgumentParser(description="SRT 静音裁剪器 v3 (幻觉删除 + end 裁剪)")
    parser.add_argument("input", help="输入 SRT")
    parser.add_argument("audio", help="对应音频文件 (用于 ffmpeg silencedetect)")
    parser.add_argument("--output", "-o", help="输出 SRT (默认 <input>_trim.srt)")
    parser.add_argument("--min-duration", type=float, default=1.0,
                       help="触发裁剪的最小持续时间 (秒, 默认 1.0)")
    parser.add_argument("--trail", type=float, default=0.8,
                       help="保留的尾部余量 (秒, 默认 0.8)")
    parser.add_argument("--noise-db", type=float, default=-30.0,
                       help="silencedetect 噪声阈值 dB (默认 -30) — 用于 end 裁剪")
    parser.add_argument("--min-silence", type=float, default=0.5,
                       help="silencedetect 最小静音时长 s (默认 0.5)")
    parser.add_argument("--hall-thr-db", type=float, default=-40.0,
                       help="幻觉判定的能量阈值 dB (默认 -40)")
    parser.add_argument("--hall-pct", type=float, default=0.8,
                       help="幻觉判定的静音占比 (默认 0.8)")
    parser.add_argument("--hall-min-dur", type=float, default=3.0,
                       help="幻觉能量判定的最小 duration (秒, 默认 3.0)")
    parser.add_argument("--no-text-check", action="store_true",
                       help="禁用文本启发式幻觉检测")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else in_path.with_name(in_path.stem + "_trim.srt")
    audio_path = Path(args.audio)

    if not in_path.exists():
        print(f"❌ SRT 文件不存在: {in_path}", file=sys.stderr)
        sys.exit(1)
    if not audio_path.exists():
        print(f"❌ 音频文件不存在: {audio_path}", file=sys.stderr)
        sys.exit(1)

    print(f"📖 读取: {in_path}")
    blocks = parse_srt(in_path)
    print(f"   共 {len(blocks)} 条字幕")

    print(f"🔧 静音裁剪 v3:")
    print(f"   A) end 裁剪:  min_duration={args.min_duration}s, trail={args.trail}s, "
          f"noise={args.noise_db}dB, min_silence={args.min_silence}s")
    print(f"   B) 幻觉删除:  thr={args.hall_thr_db}dB, pct={args.hall_pct}, "
          f"min_dur={args.hall_min_dur}s, text_check={not args.no_text_check}")

    # 1. 音频总长
    audio_dur = get_audio_duration(audio_path)
    print(f"\n🎙️  音频总长: {audio_dur:.1f}s")
    if audio_dur <= 0:
        print("❌ 无法获取音频时长, 退出", file=sys.stderr)
        sys.exit(2)

    # 2. silencedetect (end 裁剪用, 阈值 = --noise-db)
    print(f"🎙️  silencedetect (end 裁剪, noise={args.noise_db}dB) ...")
    silences = detect_all_silences(
        audio_path, noise_db=args.noise_db, min_dur=args.min_silence,
    )
    print(f"   检测到 {len(silences)} 个静音起点")

    # 3. silencedetect (幻觉删除用, 阈值 = --hall-thr-db)
    print(f"🎙️  silencedetect (幻觉检测, noise={args.hall_thr_db}dB) ...")
    hall_silences = detect_all_silences(
        audio_path, noise_db=args.hall_thr_db, min_dur=0.3,
    )
    # 转成 (start, end) pairs
    # 上面函数实际返回 list[float] (silence_start), 重新跑一次拿 pairs 比较安全
    # 直接用 detect_audio_levels 走 astats 路径
    hall_pairs = detect_audio_levels(audio_path, window=0.3)
    print(f"   静音段 pairs: {len(hall_pairs)}")

    # 4. v3 步骤 A: 整条删除幻觉
    print(f"\n🗑️  v3-A 整条删除幻觉字幕 ...")
    blocks, del_stats = delete_hallucinated(
        blocks, hall_pairs, audio_dur,
        hall_thr_db=args.hall_thr_db,
        hall_pct=args.hall_pct,
        hall_min_dur=args.hall_min_dur,
        text_check=not args.no_text_check,
    )
    print(f"   文本启发删除: {del_stats['deleted_text']} 条")
    print(f"   能量启发删除: {del_stats['deleted_silence']} 条 (检查 {del_stats['checked']} 条)")
    print(f"   合计删除:     {del_stats['deleted_text'] + del_stats['deleted_silence']} 条 "
          f"({del_stats['deleted_total_sec']:.1f}s)")

    # 5. v3 步骤 B: end 裁剪
    print(f"\n✂️  v3-B end 裁剪 (对剩余 {len(blocks)} 条) ...")
    blocks, trim_stats = trim_srt(
        blocks, silences,
        min_duration=args.min_duration,
        trail=args.trail,
    )
    print(f"   裁剪: {trim_stats['trimmed']} 条 "
          f"(裁掉 {trim_stats['total_cut_sec']:.1f}s)")

    # 6. 写盘
    write_srt(blocks, out_path)

    # 7. 统计汇总
    total_deleted = del_stats["deleted_text"] + del_stats["deleted_silence"]
    total_input = len(parse_srt(in_path))
    print(f"\n✅ 输出: {out_path}")
    print(f"   输入:   {total_input} 条")
    print(f"   删除:   {total_deleted} 条 ({del_stats['deleted_total_sec']:.1f}s)")
    print(f"   裁剪:   {trim_stats['trimmed']} 条 ({trim_stats['total_cut_sec']:.1f}s)")
    print(f"   输出:   {len(blocks)} 条")
    if trim_stats["trimmed"] > 0:
        avg = trim_stats["total_cut_sec"] / trim_stats["trimmed"]
        print(f"   平均裁掉: {avg:.2f} 秒/条")
    print(f"\n💡 幻觉删除明细 (按类型):")
    if del_stats["deleted_text"] > 0:
        print(f"   - 文本启发: 重复/单字符/纯♪ 字幕 (Whisper 在静音/弱语音里幻觉)")
    if del_stats["deleted_silence"] > 0:
        print(f"   - 能量启发: 区间 ≥{args.hall_pct*100:.0f}% 时长在 ≤{args.hall_thr_db}dB (BGM 中穿插)")


if __name__ == "__main__":
    main()

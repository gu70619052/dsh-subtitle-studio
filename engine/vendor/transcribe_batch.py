#!/usr/bin/env python3
"""
批量 MLX Whisper 转写器 — 模型只加载一次

原 transcribe_srt.py 每调用一次就重新加载 MLX 模型（~15s），对 100+ 个 1 分钟分片
来说是 25 分钟的纯加载开销。本脚本接受多文件参数，只加载模型一次。

用法：
    python3 transcribe_batch.py chunk_000.wav chunk_001.wav ... \
        --output-dir ./srts --language ja

参数：
    files                   一个或多个音频文件
    --output-dir            SRT 输出目录（默认与输入同目录）
    --language              语言代码（默认自动检测）
    --max-line-ms           单条字幕最大时长毫秒数（默认 6000）
    --pause-ms              词间停顿超过此毫秒数则断句（默认 500）
    --condition-on-previous-text  让转写接上文（默认关闭，防幻觉传染）
    --temperature            温度回退上限（0=只用最可能结果）
    --hallucination-silence-threshold  静音超过这个秒数就认为解码不可靠
    --min-avg-logprob        低于该平均对数概率的片段直接丢弃
    --max-no-speech-prob     高于该无语音概率的片段直接丢弃
    --keep-thanks            保留致谢片尾语
    --keep-repeats           保留复读内容
"""
import argparse
import sys
import os
from pathlib import Path


# 复用原 transcribe_srt.py 的所有工具函数
sys.path.insert(0, str(Path(__file__).parent))
from transcribe_srt import (
    format_timestamp,
    _transcribe_mlx,
    _transcribe_faster,
    merge_words_to_segments,
    _postprocess,
    build_options,
    _filter_low_confidence,
    _clean_hallucinations,
)


def transcribe_one(audio_path: str, output_path: str, language: str,
                   max_line_ms: int, pause_ms: int, options=None) -> bool:
    """转写单个音频文件。返回是否成功。"""
    try:
        # 确保 output_path 的父目录存在（--output-dir 场景）
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        options = options or build_options()

        # 默认用 MLX（Metal GPU）
        segments = _transcribe_mlx(audio_path, language, options)
        if segments is None:
            print(f"  MLX Whisper 失败，尝试 faster-whisper（CPU）...", file=sys.stderr)
            segments = _transcribe_faster(audio_path, "large-v3-turbo", language, options)
        if segments is None:
            print(f"  [FAIL] {audio_path}: 没有可用引擎", file=sys.stderr)
            return False

        # 置信度过滤（丢整段幻觉）→ 切句 → 压复读 / 丢致谢片尾语
        segments = _filter_low_confidence(segments, options)
        srt_segments = merge_words_to_segments(
            segments, max_line_ms=max_line_ms, pause_ms=pause_ms
        )
        srt_segments = _clean_hallucinations(srt_segments, options)
        # 始终创建 output_path（即使空 SRT 也要占位，否则流水线后续合并不认这个 chunk）
        with open(output_path, "w", encoding="utf-8") as f:
            if not srt_segments:
                f.write("")  # 空 SRT 占位
            else:
                for i, seg in enumerate(srt_segments, 1):
                    f.write(f"{i}\n")
                    f.write(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n")
                    f.write(f"{seg['text']}\n\n")
        print(f"完成！共 {len(srt_segments)} 条字幕 → {output_path}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"  [FAIL] {audio_path}: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="批量 MLX Whisper 转写（模型只加载一次）"
    )
    parser.add_argument("files", nargs="+", help="一个或多个音频文件")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="SRT 输出目录（默认与输入同目录）")
    parser.add_argument("--language", "-l", default=None,
                        help="语言代码（默认自动检测）")
    parser.add_argument("--max-line-ms", type=int, default=4000)
    parser.add_argument("--pause-ms", type=int, default=500)
    parser.add_argument("--condition-on-previous-text", action="store_true",
                        help="让转写接上文（默认关闭，防幻觉传染）")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="温度回退上限：0=只用最可能结果")
    parser.add_argument("--hallucination-silence-threshold", type=float, default=None,
                        help="静音超过这个秒数就认为解码不可靠")
    parser.add_argument("--min-avg-logprob", type=float, default=None,
                        help="低于该平均对数概率的片段直接丢弃")
    parser.add_argument("--max-no-speech-prob", type=float, default=None,
                        help="高于该无语音概率的片段直接丢弃")
    parser.add_argument("--keep-thanks", action="store_true",
                        help="保留致谢片尾语")
    parser.add_argument("--keep-repeats", action="store_true",
                        help="保留复读内容")
    args = parser.parse_args()

    options = build_options(
        condition_on_previous_text=bool(args.condition_on_previous_text),
        temperature=float(args.temperature or 0.0),
        hallucination_silence_threshold=args.hallucination_silence_threshold,
        min_avg_logprob=args.min_avg_logprob,
        max_no_speech_prob=args.max_no_speech_prob,
        drop_thanks=not args.keep_thanks,
        collapse_repeats=not args.keep_repeats,
    )

    if not args.files:
        parser.error("至少需要一个音频文件")

    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    # 预加载模型：调用一次 _transcribe_mlx 让 mlx_whisper 内部缓存模型
    # 然后后续 transcribe 会复用。第一次调用会花 ~15s 加载，后续每次 < 1s。
    first = Path(args.files[0])
    print(f"=== 预加载 MLX Whisper 模型（首次需要 ~15s）===", file=sys.stderr)
    print(f"=== 待转写 {len(args.files)} 个文件 ===", file=sys.stderr)

    succeeded = 0
    failed = 0
    for i, f in enumerate(args.files, 1):
        fpath = Path(f)
        if not fpath.exists():
            print(f"  [{i}/{len(args.files)}] [SKIP] {f}: 文件不存在", file=sys.stderr)
            failed += 1
            continue

        srt_name = fpath.stem + ".srt"
        srt_path = (output_dir / srt_name) if output_dir else fpath.with_suffix(".srt")
        print(f"  [{i}/{len(args.files)}] {fpath.name}", file=sys.stderr)
        if transcribe_one(str(fpath), str(srt_path), args.language,
                          args.max_line_ms, args.pause_ms, options):
            succeeded += 1
        else:
            failed += 1

    print(f"\n=== 批量转写完成：成功 {succeeded} / 失败 {failed} / 共 {len(args.files)} ===",
          file=sys.stderr)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
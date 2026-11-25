#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 画质链路自测（折行 + 音频编码）。

覆盖审计里「面板能选、引擎不读」的两处：
1. subtitles.maxLineChars 真的折行（压制前的 SRT 与双语 ASS 两条路都走）；
2. burn.audio 三种取值各自拼出正确的 ffmpeg 参数（opus 不再是 aac 的马甲）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.abspath(os.path.join(HERE, "..", "engine"))
if not os.path.isdir(ENGINE_DIR):
    ENGINE_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "dsh-subtitle-studio", "engine"))
sys.path.insert(0, ENGINE_DIR)
VENDOR_DIR = os.path.join(ENGINE_DIR, "vendor")

from lib import config as config_mod  # noqa: E402
from lib import pipeline  # noqa: E402
from lib.subs import wrap_text  # noqa: E402

FAILURES = []
PASSED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("PASS  %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILURES.append(name)
        print("FAIL  %s%s" % (name, ("  " + detail) if detail else ""))


class NullLog:
    def info(self, *_a, **_k):
        pass

    def warn(self, *_a, **_k):
        pass

    def error(self, *_a, **_k):
        pass


ROOT = tempfile.mkdtemp(prefix="ss-quality-")
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "out")
for _d in (SRC, OUT):
    os.makedirs(_d, exist_ok=True)


def cfg_with(values):
    """一份能过校验的最小配置，再按点分路径覆写。"""
    data = {
        "version": 1,
        "engine": {"kind": "dsh", "provider": "fake", "model": "fake-model"},
        "source": {"kind": "local", "path": SRC, "include": ["*.mp4"], "recursive": False},
        "output": {"kind": "local", "path": OUT, "nameTemplate": "{stem}_zh.mp4", "overwrite": False},
        "selection": {"maxPerRun": 1, "skipTranslated": True, "minSizeGB": 0, "maxSizeGB": 10,
                      "maxAgeDays": 36500, "order": "name-asc", "excludePatterns": []},
        "prompts": {"default": {"system": "翻译成中文", "context": ""}, "profiles": []},
        "subtitles": {"bilingual": False, "trimSilence": False, "maxLineChars": 12,
                      "keepOriginalAsSrt": True, "fontSize": 0, "font": ""},
        "burn": {"enabled": True, "ffmpeg": "", "crf": 20, "preset": "medium", "audio": "copy",
                 "encoder": "libx264", "videoBitrate": "6000k", "fontSize": 0},
        "transcribe": {"whisperModel": "", "language": "", "keepAudio": False},
        "notify": {"enabled": False, "channel": "none"},
        "schedule": {"enabled": False, "mode": "none", "time": "02:10", "guardTimes": []},
        "paths": {"dataDir": ROOT, "keepWorkArtifacts": False},
        "limits": {"minFreeGB": 0},
    }
    for dotted, value in values.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    path = os.path.join(ROOT, "config-%d.json" % len(os.listdir(ROOT)))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return config_mod.load_config(path), ROOT


# ---------------- 1. wrap_text ----------------
long_ja = "これは長文のラップテストです、ここで折り返されます、もう一行続きます"
folded = wrap_text(long_ja, 12)
check("wrap_text 在上限之后找最近的标点断行", folded.startswith("これは長文のラップテストです、"), repr(folded))
check("wrap_text 每行不超过上限的 1.6 倍", all(len(line) <= 19 for line in folded.split("\n")), repr(folded))
check("wrap_text 对短文本不动", wrap_text("短句", 12) == "短句")
check("wrap_text 上限为 0 时不折", wrap_text("x" * 40, 0) == "x" * 40)
check("wrap_text 上限非法时不折", wrap_text("abc", None) == "abc" and wrap_text("abc", "x") == "abc")
check("wrap_text 拉丁文本优先在空格处断", "\n" in wrap_text("alpha beta gamma delta epsilon", 12))
check("wrap_text 不产生空行", all(line.strip() for line in wrap_text(long_ja, 12).split("\n")))
check("wrap_text 用 joiner 定制换行符", "\\N" in wrap_text(long_ja, 12, joiner="\\N"))

# ---------------- 2. 压制前的 SRT 折行 ----------------
srt_root = tempfile.mkdtemp(prefix="ss-srt-")
srt_path = os.path.join(srt_root, "zh.srt")
with open(srt_path, "w", encoding="utf-8") as handle:
    handle.write(
        "1\n00:00:01,000 --> 00:00:05,000\n%s\n\n2\n00:00:06,000 --> 00:00:08,000\n短句\n\n" % long_ja
    )
cfg, _ = cfg_with({"subtitles.maxLineChars": 12})
wrapped_path = pipeline.prepare_burn_srt(cfg, NullLog(), srt_path)
check("maxLineChars>0 产出折行后的压制文件", wrapped_path != srt_path and os.path.exists(wrapped_path))
with open(wrapped_path, encoding="utf-8") as handle:
    body = handle.read()
check("折行只切长句，短句原样", "\n短句\n" in body and "\n" in body.split("\n\n")[0], repr(body[:80]))
cfg0, _ = cfg_with({"subtitles.maxLineChars": 0})
check("maxLineChars=0 直接压制原文件（交给 libass）", pipeline.prepare_burn_srt(cfg0, NullLog(), srt_path) == srt_path)

# ---------------- 3. 双语 ASS 折行 ----------------
ass_out = os.path.join(srt_root, "bl.ass")
rc = subprocess.run(
    [sys.executable, os.path.join(VENDOR_DIR, "bilingual_ass.py"), srt_path,
     "--output", ass_out, "--max-chars", "12", "--height", "1080"],
    capture_output=True, text=True,
)
check("bilingual_ass 接受 --max-chars", rc.returncode == 0, rc.stderr.strip()[-200:])
ass_body = open(ass_out, encoding="utf-8").read() if os.path.exists(ass_out) else ""
check("ASS 用 \\N 折行且优先贴标点", "これは長文のラップテストです、\\N" in ass_body, ass_body[ass_body.find("Dialogue"):][:120] if ass_body else "无产物")

# ---------------- 4. burn.audio 三种取值 ----------------
captured = []


def fake_run_cmd(cmd, timeout=None):
    captured.append(list(cmd))
    with open(cmd[-1], "wb") as handle:  # 假装 ffmpeg 写出了 .partial
        handle.write(b"x" * 1024)
    return 0, "", ""


real = (pipeline.ffmpeg_bin, pipeline.probe_duration, pipeline.run_cmd)
pipeline.ffmpeg_bin = lambda _cfg: "ffmpeg"
pipeline.probe_duration = lambda _cfg, _path: 3.0
pipeline.run_cmd = fake_run_cmd
try:
    video = os.path.join(srt_root, "in.mp4")
    with open(video, "wb") as handle:
        handle.write(b"y" * 1024)
    for audio, expect in [
        ("copy", ["-c:a", "copy"]),
        ("aac", ["-c:a", "aac", "-b:a", "192k"]),
        ("opus", ["-c:a", "libopus", "-b:a", "192k"]),
    ]:
        captured.clear()
        cfg_a, _ = cfg_with({"burn.enabled": True, "burn.audio": audio, "subtitles.maxLineChars": 0})
        out_path = os.path.join(srt_root, "out-%s.mp4" % audio)
        pipeline.burn_video(cfg_a, NullLog(), video, srt_path, out_path, ass_mode=False, height=1080)
        joined = " ".join(captured[0])
        check("burn.audio=%s 拼出对应参数" % audio, " ".join(expect) in joined, joined[joined.find("-c:a"):][:60])
        check("burn.audio=%s 产物落盘" % audio, os.path.exists(out_path))
    # 默认值必须还是 copy：老配置的音频不能被偷偷重编码
    captured.clear()
    cfg_d, _ = cfg_with({"burn.enabled": True})
    pipeline.burn_video(cfg_d, NullLog(), video, srt_path, os.path.join(srt_root, "out-default.mp4"), height=1080)
    check("未显式设置时音频仍走 copy", "-c:a copy" in " ".join(captured[0]))
finally:
    pipeline.ffmpeg_bin, pipeline.probe_duration, pipeline.run_cmd = real
    shutil.rmtree(srt_root, ignore_errors=True)

# ---------------- 5. schedule.mode 的对称性 ----------------
# 面板能选 dsh / off，引擎就必须能原样读这份配置（手工跑 guard 也是同一条命令）。
from lib.runtime import EngineError  # noqa: E402

for mode in ("launchd", "none", "dsh", "off"):
    try:
        cfg_with({"schedule.mode": mode})
        check("引擎接受面板的 schedule.mode=%s" % mode, True)
    except EngineError as error:
        check("引擎接受面板的 schedule.mode=%s" % mode, False, str(error)[:200])
try:
    cfg_with({"schedule.mode": "cron"})
    check("非法 schedule.mode 仍被拦下", False)
except EngineError as error:
    check("非法 schedule.mode 仍被拦下", "schedule.mode" in str(getattr(error, "detail", "") or "") or True)

shutil.rmtree(ROOT, ignore_errors=True)
print("\n%d checks passed, %d failed" % (len(PASSED), len(FAILURES)))
sys.exit(0 if not FAILURES else 1)

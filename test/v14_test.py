#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕工坊 v1.4 引擎自测（需求池 REQ-01 / 02 / 03 / 27 / 42）。

覆盖：
1. REQ-01 音轨起始偏移补偿 —— 用真实 ffmpeg 造一个「音轨晚 3 秒开始」的片子，
   抽音频后确认时间轴被补齐（没 ffmpeg 就跳过这部分）；
2. REQ-02 复读压平 / 致谢片尾语丢弃 / 低置信段丢弃；
3. REQ-03 温度回退阶梯与反幻觉参数透传；
4. REQ-27 样式模板 —— 默认预设的 Style 行与旧版逐字一致，预设可切换；
5. REQ-42 输出方式 —— burn / srt 两个模式、成品名按模板换扩展名、跳过判定同步；
6. 新配置键的校验规则。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# This script ships inside <package>/test/, so the engine is one level up.
ENGINE_DIR = os.path.join(HERE, "..", "engine")
if not os.path.isdir(ENGINE_DIR):
    ENGINE_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "dsh-subtitle-studio", "engine"))
ENGINE_DIR = os.path.abspath(ENGINE_DIR)
sys.path.insert(0, ENGINE_DIR)
sys.path.insert(0, os.path.join(ENGINE_DIR, "vendor"))

from lib import config as config_mod  # noqa: E402
from lib import pipeline  # noqa: E402
from lib import select as selection  # noqa: E402
from lib import style_templates  # noqa: E402
from lib.runtime import Log  # noqa: E402
import transcribe_srt  # noqa: E402
import bilingual_ass  # noqa: E402

FAILURES = []
PASSED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("PASS  %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILURES.append(name)
        print("FAIL  %s%s" % (name, ("  " + detail) if detail else ""))


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class FlatConfig(object):
    """模拟引擎的 Config 对象：get() 走点分路径。"""

    def __init__(self, data):
        self.data = data

    def get(self, path, default=None):
        cur = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def base_config(**overrides):
    data = {
        "version": 1,
        "engine": {"kind": "dsh", "provider": "fake-provider", "model": "fake-model"},
        "source": {"kind": "local", "path": "/tmp", "include": ["*.mp4"], "recursive": False,
                   "ssh": {"host": "", "port": 22, "user": "", "keyPath": ""}},
        "output": {"kind": "local", "path": "/tmp", "nameTemplate": "{stem}_zh.mp4",
                   "overwrite": False, "mode": "burn",
                   "ssh": {"host": "", "port": 22, "user": "", "keyPath": ""}},
        "selection": {"maxPerRun": 2, "skipTranslated": True, "respectSkipList": True,
                      "minSizeGB": 0, "maxSizeGB": 10, "maxAgeDays": 36500,
                      "order": "name-asc", "excludePatterns": []},
        "prompts": {"default": {"system": "s", "context": ""}, "profiles": []},
        "subtitles": {"bilingual": False, "trimSilence": True, "maxLineChars": 12,
                      "keepOriginalAsSrt": True, "fontSize": 0, "font": "",
                      "styleTemplate": "default"},
        "burn": {"enabled": True, "ffmpeg": "", "crf": 20, "preset": "medium",
                 "audio": "copy", "encoder": "libx264", "videoBitrate": "6000k", "fontSize": 0},
        "transcribe": {"whisperModel": "", "language": "ja", "keepAudio": False,
                       "chunkSeconds": 60, "maxLineMs": 4000, "pauseMs": 500,
                       "silenceDb": -50, "engine": "mlx", "conditionOnPreviousText": False,
                       "temperature": 0, "hallucinationSilenceThreshold": 0,
                       "minAvgLogprob": 0, "maxNoSpeechProb": 0,
                       "keepThanksPhrases": False, "keepRepeats": False},
        "translation": {"rerunThresholdPct": 5, "rerunThresholdCount": 50},
        "notify": {"enabled": True, "channel": "none", "qqAppId": "", "qqOpenId": "",
                   "qqSecretService": "s", "qqSendPath": "", "command": "", "webhookUrl": "",
                   "webhookBody": "", "onStart": False, "onSuccess": True, "onFailure": True},
        "schedule": {"enabled": False, "mode": "launchd", "time": "02:10",
                     "guardTimes": [], "label": "com.subtitle-studio.run"},
        "paths": {"dataDir": "~/.dsh/data/subtitle-studio", "keepWorkArtifacts": False},
        "limits": {"minFreeGB": 15},
    }
    for key, value in overrides.items():
        data[key] = value
    return data


# ---------------------------------------------------------------- REQ-27
def test_style_templates():
    legacy = ("Style: Default,PingFang SC,20,&H00FFFFFF,&H000000FF,&H64000000,&H00000000,"
              "1,0,0,0,100,100,0,0,1,1.2,0,2,20,20,16,1")
    got = style_templates.ass_style_line(style_templates.get_preset("default"),
                                         font="PingFang SC", cn_size=20)
    check("REQ-27 the default preset reproduces the legacy style line byte for byte",
          got == legacy, got)

    ids = style_templates.style_ids()
    check("REQ-27 five presets are available",
          set(ids) == {"default", "bold-yellow", "top-white", "boxed", "cinema"}, str(ids))

    unknown = style_templates.get_preset("no-such-style")
    check("REQ-27 an unknown preset falls back to the default one",
          unknown is style_templates.get_preset("default"))
    check("REQ-27 an unknown id never raises",
          style_templates.get_preset(None) is not None)

    fields = ("primary", "secondary", "outline_colour", "back", "bold", "outline",
              "shadow", "border_style", "alignment", "margin_l", "margin_r", "margin_v")
    missing = [pid for pid in ids
               if any(field not in style_templates.get_preset(pid) for field in fields)]
    check("REQ-27 every preset carries the full field set", not missing, str(missing))

    style = style_templates.force_style(style_templates.get_preset("bold-yellow"),
                                        font="PingFang SC", font_size=24)
    check("REQ-27 force_style carries the colours of the preset",
          "PrimaryColour=&H0000FFFF" in style and "FontSize=24" in style, style)
    check("REQ-27 force_style values contain no comma or colon that libass would choke on",
          ":" not in style.replace("FontName=PingFang SC", ""), style)

    top = style_templates.ass_style_line(style_templates.get_preset("top-white"))
    check("REQ-27 the top preset anchors the subtitles to the top",
          ",8," in top and top.rstrip().endswith(",48,1"), top)


# ---------------------------------------------------------------- REQ-42
def test_output_mode():
    cfg = base_config()
    cfg["output"]["mode"] = "srt"
    check("REQ-42 an explicit srt mode wins",
          pipeline.output_mode(FlatConfig(cfg)) == "srt")

    cfg["output"]["mode"] = "burn"
    check("REQ-42 an explicit burn mode wins",
          pipeline.output_mode(FlatConfig(cfg)) == "burn")

    cfg["output"]["mode"] = ""
    cfg["burn"]["enabled"] = False
    check("REQ-42 an empty mode falls back to burn.enabled=false",
          pipeline.output_mode(FlatConfig(cfg)) == "srt")
    cfg["burn"]["enabled"] = True
    check("REQ-42 an empty mode falls back to burn.enabled=true",
          pipeline.output_mode(FlatConfig(cfg)) == "burn")


def test_output_naming():
    check("REQ-42 the video name keeps the template extension",
          selection.output_name("{stem}_zh.mp4", "第01话.mp4") == "第01话_zh.mp4")
    check("REQ-42 the subtitle name swaps the extension",
          selection.output_name("{stem}_zh.mp4", "第01话.mp4", ext="srt") == "第01话_zh.srt")
    check("REQ-42 a dotted template swaps the extension as well",
          selection.output_name("{stem}.zh.mp4", "第01话.mp4", ext="srt") == "第01话.zh.srt")
    check("REQ-42 a broken template falls back to the same extension",
          selection.output_name("{nope}", "第01话.mp4", ext="srt") == "第01话_zh.srt")


def test_select_uses_srt_extension():
    tmp = tempfile.mkdtemp(prefix="subtitle-v14-select-")
    try:
        os.makedirs(os.path.join(tmp, "out"), exist_ok=True)
        with open(os.path.join(tmp, "out", "movie_zh.srt"), "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        entries = [{"name": "movie.mp4", "path": os.path.join(tmp, "movie.mp4"),
                    "size": 10, "mtime": 1.0, "mtimeISO": "2026-01-01T00:00:00",
                    "ageDays": 0}]
        cfg = base_config()
        cfg["selection"].update({"include": ["*.mp4"], "recursive": False,
                                  "skipTranslated": True, "respectSkipList": True,
                                  "minSizeGB": 0, "maxSizeGB": 10, "maxAgeDays": 36500,
                                  "order": "name-asc", "excludePatterns": [],
                                  "maxPerRun": 5})
        cfg["output"]["nameTemplate"] = "{stem}_zh.mp4"
        picked = selection.select(
            entries, FlatConfig(cfg), existing_outputs={"movie_zh.srt"},
            name_template="{stem}_zh.mp4", output_ext="srt")
        candidates = picked["candidates"]
        reasons = [c.get("reason") for c in candidates]
        check("REQ-42 an existing .srt is recognised as a finished translation",
              "translated-exists" in reasons, str(reasons))
        check("REQ-42 the expected output name follows the srt extension",
              candidates and candidates[0].get("expectedOutput") == "movie_zh.srt",
              str(candidates[0].get("expectedOutput") if candidates else None))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- REQ-02 / 03
def test_asr_helpers():
    check("REQ-02 a Japanese end-card line is recognised",
          transcribe_srt._is_thanks_text("ご視聴ありがとうございました"))
    check("REQ-02 an English end-card line is recognised",
          transcribe_srt._is_thanks_text("Thanks for watching"))
    check("REQ-02 a Chinese end-card line is recognised",
          transcribe_srt._is_thanks_text("请不吝点赞订阅"))
    check("REQ-02 ordinary dialogue is not treated as an end card",
          not transcribe_srt._is_thanks_text("大家好，今天是正式演出"))
    check("REQ-02 a long sentence mentioning thanks keeps its text",
          not transcribe_srt._is_thanks_text(
              "这段话很长，里面提到了一次感谢，但是正文不能被丢掉，所以要保留下来继续往后走。"))

    check("REQ-02 a repeated phrase is squeezed into one",
          transcribe_srt._collapse_repeat_text("ありがとうありがとうありがとう") == "ありがとう")
    check("REQ-02 a repeated word is squeezed into one",
          transcribe_srt._collapse_repeat_text("spring spring spring") == "spring")
    check("REQ-02 a twice repeated phrase is left alone",
          transcribe_srt._collapse_repeat_text("very very good") == "very very good")
    check("REQ-02 short text is left alone",
          transcribe_srt._collapse_repeat_text("好啊") == "好啊")

    class Seg:
        def __init__(self, text, start, end, avg_logprob=None, no_speech_prob=None):
            self.text = text
            self.start = start
            self.end = end
            self.avg_logprob = avg_logprob
            self.no_speech_prob = no_speech_prob

    segments = [Seg("keep me", 0.0, 1.0, -0.2, 0.1),
                Seg("too unsure", 1.0, 2.0, -1.8, 0.2),
                Seg("mostly noise", 2.0, 3.0, -0.3, 0.95)]
    kept = transcribe_srt._filter_low_confidence(
        segments, transcribe_srt.build_options(min_avg_logprob=-1.0))
    check("REQ-03 a segment below the confidence floor is dropped",
          [s.text for s in kept] == ["keep me", "mostly noise"], str([s.text for s in kept]))
    kept = transcribe_srt._filter_low_confidence(
        segments, transcribe_srt.build_options(max_no_speech_prob=0.5))
    check("REQ-03 a segment that is mostly silence is dropped",
          [s.text for s in kept] == ["keep me", "too unsure"], str([s.text for s in kept]))
    kept = transcribe_srt._filter_low_confidence(segments, transcribe_srt.build_options())
    check("REQ-03 with the filters off nothing is dropped", len(kept) == 3)

    cleaned = transcribe_srt._clean_hallucinations(
        [{"text": "ご視聴ありがとうございました", "start": 0.0, "end": 1.0},
         {"text": "こんにちは", "start": 1.0, "end": 2.0},
         {"text": "こんにちは", "start": 2.0, "end": 3.0}],
        transcribe_srt.build_options())
    check("REQ-02 the end card is dropped and the duplicate line is merged",
          [item["text"] for item in cleaned] == ["こんにちは"], str(cleaned))
    check("REQ-02 the merged line keeps the longer end time",
          cleaned and cleaned[0]["end"] == 3.0)

    kept = transcribe_srt._clean_hallucinations(
        [{"text": "ご視聴ありがとうございました", "start": 0.0, "end": 1.0}],
        transcribe_srt.build_options(drop_thanks=False))
    check("REQ-02 the end card can be kept on request",
          len(kept) == 1)


def test_temperature_ladder():
    check("REQ-03 a temperature of zero means one deterministic pass",
          transcribe_srt._temperature_ladder(0) == 0.0)
    check("REQ-03 a temperature of zero means one deterministic pass (float)",
          transcribe_srt._temperature_ladder(0.0) == 0.0)
    ladder = transcribe_srt._temperature_ladder(0.6)
    check("REQ-03 a positive temperature builds an increasing ladder",
          list(ladder) == [0.0, 0.2, 0.4, 0.6], str(ladder))
    ladder = transcribe_srt._temperature_ladder(1.0)
    check("REQ-03 the ladder never exceeds the ceiling",
          list(ladder) == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], str(ladder))


def test_transcribe_command_arguments():
    """管道把配置里的反幻觉参数透传给批量转写脚本。"""
    captured = {}

    class FakeLog:
        def info(self, message):
            pass

        def warn(self, message):
            pass

        def stage(self, message):
            pass

        def error(self, message):
            pass

    real_run = pipeline.run_cmd
    real_probe = pipeline.probe_duration

    def fake_run(cmd, timeout=None, env=None):
        captured["cmd"] = list(cmd)
        return 0, "", ""

    def fake_probe(cfg, path):
        return 12.0

    pipeline.run_cmd = fake_run
    pipeline.probe_duration = fake_probe
    try:
        tmp = tempfile.mkdtemp(prefix="subtitle-v14-cmd-")
        try:
            cfg = base_config()
            cfg["transcribe"].update({
                "conditionOnPreviousText": True,
                "temperature": 0.6,
                "hallucinationSilenceThreshold": 2,
                "minAvgLogprob": -1.2,
                "maxNoSpeechProb": 0.6,
                "keepThanksPhrases": True,
                "keepRepeats": True,
            })
            chunk = os.path.join(tmp, "chunk_0001.wav")
            pipeline.transcribe_chunks(FlatConfig(cfg), FakeLog(), [chunk])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        pipeline.run_cmd = real_run
        pipeline.probe_duration = real_probe

    cmd = captured.get("cmd") or []
    joined = " ".join(cmd)
    check("REQ-03 the pipeline asks ASR to carry the previous text",
          "--condition-on-previous-text" in joined, joined)
    check("REQ-03 the pipeline passes the temperature ceiling",
          "--temperature 0.6" in joined, joined)
    check("REQ-03 the pipeline passes the hallucination silence threshold",
          "--hallucination-silence-threshold 2.0" in joined, joined)
    check("REQ-03 the pipeline passes the confidence floor",
          "--min-avg-logprob -1.2" in joined, joined)
    check("REQ-03 the pipeline passes the no-speech ceiling",
          "--max-no-speech-prob 0.6" in joined, joined)
    check("REQ-02 the pipeline can keep the end-card lines",
          "--keep-thanks" in joined, joined)
    check("REQ-02 the pipeline can keep the repeated lines",
          "--keep-repeats" in joined, joined)

    # 反方向：默认配置（全关）时一个参数都不该出现——否则「0 = 关闭」的语义就废了
    captured.clear()
    real_run = pipeline.run_cmd
    real_probe = pipeline.probe_duration

    def fake_run2(cmd, timeout=None, env=None):
        captured["cmd"] = list(cmd)
        return 0, "", ""

    pipeline.run_cmd = fake_run2
    pipeline.probe_duration = lambda cfg, path: 12.0
    try:
        tmp = tempfile.mkdtemp(prefix="subtitle-v14-cmd-off-")
        try:
            pipeline.transcribe_chunks(FlatConfig(base_config()), FakeLog(),
                                       [os.path.join(tmp, "chunk_0001.wav")])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        pipeline.run_cmd = real_run
        pipeline.probe_duration = real_probe

    off = " ".join(captured.get("cmd") or [])
    for flag in ("--condition-on-previous-text", "--temperature",
                 "--hallucination-silence-threshold", "--min-avg-logprob",
                 "--max-no-speech-prob", "--keep-thanks", "--keep-repeats"):
        check("REQ-03 a default config passes no %s" % flag, flag not in off, off)


# ---------------------------------------------------------------- REQ-01
def test_timeline_alignment():
    if not have_ffmpeg():
        print("SKIP  REQ-01 timeline alignment (no ffmpeg on this machine)")
        return
    tmp = tempfile.mkdtemp(prefix="subtitle-v14-align-")
    try:
        video = os.path.join(tmp, "delayed.mp4")
        # 画面 12 秒，音轨从第 3 秒才开始（容器级延迟）
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:r=12:d=12",
             "-f", "lavfi", "-itsoffset", "3", "-i", "sine=frequency=440:duration=9",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-t", "12", video],
            check=True, capture_output=True)
        cfg = base_config()
        cfg["source"]["path"] = tmp
        cfg["output"]["path"] = tmp
        log = Log()
        wav = os.path.join(tmp, "audio.wav")
        pipeline.extract_audio(FlatConfig(cfg), log, video, wav)
        before = pipeline.probe_duration(FlatConfig(cfg), wav)
        timing = pipeline.probe_stream_timing(FlatConfig(cfg), video)
        check("REQ-01 the probe sees the delayed audio track",
              timing is not None and (timing.get("audioStart") or 0) > 2.0, str(timing))
        check("REQ-01 ffmpeg drops the leading gap when extracting",
              before is not None and before < 11.0, str(before))
        shifted = pipeline.align_audio_timeline(FlatConfig(cfg), log, video, wav)
        after = pipeline.probe_duration(FlatConfig(cfg), wav)
        check("REQ-01 the gap is padded back in",
              shifted and after is not None and after > before + 2.0,
              "before=%s after=%s shift=%s" % (before, after, shifted))
        check("REQ-01 the padded audio now matches the picture length",
              after is not None and after >= 11.5, str(after))

        # 再跑一次不应该继续加静音（幂等）
        again = pipeline.align_audio_timeline(FlatConfig(cfg), log, video, wav)
        after2 = pipeline.probe_duration(FlatConfig(cfg), wav)
        check("REQ-01 aligning twice does not pad twice",
              not again and after2 is not None and abs(after2 - after) < 0.2,
              "after=%s after2=%s" % (after, after2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 配置校验
def test_config_validation():
    data = base_config()
    data["output"]["mode"] = "nope"
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("REQ-42 an unknown output mode is refused",
          any(i.get("path") == "output.mode" and i.get("level") == "error" for i in issues),
          str(issues))

    data = base_config()
    data["subtitles"]["styleTemplate"] = "nope"
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("REQ-27 an unknown style template is refused",
          any(i.get("path") == "subtitles.styleTemplate" and i.get("level") == "error"
              for i in issues), str(issues))

    data = base_config()
    data["transcribe"]["temperature"] = 5
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("REQ-03 a temperature above 2 is refused",
          any(i.get("path") == "transcribe.temperature" and i.get("level") == "error"
              for i in issues), str(issues))

    data = base_config()
    data["transcribe"]["minAvgLogprob"] = 0.5
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("REQ-03 a positive confidence floor is refused",
          any(i.get("path") == "transcribe.minAvgLogprob" and i.get("level") == "error"
              for i in issues), str(issues))

    data = base_config()
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("REQ-42 a valid configuration passes", not issues, str(issues))

    # ssh 的 host / user 会被拼成 `user@host` 交给命令行：以 - 开头的值就成了选项，
    # 等于配置文件里一个字段能起进程。字体名同理会切坏 ffmpeg 滤镜串。
    data = base_config()
    data["source"]["kind"] = "ssh"
    data["source"]["ssh"] = {"host": "-oProxyCommand=touch /tmp/pwned", "user": "me"}
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("被 ssh 当成选项的 host 直接报错",
          any(i.get("path", "").endswith("source.ssh.host") and i.get("level") == "error"
              for i in issues), str(issues))

    data = base_config()
    data["source"]["kind"] = "ssh"
    data["source"]["ssh"] = {"host": "media.example", "user": "-F /tmp/evil"}
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("被 ssh 当成选项的 user 直接报错",
          any(i.get("path", "").endswith("source.ssh.user") and i.get("level") == "error"
              for i in issues), str(issues))

    data = base_config()
    data["subtitles"]["font"] = "Arial':force_style"
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("含引号的字体名报错（否则切坏 force_style）",
          any(i.get("path") == "subtitles.font" and i.get("level") == "error"
              for i in issues), str(issues))

    data = base_config()
    data["source"]["kind"] = "ssh"
    data["source"]["ssh"] = {"host": "media.example", "user": "me", "port": 22}
    data["output"]["kind"] = "ssh"
    data["output"]["ssh"] = {"host": "media.example", "user": "me", "port": 22}
    data["subtitles"]["font"] = "PingFang SC"
    issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))
    check("合法 host / user / 字体不误伤",
          not [i for i in issues if i.get("level") == "error"], str(issues))


# ---------------------------------------------------------------- ASS 输出
def test_ass_style_switch():
    tmp = tempfile.mkdtemp(prefix="subtitle-v14-ass-")
    try:
        srt = os.path.join(tmp, "bi.srt")
        with open(srt, "w", encoding="utf-8") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\n大家好\nhello everyone\n")
        # 五种预设都过一遍 ASS 写入器：只测三种的话，另外两种写坏了也不会有人发现
        for style_id, marker in (("default", "&H00FFFFFF"), ("bold-yellow", "&H0000FFFF"),
                                 ("top-white", ",8,"), ("boxed", ",3,0,0,2,"),
                                 ("cinema", "&H80000000")):
            out = os.path.join(tmp, "out-%s.ass" % style_id)
            proc = subprocess.run(
                [sys.executable, os.path.join(ENGINE_DIR, "vendor", "bilingual_ass.py"),
                 srt, "--output", out, "--style", style_id],
                capture_output=True, text=True)
            if proc.returncode != 0:
                check("REQ-27 the ASS writer accepts the %s preset" % style_id, False,
                      proc.stderr[-200:])
                continue
            with open(out, encoding="utf-8") as handle:
                content = handle.read()
            style_line = [line for line in content.splitlines() if line.startswith("Style:")]
            check("REQ-27 the %s preset reaches the ASS style line" % style_id,
                  bool(style_line) and marker in style_line[0],
                  style_line[0] if style_line else "no style line")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- REQ-42 双语交付
def test_srt_mode_delivers_bilingual():
    """勾了双语 + 只生成字幕文件：成品必须是双语那份，不能把双语文件留在工作目录里。"""
    tmp = tempfile.mkdtemp(prefix="v14-bi-")
    saved = {}
    names = ["prefilter", "translate", "retry_translate", "quality_gate",
             "trim_silence", "probe_height"]
    try:
        src_dir = os.path.join(tmp, "src")
        out_dir = os.path.join(tmp, "out")
        os.makedirs(src_dir)
        os.makedirs(out_dir)
        video = os.path.join(src_dir, "ep01.mp4")
        open(video, "wb").write(b"x" * 2048)

        data = base_config(
            source=dict(base_config()["source"], path=src_dir),
            output=dict(base_config()["output"], path=out_dir, mode="srt",
                        nameTemplate="{stem}_zh.mp4", overwrite=True),
            subtitles=dict(base_config()["subtitles"], bilingual=True,
                           keepOriginalAsSrt=True),
            paths={"dataDir": os.path.join(tmp, "data"), "keepWorkArtifacts": True},
        )
        os.makedirs(os.path.join(tmp, "data", "work"), exist_ok=True)
        cfg = FlatConfig(data)
        cfg.path = os.path.join(tmp, "config.json")
        cfg.data_dir = os.path.join(tmp, "data")
        cfg.work_dir = os.path.join(tmp, "data", "work")
        cfg.logs_dir = os.path.join(tmp, "data", "logs")
        cfg.state_dir = os.path.join(tmp, "data", "state")
        for folder in (cfg.logs_dir, cfg.state_dir):
            os.makedirs(folder, exist_ok=True)

        work_dir = pipeline.work_dir_for(cfg, "ep01.mp4", "")
        os.makedirs(work_dir, exist_ok=True)
        # 预置 source.srt：process_one 见到它就跳过抽音频/转写整段
        open(os.path.join(work_dir, "source.srt"), "w", encoding="utf-8").write(
            "1\n00:00:02,000 --> 00:00:03,000\nこんにちは\n\n")

        def fake_prefilter(_cfg, _log, _srt):
            return os.path.join(work_dir, "source.srt.prefilter")

        def fake_translate(_cfg, _log, _pre, zh_path, _prompt):
            open(zh_path, "w", encoding="utf-8").write(
                "1\n00:00:02,000 --> 00:00:03,000\n你好\n\n")
            return zh_path

        for name, value in [("prefilter", fake_prefilter), ("translate", fake_translate),
                           ("retry_translate", lambda *a, **k: None),
                           ("quality_gate", lambda *a, **k: {"blocks": 1, "untranslated": 0,
                                                             "untranslatedPct": 0.0}),
                           ("trim_silence", lambda *a, **k: None),
                           ("probe_height", lambda *a, **k: 720)]:
            saved[name] = getattr(pipeline, name)
            setattr(pipeline, name, value)

        log = Log()
        pipe = pipeline.Pipeline(cfg, log)
        candidate = {"name": "ep01.mp4", "path": video, "sizeBytes": 2048,
                     "dirIndex": 0, "dirName": "d", "selected": True}
        result = pipe.process_one(candidate)

        check("REQ-42 srt mode with bilingual finishes ok",
              result["status"] == "ok", "%s %s" % (result["status"], result.get("message")))
        produced = os.path.join(out_dir, "ep01_zh.srt")
        check("REQ-42 srt mode writes the template name with an srt extension",
              os.path.exists(produced), str(sorted(os.listdir(out_dir))))
        body = open(produced, encoding="utf-8").read() if os.path.exists(produced) else ""
        check("REQ-42 the delivered file carries the translation", "你好" in body, body.strip()[:80])
        check("REQ-42 the delivered file carries the original line too",
              "こんにちは" in body, body.strip()[:80])
        check("REQ-42 no burned video is produced in srt mode",
              not [n for n in os.listdir(out_dir) if n.endswith(".mp4")],
              str(sorted(os.listdir(out_dir))))
    finally:
        for name, value in saved.items():
            setattr(pipeline, name, value)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- REQ-42 预演一致
def test_dry_run_follows_output_mode():
    """手写配置里 mode 与 burn.enabled 打架时，预演必须跟真实执行站在同一边。"""
    tmp = tempfile.mkdtemp(prefix="subtitle-v14-dry-")
    try:
        data = base_config(output=dict(base_config()["output"], mode="srt"),
                           burn=dict(base_config()["burn"], enabled=True))
        cfg = FlatConfig(data)
        cfg.path = os.path.join(tmp, "config.json")
        cfg.work_dir = tmp
        cfg.data_dir = tmp
        testee = {"name": "ep01.mp4", "sizeGB": 1.0, "expectedOutput": "ep01_zh.srt",
                  "dirIndex": 0, "dirName": "d", "path": os.path.join(tmp, "ep01.mp4")}
        plan = {"source": {"kind": "local"}, "output": {}, "selection": {}, "quota": {},
                "candidates": [], "selected": ["ep01.mp4"], "selectedDetail": [testee],
                "dirs": []}
        out = pipeline.dry_run_plan(cfg, Log(), plan)
        stages = out["actions"][0]["stages"]
        check("REQ-42 a hand-written mode=srt wins over burn.enabled in the preview",
              "srt-only" in stages and "burn" not in stages, str(stages))

        data["output"]["mode"] = "burn"
        del data["burn"]["enabled"]
        again = FlatConfig(data)
        again.path = cfg.path
        again.work_dir = tmp
        again.data_dir = tmp
        out = pipeline.dry_run_plan(again, Log(), plan)
        stages = out["actions"][0]["stages"]
        check("REQ-42 an explicit burn mode shows a burn stage",
              "burn" in stages and "srt-only" not in stages, str(stages))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_style_templates()
    test_output_mode()
    test_output_naming()
    test_select_uses_srt_extension()
    test_asr_helpers()
    test_temperature_ladder()
    test_transcribe_command_arguments()
    test_timeline_alignment()
    test_config_validation()
    test_ass_style_switch()
    test_srt_mode_delivers_bilingual()
    test_dry_run_follows_output_mode()
    print("")
    print("%d/%d checks passed" % (len(PASSED), len(PASSED) + len(FAILURES)))
    if FAILURES:
        print("failed: %s" % ", ".join(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

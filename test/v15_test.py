#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕工坊 v1.5 引擎自测（需求池 REQ-49 / 50 / 51）。

覆盖：
1. REQ-51 拿到多少算多少 —— 整批返回只带前几条时，按时间戳收下能对的，
   缺的拆小批重问，实在拿不到才留原文并记明原因；
2. REQ-50 截断分类 —— token-limit / empty-answer 是确定性失败，不再同参重试；
3. 自救统计与 .report.json 的形状（上层与通知要读它）；
4. REQ-49 思考档位的配置校验，以及只在下发非空档位时才进命令行。
"""

import io
import json
import os
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(HERE, "..", "engine")
if not os.path.isdir(ENGINE_DIR):
    ENGINE_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "dsh-subtitle-studio", "engine"))
ENGINE_DIR = os.path.abspath(ENGINE_DIR)
sys.path.insert(0, ENGINE_DIR)
sys.path.insert(0, os.path.join(ENGINE_DIR, "vendor"))

from lib import config as config_mod  # noqa: E402
from lib import pipeline  # noqa: E402
import translate_srt  # noqa: E402

FAILURES = []
PASSED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("PASS  %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILURES.append(name)
        print("FAIL  %s%s" % (name, ("  " + detail) if detail else ""))


def block(idx, second):
    return {
        "idx": idx,
        "start": "00:00:%02d,000" % second,
        "end": "00:00:%02d,800" % second,
        "text": "原文 %d" % idx,
    }


def srt_of(items):
    """拼 SRT 文本。模型总会写序号，所以这里默认补一个，避免测到解析器的边角。"""
    lines = []
    for position, item in enumerate(items, start=1):
        number = item.get("idx")
        number = position if number in (None, "") else number
        lines.append("%s\n%s --> %s\n%s" % (number, item["start"], item["end"], item["text"]))
    return "\n\n".join(lines) + "\n"


class NullLog(object):
    """pipeline 的 log 只要能被调用就够。"""

    def info(self, *args, **kwargs):
        pass

    def warn(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def stage(self, *args, **kwargs):
        pass


class FakeBridge(object):
    """按调用次数回放的假中转：每次给 (srt_text, meta)。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, batch, key, model, bilingual, video_context, engine="minimax", provider=""):
        self.calls.append([item["idx"] for item in batch])
        if not self.replies:
            return None, {"fatal": True, "code": "empty-answer"}
        return self.replies.pop(0)


def reset_stats():
    translate_srt.RUN_STATS.update({
        "primary": 0, "split": 0, "truncated": 0, "failed": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0}, "untranslated": [],
    })


def run_resilient(batch, replies):
    reset_stats()
    original = translate_srt.translate_batch
    translate_srt.translate_batch = FakeBridge(replies)
    try:
        got = translate_srt.translate_batch_resilient(batch, "k", "m", False, None, engine="dsh", provider="p")
    finally:
        translate_srt.translate_batch = original
    return got, json.loads(json.dumps(translate_srt.RUN_STATS))


def case_match_by_timestamp():
    batch = [block(1, 1), block(2, 2), block(3, 3)]
    # 模型打乱顺序、少写一条，还夹了双语的空行：只有时间戳能对上。
    text = srt_of([
        {"idx": 9, "start": batch[2]["start"], "end": batch[2]["end"], "text": "第三条\n\nThird"},
        {"idx": 8, "start": batch[0]["start"], "end": batch[0]["end"], "text": "第一条\n\nFirst"},
    ])
    got = translate_srt._match_translated(batch, text)
    check("REQ-51 按时间戳反查，忽略模型自己的编号", got.get(1) == "第一条" and got.get(3) == "第三条", json.dumps(got, ensure_ascii=False))
    check("REQ-51 没写出来的块不会被安到别的时间轴上", 2 not in got, json.dumps(got, ensure_ascii=False))
    check("REQ-51 双语输出只留中文", all("\n\n" not in value for value in got.values()), json.dumps(got, ensure_ascii=False))


def case_truncated_batch_salvages_and_splits():
    batch = [block(i, i) for i in range(1, 5)]
    partial = srt_of([{"start": batch[0]["start"], "end": batch[0]["end"], "text": "一"}])
    first_half = srt_of([
        {"start": batch[1]["start"], "end": batch[1]["end"], "text": "二"},
        {"start": batch[2]["start"], "end": batch[2]["end"], "text": "三"},
    ])
    second_half = srt_of([{"start": batch[3]["start"], "end": batch[3]["end"], "text": "四"}])
    got, stats = run_resilient(batch, [
        (partial, {"total_tokens": 40, "reasoning_tokens": 30, "truncated": True, "fatal": False, "code": "token-limit"}),
        (first_half, {"total_tokens": 50, "reasoning_tokens": 0, "truncated": False, "fatal": False, "code": ""}),
        (second_half, {"total_tokens": 50, "reasoning_tokens": 0, "truncated": False, "fatal": False, "code": ""}),
    ])
    check("REQ-51 截断时先收下能对上的那几条", got.get(1) == "一", json.dumps(got, ensure_ascii=False))
    check("REQ-51 缺的块重问后被补齐", got.get(2) == "二" and got.get(3) == "三" and got.get(4) == "四", json.dumps(got, ensure_ascii=False))
    check("REQ-51 重问只问缺的，不重问整批", stats["split"] == 1, json.dumps(stats["calls"] if "calls" in stats else stats))
    check("REQ-51 截断批次被记了数", stats["truncated"] == 1, str(stats["truncated"]))
    check("REQ-51 全部拿到时未译清单为空", stats["untranslated"] == [], json.dumps(stats["untranslated"], ensure_ascii=False))
    check("REQ-50 思考 token 被单独记账", stats["tokens"]["reasoning"] == 30, json.dumps(stats["tokens"]))


def case_single_missing_is_retried_alone():
    batch = [block(1, 1), block(2, 2)]
    only_first = srt_of([{"start": batch[0]["start"], "end": batch[0]["end"], "text": "一"}])
    second = srt_of([{"start": batch[1]["start"], "end": batch[1]["end"], "text": "二"}])
    got, stats = run_resilient(batch, [
        (only_first, {"fatal": False, "code": ""}),
        (second, {"fatal": False, "code": ""}),
    ])
    check("REQ-51 只缺一条时单独重问它", got.get(2) == "二" and stats["split"] == 1, json.dumps(got, ensure_ascii=False))


def case_fatal_is_not_retried():
    batch = [block(1, 1), block(2, 2)]
    got, stats = run_resilient(batch, [
        (None, {"fatal": True, "code": "empty-answer"}),
        (srt_of([{"start": batch[1]["start"], "end": batch[1]["end"], "text": "不该被问"}]),
         {"fatal": False, "code": ""}),
    ])
    check("REQ-50 确定性失败不再同参重试", stats["split"] == 0, str(stats["split"]))
    check("REQ-51 确定性失败时保留原文", got.get(1) == "原文 1" and got.get(2) == "原文 2", json.dumps(got, ensure_ascii=False))
    check(
        "REQ-51 未译清单写明是哪条、为什么",
        len(stats["untranslated"]) == 2
        and stats["untranslated"][0]["idx"] == 1
        and stats["untranslated"][0]["reason"] == "empty-answer"
        and stats["untranslated"][0]["start"] == "00:00:01,000",
        json.dumps(stats["untranslated"], ensure_ascii=False),
    )
    check("REQ-51 失败批被记了数", stats["failed"] == 1, str(stats["failed"]))


def case_depth_and_call_caps():
    batch = [block(i, i) for i in range(1, 9)]
    replies = []
    for _ in range(30):
        replies.append((srt_of([{"start": batch[0]["start"], "end": batch[0]["end"], "text": "只给第一条"}]),
                        {"fatal": False, "code": ""}))
    got, stats = run_resilient(batch, replies)
    check(
        "REQ-51 拆分深度与总次数都有上限（不会无限重问）",
        stats["split"] <= translate_srt.SPLIT_MAX_CALLS and len(got) == len(batch),
        "split=%d" % stats["split"],
    )
    check(
        "REQ-51 拿不到的块显式记未译，不静默丢",
        len(stats["untranslated"]) >= 1 and all(item["reason"] for item in stats["untranslated"]),
        json.dumps(stats["untranslated"][:3], ensure_ascii=False),
    )


def case_report_merge():
    reset_stats()
    with tempfile.TemporaryDirectory() as tmp:
        zh = os.path.join(tmp, "ep01.zh.srt")
        io.open(zh, "w", encoding="utf-8").write(
            "1\n00:00:01,000 --> 00:00:02,000\n你好\n\n2\n00:00:02,000 --> 00:00:03,000\n原文 2\n")
        io.open(zh + ".report.json", "w", encoding="utf-8").write(json.dumps({
            "blocks": 2,
            "untranslated": 1,
            "untranslatedIds": [{"idx": 2, "start": "00:00:02,000", "reason": "token-limit"}],
            "calls": {"primary": 1, "split": 2},
            "truncatedBatches": 1,
            "failedBatches": 0,
            "tokens": {"input": 900, "output": 400, "reasoning": 120},
            "reasoningEffort": "low",
        }, ensure_ascii=False))
        stats = pipeline.translation_stats(zh)
        check(
            "REQ-51 运行结果带上未译明细",
            stats.get("untranslatedIds") == ["#2@00:00:02,000(token-limit)"],
            json.dumps(stats, ensure_ascii=False)[:200],
        )
        check(
            "REQ-50 运行结果带上截断批次数与思考 token",
            stats.get("truncatedBatches") == 1 and stats.get("tokens", {}).get("reasoning") == 120,
            json.dumps(stats, ensure_ascii=False)[:200],
        )
        check(
            "REQ-49 运行结果带上本轮的思考档位",
            stats.get("reasoningEffort") == "low",
            json.dumps(stats, ensure_ascii=False)[:200],
        )
        os.unlink(zh + ".report.json")
        bare = pipeline.translation_stats(zh)
        check(
            "没有 report 文件时统计保持旧形状（老产物也能读）",
            "truncatedBatches" not in bare and bare.get("blocks") == 2,
            json.dumps(bare, ensure_ascii=False)[:160],
        )


def case_bridge_error_classification():
    """宿主的错误分类要原样到达引擎：档位被拒不重试，限流要按长退避重试。"""
    import urllib.request

    with tempfile.TemporaryDirectory() as tmp:
        token_path = os.path.join(tmp, "bridge-token")
        io.open(token_path, "w", encoding="utf-8").write("t" * 64)
        bridge_path = os.path.join(tmp, "bridge.json")
        io.open(bridge_path, "w", encoding="utf-8").write(json.dumps({
            "baseUrl": "http://127.0.0.1:9",
            "bridgePath": "/api/dsh-subtitle-studio/llm",
            "tokenPath": token_path,
        }))
        old_bridge = os.environ.get("SUBTITLE_STUDIO_BRIDGE")
        old_sleep = translate_srt.time.sleep
        os.environ["SUBTITLE_STUDIO_BRIDGE"] = bridge_path
        translate_srt.time.sleep = lambda *_: None
        attempts = []

        def make_error(code, body):
            def raiser(*args, **kwargs):
                attempts.append(code)
                raise urllib.error.HTTPError(
                    "http://127.0.0.1:9", code, "boom", {}, io.BytesIO(body.encode("utf-8")))
            return raiser

        original = urllib.request.urlopen
        try:
            urllib.request.urlopen = make_error(422, json.dumps({
                "ok": False, "code": "unknown-effort", "error": "effort not valid"}))
            text, meta = translate_srt._call_dsh_bridge("sys", "user", "fake-provider", "fake-model")
            check("档位被拒（422 unknown-effort）算确定性失败", meta.get("fatal") is True, json.dumps(meta))
            check("档位被拒时错误码原样传到引擎", meta.get("code") == "unknown-effort", json.dumps(meta))
            check("档位被拒不重试（只问一次）", len(attempts) == 1, str(len(attempts)))

            attempts[:] = []
            urllib.request.urlopen = make_error(429, json.dumps({
                "ok": False, "code": "rate-limited", "error": "slow down"}))
            text, meta = translate_srt._call_dsh_bridge("sys", "user", "fake-provider", "fake-model")
            check("限流（429）会重试", len(attempts) == 3, str(len(attempts)))
            check("限流重试到底后如实报告不可达", meta.get("fatal") is False, json.dumps(meta))
        finally:
            urllib.request.urlopen = original
            translate_srt.time.sleep = old_sleep
            if old_bridge is None:
                os.environ.pop("SUBTITLE_STUDIO_BRIDGE", None)
            else:
                os.environ["SUBTITLE_STUDIO_BRIDGE"] = old_bridge


def case_config_validation():
    base = json.loads(json.dumps(config_mod.DEFAULTS))

    def issues(data):
        return config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, data))

    base["source"] = dict(base.get("source") or {}, kind="local", path="/tmp/in")
    base["output"] = dict(base.get("output") or {}, kind="local", path="/tmp/out")
    base["engine"] = dict(base["engine"], kind="dsh", provider="p", model="m", reasoningEffort="low")

    def errors(data):
        return [item for item in issues(data) if item.get("level") == "error"]

    check("REQ-49 合法档位通过校验", errors(base) == [], json.dumps(errors(base), ensure_ascii=False)[:200])
    base["engine"]["reasoningEffort"] = ""
    check("REQ-49 留空（跟随模型默认）通过校验", errors(base) == [], json.dumps(errors(base), ensure_ascii=False)[:160])
    base["engine"]["reasoningEffort"] = "very high!"
    found = issues(base)
    check(
        "REQ-49 带空格/标点的档位名被拒",
        any(item.get("path") == "engine.reasoningEffort" for item in found),
        json.dumps(found, ensure_ascii=False)[:200],
    )


def case_cli_passes_effort():
    """档位只在下发非空值时才进翻译命令行 —— 留空等于让模型用自己的默认。"""
    captured = []

    class Cfg(object):
        data = {}

        def get(self, path, default=None):
            node = self.data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    workdir = tempfile.mkdtemp()
    src = os.path.join(workdir, "a.src.srt")
    io.open(src, "w", encoding="utf-8").write("1\n00:00:01,000 --> 00:00:02,000\n原文\n")
    zh = os.path.join(workdir, "a.zh.srt")
    cfg = Cfg()
    cfg.data = {
        "transcribe": {"engine": "mlx"},
        "engine": {"kind": "dsh", "provider": "p", "model": "m", "temperature": 0.1,
                   "seed": 1, "maxTokens": 8000, "batchSize": 10, "reasoningEffort": "high"},
        "subtitles": {},
        "paths": {},
    }
    original = pipeline.run_cmd
    original_env = pipeline.engine_env
    pipeline.engine_env = lambda cfg: {}

    def fake_run(cmd, timeout=None, check=None, **kwargs):
        captured.append(list(cmd))
        raise RuntimeError("stop here")

    pipeline.run_cmd = fake_run
    try:
        try:
            pipeline.translate(cfg, NullLog(), src, zh, {"system": ""})
        except Exception:
            pass
    finally:
        pipeline.run_cmd = original
    joined = " ".join(str(part) for part in captured[0]) if captured else ""
    check("REQ-49 非空档位被下发到翻译命令行", "--reasoning-effort high" in joined, joined[:200])
    captured[:] = []
    cfg.data["engine"]["reasoningEffort"] = ""
    pipeline.run_cmd = fake_run
    try:
        try:
            pipeline.translate(cfg, NullLog(), src, zh, {"system": ""})
        except Exception:
            pass
    finally:
        pipeline.run_cmd = original
        pipeline.engine_env = original_env
    joined = " ".join(str(part) for part in captured[0]) if captured else ""
    check("REQ-49 留空时不传该参数（跟随默认）", "--reasoning-effort" not in joined, joined[:200])


def main():
    case_match_by_timestamp()
    case_truncated_batch_salvages_and_splits()
    case_single_missing_is_retried_alone()
    case_fatal_is_not_retried()
    case_depth_and_call_caps()
    case_report_merge()
    case_bridge_error_classification()
    case_config_validation()
    case_cli_passes_effort()
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILURES)))
    if FAILURES:
        print("FAILED:")
        for name in FAILURES:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

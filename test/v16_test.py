#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕工坊 v1.6 引擎自测（需求池 REQ-46 / REQ-59 / REQ-60）。

覆盖：
1. REQ-46 通知口径 —— run 与 anyOutput 在五种局面下各发什么事件，以及
   execute() 真实链路到底发了几条、标题是什么（这块此前零覆盖）；
2. REQ-59 成品台账 —— 记一条读回来、命中即跳过并带依据、成品没了就不跳过、
   超量淘汰、--only 无视台账、关掉「跳过已翻译」也不拦；
3. REQ-60 自家成品 —— 源目录含自家成品时按命名模板反推排掉，
   关掉开关能选回来，且不靠后缀误伤真实片源；
4. 两个新配置键的校验与宿主默认值/字段表。
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.abspath(os.path.join(HERE, "..", "engine"))
if not os.path.isdir(ENGINE_DIR):
    ENGINE_DIR = os.path.abspath(
        os.path.join(HERE, "..", "..", "dsh-subtitle-studio", "engine"))
sys.path.insert(0, ENGINE_DIR)

from lib import config as config_mod  # noqa: E402
from lib import notify, pipeline, runtime  # noqa: E402
from lib import select as selection  # noqa: E402

FAILURES = []
PASSED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("PASS  %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILURES.append(name)
        print("FAIL  %s%s" % (name, ("  <- " + detail) if detail else ""))


class Cfg(object):
    """够用的配置替身：点分 get + execute() 要的四个目录与 path。"""

    def __init__(self, data, root=None):
        self.data = data
        root = root or tempfile.mkdtemp(prefix="v16-")
        self.state_dir = os.path.join(root, "state")
        self.data_dir = root
        self.work_dir = os.path.join(root, "work")
        self.logs_dir = os.path.join(root, "logs")
        self.path = os.path.join(root, "config.json")

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def make_cfg(notify_extra=None, selection_extra=None, root=None):
    data = json.loads(json.dumps(config_mod.DEFAULTS))
    data["notify"].update({"enabled": True, "channel": "command",
                           "command": "true"})
    data["notify"].update(notify_extra or {})
    data["selection"].update({"maxPerRun": 0})
    data["output"]["path"] = "/v"
    data["selection"].update(selection_extra or {})
    return Cfg(data, root=root)


def totals(selected=0, ok=0, skipped=0, failed=0):
    return {"selected": selected, "ok": ok, "skipped": skipped, "failed": failed}


def rows(*pairs):
    return [{"name": "f%d.mp4" % index, "status": status, "output": output,
             "message": ""}
            for index, (status, output) in enumerate(pairs)]


# ==================================================================== REQ-46 纯判定

def case_events_run():
    cfg = make_cfg({"successWhen": "run"})
    check("REQ46 run 全成功 -> success",
          notify.resolve_events(cfg, totals(2, 2), rows(("ok", "/a"), ("ok", "/b"))) == ["success"])
    check("REQ46 run 空跑仍算成功（保旧语义）",
          notify.resolve_events(cfg, totals(0, 0, 3), []) == ["success"])
    check("REQ46 run 部分成功 -> 只发 failure（旧行为不变）",
          notify.resolve_events(cfg, totals(3, 2, 0, 1),
                               rows(("ok", "/a"), ("ok", "/b"), ("failed", None))) == ["failure"])
    check("REQ46 run 全失败 -> failure",
          notify.resolve_events(cfg, totals(1, 0, 0, 1), rows(("failed", None))) == ["failure"])
    check("REQ46 run 中止但有产出 -> failure",
          notify.resolve_events(cfg, totals(2, 1), rows(("ok", "/a")), abort=4) == ["failure"])


def case_events_any_output():
    cfg = make_cfg({"successWhen": "anyOutput"})
    check("REQ46 anyOutput 有产出 -> success",
          notify.resolve_events(cfg, totals(1, 1), rows(("ok", "/a"))) == ["success"])
    check("REQ46 anyOutput 部分成功 -> 成功与失败各一条",
          notify.resolve_events(cfg, totals(3, 2, 0, 1),
                               rows(("ok", "/a"), ("ok", "/b"), ("failed", None)))
          == ["success", "failure"])
    check("REQ46 anyOutput 空跑静默（定时扫描不再吵）",
          notify.resolve_events(cfg, totals(0, 0, 5), []) == [])
    check("REQ46 anyOutput 一部没出但有失败 -> failure",
          notify.resolve_events(cfg, totals(2, 0, 0, 2),
                               rows(("failed", None), ("failed", None))) == ["failure"])
    check("REQ46 anyOutput 全跳过没产出没失败 -> 静默",
          notify.resolve_events(cfg, totals(2, 0, 2), rows(("skipped", None), ("skipped", None))) == [])
    check("REQ46 anyOutput 中止但有产出 -> 成功 + 失败都发",
          notify.resolve_events(cfg, totals(2, 1), rows(("ok", "/a")), abort=4)
          == ["success", "failure"])
    check("REQ46 产出只数真写了 output 的那几条",
          notify.resolve_events(cfg, totals(2, 2), rows(("ok", "/a"), ("skipped", None))) == ["success"])


def case_gating_is_separate():
    """resolve_events 只管「发生了什么」，开关归 should_notify —— 两层都要对。"""
    cfg = make_cfg({"successWhen": "anyOutput", "enabled": False})
    check("REQ46 关掉总开关后 should_notify 全部拒发",
          not any(notify.should_notify(cfg, event) for event in ("start", "success", "failure")))
    cfg2 = make_cfg({"successWhen": "anyOutput", "onSuccess": False})
    events = notify.resolve_events(cfg2, totals(2, 1, 0, 1), rows(("ok", "/a"), ("failed", None)))
    sent = [event for event in events if notify.should_notify(cfg2, event)]
    check("REQ46 onSuccess 关了只发 failure", sent == ["failure"], str(events) + " -> " + str(sent))


# ==================================================================== REQ-46 真链路

class SpyNotify(object):
    def __init__(self):
        self.sent = []

    def send(self, cfg, title, body, logger=None):
        self.sent.append((title, body))
        return {"sent": True}


def run_execute(cfg, plan, monkey_results):
    """用假 process_one 跑 execute()，只看通知行为，不碰 ffmpeg。"""
    original = pipeline.Pipeline.process_one
    queue = list(monkey_results)

    def fake(self, candidate):
        return queue.pop(0)

    pipeline.Pipeline.process_one = fake
    spy = SpyNotify()
    log = runtime.Log(cfg.logs_dir, name="v16", echo=False)
    try:
        payload = pipeline.execute(cfg, log, plan, dry_run=False, notify_module=spy)
    finally:
        pipeline.Pipeline.process_one = original
    return payload, spy


def empty_plan(root):
    return {
        "source": {"display": "local:/src"},
        "output": {"display": "local:/out"},
        "dirs": [{"name": "d", "source": {"display": "local:/src"},
                  "output": {"display": "local:/out"}, "reachable": True}],
        "multiDir": False,
        "selectedDetail": [],
    }


def case_execute_notifications():
    root = tempfile.mkdtemp(prefix="v16-exec-")
    cfg = make_cfg({"successWhen": "run", "onStart": True}, root=root)
    payload, spy = run_execute(cfg, empty_plan(root), [])
    check("REQ46 run 空跑 execute 发一条", len(spy.sent) == 1, str(spy.sent))
    check("REQ46 run 空跑标题改成「本轮无新片」",
          spy.sent and spy.sent[0][0] == "本轮无新片", spy.sent[0][0] if spy.sent else "")
    check("REQ46 空跑不发 start（没选中就没开始）",
          all(title != "开始处理" for title, _ in spy.sent))

    root2 = tempfile.mkdtemp(prefix="v16-exec2-")
    cfg2 = make_cfg({"successWhen": "anyOutput"}, root=root2)
    payload2, spy2 = run_execute(cfg2, empty_plan(root2), [])
    check("REQ46 anyOutput 空跑 execute 一条都不发", spy2.sent == [], str(spy2.sent))

    root3 = tempfile.mkdtemp(prefix="v16-exec3-")
    cfg3 = make_cfg({"successWhen": "anyOutput"}, root=root3)
    plan3 = empty_plan(root3)
    plan3["selectedDetail"] = [{"name": "好.mp4"}, {"name": "坏.mp4"}]
    good = {"name": "好.mp4", "status": "ok", "stage": "done", "message": "",
            "code": None, "output": "/out/好_zh.mp4", "outputBytes": 10,
            "elapsedSec": 1, "workDir": root3}
    bad = {"name": "坏.mp4", "status": "failed", "stage": "translate",
           "message": "炸了", "code": 5, "output": None, "outputBytes": None,
           "elapsedSec": 1, "workDir": root3}
    payload3, spy3 = run_execute(cfg3, plan3, [good, bad])
    titles = [title for title, _ in spy3.sent]
    check("REQ46 anyOutput 部分成功 execute 连发两条",
          titles == ["处理完成", "处理有失败"], str(titles))
    check("REQ46 成功那条列出了产出",
          "好_zh.mp4" in spy3.sent[0][1], spy3.sent[0][1][:80])
    check("REQ46 失败那条列出了失败原因",
          "炸了" in spy3.sent[1][1], spy3.sent[1][1][:80])
    check("REQ46 汇总数字仍然如实（选中2 成功1 失败1）",
          payload3["totals"] == {"selected": 2, "ok": 1, "skipped": 0, "failed": 1,
                                 "notRun": 0},
          str(payload3["totals"]))
    check("REQ46 失败那条不抄成功那条的产出清单",
          "好_zh.mp4" not in spy3.sent[1][1] and "另有 1 部已产出" in spy3.sent[1][1],
          spy3.sent[1][1][:120])


# ==================================================================== REQ-59 台账

def case_ledger_roundtrip():
    cfg = make_cfg()
    check("REQ59 台账初始为空", pipeline.load_translated(cfg) == {})
    pipeline.record_translated(cfg, "a.mp4", "/out/a_zh.mp4", "burn")
    pipeline.record_translated(cfg, "b.mp4", "/out/b_zh.srt", "srt", source_dir="/src")
    data = pipeline.load_translated(cfg)
    check("REQ59 记两条读回两条", sorted(data) == ["a.mp4", "b.mp4"], str(sorted(data)))
    check("REQ59 记录带成品路径与输出方式",
          data["a.mp4"]["output"] == "/out/a_zh.mp4" and data["a.mp4"]["mode"] == "burn")
    check("REQ59 记录带时间戳", bool(data["b.mp4"].get("at")))
    pipeline.record_translated(cfg, "a.mp4", "/out2/a_zh.mp4", "srt")
    check("REQ59 同片重复记账是覆盖不是堆键",
          pipeline.load_translated(cfg)["a.mp4"]["output"] == "/out2/a_zh.mp4")
    check("REQ59 台账落在 state 目录",
          os.path.isfile(os.path.join(cfg.state_dir, pipeline.TRANSLATED_FILE)))
    check("REQ59 台账文件权限 0600",
          (os.stat(os.path.join(cfg.state_dir, pipeline.TRANSLATED_FILE)).st_mode & 0o777) == 0o600)


def case_ledger_cap():
    cfg = make_cfg()
    for index in range(pipeline.TRANSLATED_MAX + 25):
        pipeline.record_translated(cfg, "f%05d.mp4" % index, "/o/f%05d_zh.mp4" % index, "burn")
    data = pipeline.load_translated(cfg)
    check("REQ59 台账有条数上限", len(data) <= pipeline.TRANSLATED_MAX,
          "%d <= %d" % (len(data), pipeline.TRANSLATED_MAX))
    check("REQ59 淘汰的是最旧的", ("f%05d.mp4" % (pipeline.TRANSLATED_MAX + 24)) in data)


def case_select_with_ledger():
    cfg = make_cfg()
    files = [
        {"name": "已译.mp4", "path": "/src/已译.mp4", "size": 5 * 1024 ** 3, "mtime": 1},
        {"name": "新片.mp4", "path": "/src/新片.mp4", "size": 4 * 1024 ** 3, "mtime": 2},
    ]
    plan = selection.select(files, cfg, existing_outputs=[],
                            name_template="{stem}_zh.mp4",
                            translated_ledger={"已译.mp4": "/old/已译_zh.mp4"})
    reasons = plan["counts"]["byReason"]
    check("REQ59 台账命中即跳过，哪怕当前输出目录里没有那个文件",
          reasons.get("translated-ledger") == 1 and reasons.get("selected") == 1, str(reasons))
    hit = [c for c in plan["candidates"] if c["reason"] == "translated-ledger"][0]
    check("REQ59 跳过理由写明依据是哪个成品",
          hit["translatedEvidence"] == "/old/已译_zh.mp4", str(hit.get("translatedEvidence")))
    check("REQ59 跳过文案可读", "台账" in hit["reasonText"], hit["reasonText"])

    forced = selection.select(files, cfg, existing_outputs=[], only="已译.mp4", force=True,
                              name_template="{stem}_zh.mp4",
                              translated_ledger={"已译.mp4": "/old/已译_zh.mp4"})
    check("REQ59 --only 指名要做的片子无视台账",
          forced["counts"]["byReason"].get("selected") == 1,
          str(forced["counts"]["byReason"]))

    cfg_off = make_cfg(selection_extra={"skipTranslated": False})
    plain = selection.select(files, cfg_off, existing_outputs=[], name_template="{stem}_zh.mp4",
                            translated_ledger={"已译.mp4": "/old/已译_zh.mp4"})
    check("REQ59 关掉「跳过已翻译」后台账也不拦",
          plain["counts"]["byReason"].get("translated-ledger") is None
          and plain["counts"]["byReason"].get("selected") == 2,
          str(plain["counts"]["byReason"]))

    dir_plan = selection.select(files, cfg, existing_outputs=["新片_zh.mp4"],
                                name_template="{stem}_zh.mp4")
    hit2 = [c for c in dir_plan["candidates"] if c["reason"] == "translated-exists"][0]
    check("REQ59 目录命中也带出依据路径",
          hit2["translatedEvidence"].endswith("新片_zh.mp4"), hit2["translatedEvidence"])


def case_verify_ledger_hits():
    """成品真没了就不能再当已译：宁可重译，不能永不再做。"""
    listings = {"/old": ["a_zh.mp4"], "/out": []}
    made = []

    class FakeEndpoint(object):
        def __init__(self, kind, path, ssh=None, logger=None):
            self.path = path
            made.append(path)

        def probe(self):
            return {"ok": self.path in listings}

        def list_files(self, include=None, recursive=False):
            return [{"name": name} for name in listings.get(self.path, [])]

    original = pipeline.Endpoint
    pipeline.Endpoint = FakeEndpoint
    entry = {"output": {"kind": "local", "path": "/out", "ssh": {}}}
    log = runtime.Log(tempfile.mkdtemp(prefix="v16-log-"), name="t", echo=False)
    try:
        kept, unchecked = pipeline.verify_ledger_hits(
            entry, {"a.mp4": "/old/a_zh.mp4", "b.mp4": "/old/gone_zh.mp4"}, log)
        check("REQ59 成品还在 -> 认账", "a.mp4" in kept, str(kept))
        check("REQ59 成品被删 -> 不认账，会重译", "b.mp4" not in kept, str(kept))
        check("REQ59 看清了不在，不算未核实", unchecked == 0, str(unchecked))
        hits = {("m%03d.mp4" % i): ("/d%03d/m%03d_zh.mp4" % (i, i)) for i in range(20)}
        listings.update(dict(("/d%03d" % i, ["m%03d_zh.mp4" % i]) for i in range(20)))
        many, many_unchecked = pipeline.verify_ledger_hits(entry, hits, log, max_dirs=8)
        check("REQ59 成品散落太多目录时不过度扫描，但也不重译（记为未核实）",
              len(many) == 20 and many_unchecked == 20,
              "kept=%d unchecked=%d" % (len(many), many_unchecked))
        # 输出目录连不上：一次网络抖动不该换来整库重译
        listings.clear()
        gone, gone_unchecked = pipeline.verify_ledger_hits(
            entry, {"a.mp4": "/old/a_zh.mp4"}, log)
        check("REQ59 输出目录连不上 -> 保留台账命中并记未核实",
              gone == {"a.mp4": "/old/a_zh.mp4"} and gone_unchecked == 1,
              "%s / %s" % (gone, gone_unchecked))

        def explode(*args, **kwargs):
            raise pipeline.EngineError(4, "来源不可达（模拟）")
        pipeline.Endpoint = lambda *a, **k: type("Boom", (), {
            "probe": lambda self: {"ok": True}, "list_files": explode})()
        try:
            broke, broke_unchecked = pipeline.verify_ledger_hits(
                entry, {"a.mp4": "/old/a_zh.mp4"}, log)
            check("REQ59 列目录抛错 -> 同样保留并记未核实",
                  broke == {"a.mp4": "/old/a_zh.mp4"} and broke_unchecked == 1,
                  "%s / %s" % (broke, broke_unchecked))
        finally:
            pipeline.Endpoint = FakeEndpoint
    finally:
        pipeline.Endpoint = original


def case_record_wired_into_process_one():
    src = open(os.path.join(ENGINE_DIR, "lib", "pipeline.py"), encoding="utf-8").read()
    body = src.split("def process_one", 1)[1].split("def _cleanup", 1)[0]
    check("REQ59 成功路径会写台账", "record_translated(" in body)
    plan_src = src.split("def build_plan", 1)[1].split("def dry_run_plan", 1)[0]
    check("REQ59 选片时会读台账并先核实",
          "load_translated(" in plan_src and "verify_ledger_hits(" in plan_src)


# ==================================================================== REQ-60 自家成品

def case_own_output():
    cfg = make_cfg()
    check("REQ60 模板匹配器：字面部分够长才生效",
          selection.template_matcher("{stem}_zh.mp4") is not None
          and selection.template_matcher("{name}") is None
          and selection.template_matcher("{stem}.{ext}") is None)
    matcher = selection.template_matcher("{stem}_zh.mp4")
    check("REQ60 匹配器认得出两种输出方式的成品、不认原片",
          bool(matcher.match("第一话_zh.mp4")) and bool(matcher.match("第一话_zh.srt"))
          and not matcher.match("第一话.mp4"), matcher.pattern)
    files = [
        {"name": "第一话.mp4", "path": "/v/第一话.mp4", "size": 5 * 1024 ** 3, "mtime": 1},
        {"name": "第一话_zh.mp4", "path": "/v/第一话_zh.mp4", "size": 4 * 1024 ** 3, "mtime": 2},
        {"name": "第二话_zh.mp4", "path": "/v/第二话_zh.mp4", "size": 4 * 1024 ** 3, "mtime": 3},
    ]
    plan = selection.select(files, cfg, existing_outputs=[], name_template="{stem}_zh.mp4",
                            output_dir="/v")
    reasons = plan["counts"]["byReason"]
    check("REQ60 自家成品不被当片源（不会套成 _zh_zh）",
          reasons.get("own-output") == 2 and reasons.get("selected") == 1, str(reasons))
    hit = [c for c in plan["candidates"] if c["reason"] == "own-output"][0]
    check("REQ60 文案说清是按命名模板认出来的",
          "命名模板" in hit["reasonText"], hit["reasonText"])

    ext_token = selection.template_matcher("{stem}_zh.{ext}")
    check("REQ60 模板把扩展名写成 {ext} 时匹配器照样生效（不会静默失灵）",
          ext_token is not None and bool(ext_token.match("第一话_zh.mp4"))
          and bool(ext_token.match("第一话_zh.srt")) and not ext_token.match("第一话.mp4"),
          getattr(ext_token, "pattern", "None"))
    plan_ext = selection.select(files, cfg, existing_outputs=[],
                                name_template="{stem}_zh.{ext}", output_dir="/v")
    check("REQ60 {ext} 模板下成品目录当片源同样被排掉",
          plan_ext["counts"]["byReason"].get("own-output") == 2,
          str(plan_ext["counts"]["byReason"]))
    check("REQ60 认不出的占位符不做正则（宁可不用这条规则，也不要静默漏判）",
          selection.template_matcher("{stem.lower()}") is None
          and selection.template_matcher("{stem}_{title}") is None)
    forced = selection.select(files, cfg, existing_outputs=[], name_template="{stem}_zh.mp4",
                              output_dir="/v", force=True)
    check("REQ60 force 点名要处理时，自家成品的判定让路",
          forced["counts"]["byReason"].get("own-output") is None,
          str(forced["counts"]["byReason"]))
    named = selection.select(files, cfg, existing_outputs=[], name_template="{stem}_zh.mp4",
                             output_dir="/v", only="第一话_zh.mp4")
    check("REQ60 --only 精确点名的文件不被自家成品规则挡掉",
          named["counts"]["byReason"].get("selected") == 1
          and named["selected"][0]["name"] == "第一话_zh.mp4",
          str(named["counts"]["byReason"]))

    cfg_off = make_cfg(selection_extra={"skipOwnOutputs": False})
    plan_off = selection.select(files, cfg_off, existing_outputs=[], name_template="{stem}_zh.mp4",
                                output_dir="/v")
    check("REQ60 开关关掉能选回来自家成品（逃生口）",
          plan_off["counts"]["byReason"].get("selected") == 3,
          str(plan_off["counts"]["byReason"]))

    inside = selection.select(
        [{"name": "剧场版_zh.mp4", "path": "/v/剧场版_zh.mp4", "size": 5 * 1024 ** 3, "mtime": 1}],
        cfg, existing_outputs=[], name_template="{stem}_zh.mp4", output_dir="/v")
    check("REQ60 成品目录被当成片源时，按名字规则排掉（就是这次多翻一遍的根因）",
          inside["counts"]["byReason"].get("own-output") == 1, str(inside["counts"]["byReason"]))
    elsewhere = selection.select(
        [{"name": "剧场版_zh.mp4", "path": "/other/剧场版_zh.mp4", "size": 5 * 1024 ** 3, "mtime": 1}],
        cfg, existing_outputs=[], name_template="{stem}_zh.mp4", output_dir="/v")
    check("REQ60 规则只作用在输出目录里，不伤别的库里真叫 _zh 的片源",
          elsewhere["counts"]["byReason"].get("selected") == 1,
          str(elsewhere["counts"]["byReason"]))
    no_dir = selection.select(
        [{"name": "第一话.mp4", "path": "/v/第一话.mp4", "size": 5 * 1024 ** 3, "mtime": 1},
         {"name": "第一话_zh.mp4", "path": "/v/第一话_zh.mp4", "size": 4 * 1024 ** 3, "mtime": 2}],
        cfg, existing_outputs=[], name_template="{stem}_zh.mp4", output_dir="")
    check("REQ60 没给输出目录时靠同批兄弟文件也能认出自家成品",
          no_dir["counts"]["byReason"].get("own-output") == 1, str(no_dir["counts"]["byReason"]))

    srt_plan = selection.select(
        [{"name": "第一话.mp4", "path": "/v/第一话.mp4", "size": 5 * 1024 ** 3, "mtime": 1},
         {"name": "第一话_zh.srt", "path": "/v/第一话_zh.srt", "size": 1024, "mtime": 2},
         {"name": "第一话_zh.mp4", "path": "/v/第一话_zh.mp4", "size": 4 * 1024 ** 3, "mtime": 3}],
        cfg, existing_outputs=[], name_template="{stem}_zh.mp4", output_ext="srt",
        output_dir="/v")
    check("REQ60 字幕模式下 .srt 成品与旧 mp4 成品都排掉",
          srt_plan["counts"]["byReason"].get("own-output") == 2,
          str(srt_plan["counts"]["byReason"]))

    odd = selection.select(
        [{"name": "电影.mp4", "path": "/v/电影.mp4", "size": 5 * 1024 ** 3, "mtime": 1}],
        cfg, existing_outputs=[], name_template="{stem}.mp4")
    check("REQ60 模板不产生新名字时不会把自己排掉",
          odd["counts"]["byReason"].get("selected") == 1, str(odd["counts"]["byReason"]))


# ==================================================================== 配置与宿主字段

def case_plan_wiring_real_dirs():
    """走真 build_plan，不只喂 select()。

    output_dir 这一环若换成展示串（Endpoint.describe() 是 "local:/x"，不是路径），
    自家成品规则会静默失效，而直接调 select() 的单测传的是写死路径，看不见
    这个 bug（R3）。同时验证规则的边界：只有躺在输出目录里的才按名字排。
    """
    root = tempfile.mkdtemp(prefix="v16-plan-")
    lib = os.path.join(root, "lib")
    done = os.path.join(root, "done")
    os.makedirs(lib)
    os.makedirs(done)

    def touch(directory, name):
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(b"\0" * 4096)

    touch(lib, "第一话.mp4")          # 原片，成品在 done 里
    touch(lib, "剧场版_zh.mp4")       # 片库里真有个叫 _zh 的片源，不在输出目录
    touch(done, "第一话_zh.mp4")      # 已产出的成品
    touch(done, "OVA_zh.mp4")         # 成品目录被当片源时会扫到这种

    def plan_for(source_dir, output_dir):
        config = json.loads(json.dumps(config_mod.DEFAULTS))
        config["version"] = 1
        config["engine"].update({"kind": "dsh", "provider": "p", "model": "m"})
        config["paths"] = {"dataDir": os.path.join(root, "data"), "keepWorkArtifacts": False}
        config["source"] = {"kind": "local", "path": source_dir, "include": ["*.mp4"],
                            "recursive": False, "ssh": {}}
        config["output"] = {"kind": "local", "path": output_dir,
                            "nameTemplate": "{stem}_zh.mp4", "overwrite": False}
        config["selection"].update({"maxPerRun": 0, "skipTranslated": True,
                                    "skipOwnOutputs": True, "minSizeGB": 0,
                                    "maxSizeGB": 10, "excludePatterns": []})
        path = os.path.join(root, "config-%s.json" % os.path.basename(output_dir))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False)
        cfg = config_mod.load_config(path)
        return pipeline.build_plan(cfg, runtime.Log(None, "v16-plan", echo=False))

    spread = plan_for(lib, done)
    reasons = {c["name"]: c["reason"] for c in spread["candidates"]}
    check("走真 build_plan：输出目录里的成品认得出（translated-exists 是目录命中）",
          reasons.get("第一话.mp4") == "translated-exists", str(reasons))
    check("走真 build_plan：片库里真叫 _zh 的片源不被误伤（不在输出目录）",
          reasons.get("剧场版_zh.mp4") in ("selected", "eligible"), str(reasons))
    report = (spread.get("dirs") or [{}])[0].get("ledger")
    check("计划里带着这本台账的账（命中/没了/没核实）",
          isinstance(report, dict) and set(report) == {"hits", "missing", "unchecked"},
          str(report))

    # 成品目录被直接当片源（这次多翻一遍的根因）
    merged = plan_for(done, done)
    merged_reasons = {c["name"]: c["reason"] for c in merged["candidates"]}
    check("走真 build_plan：成品目录当片源时按命名模板排掉，不再套 _zh_zh",
          merged_reasons.get("OVA_zh.mp4") == "own-output"
          and merged_reasons.get("第一话_zh.mp4") == "own-output", str(merged_reasons))
    check("排掉之后本轮没有可选文件（不会白烧额度）",
          merged["selectedDetail"] == [] and merged["selected"] == [],
          str(merged["selected"]))

    # dry-run 不许写 state：迁移与台账都不能落盘
    cfg_probe = config_mod.load_config(os.path.join(root, "config-done.json"))
    state_dir = os.path.join(cfg_probe.data_dir, "state")
    before = sorted(os.listdir(state_dir)) if os.path.isdir(state_dir) else []
    pipeline.build_plan(cfg_probe, runtime.Log(None, "v16-dry", echo=False), write_state=False)
    after = sorted(os.listdir(state_dir)) if os.path.isdir(state_dir) else []
    check("write_state=False 时选片不新增状态文件", after == before, "%s -> %s" % (before, after))


def case_config_validation():
    def errors_for(mutate):
        data = json.loads(json.dumps(config_mod.DEFAULTS))
        data["source"]["path"] = "/src"
        data["output"]["path"] = "/out"
        mutate(data)
        return [i for i in config_mod.validate(data) if i.get("level") == "error"]

    def errors_at(path, mutate):
        return [i for i in errors_for(mutate) if i["path"] == path]

    check("默认值本身在两个新键上没有 error",
          errors_at("notify.successWhen", lambda d: None) == []
          and errors_at("selection.skipOwnOutputs", lambda d: None) == [])
    check("接受 successWhen=run",
          errors_at("notify.successWhen", lambda d: d["notify"].__setitem__("successWhen", "run")) == [])
    check("接受 successWhen=anyOutput",
          errors_at("notify.successWhen", lambda d: d["notify"].__setitem__("successWhen", "anyOutput")) == [])
    bad = errors_for(lambda d: d["notify"].__setitem__("successWhen", "always"))
    check("拒绝非法 successWhen", any(i["path"] == "notify.successWhen" for i in bad), str(bad))
    check("拒绝渲染不了的命名模板（{stem.lower()} 会在选片阶段炸整轮）",
          errors_at("output.nameTemplate",
                    lambda d: d["output"].__setitem__("nameTemplate", "{stem.lower()}_zh.mp4")) != []
          and errors_at("dirs[0].output.nameTemplate",
                        lambda d: d.__setitem__("dirs", [{
                            "source": dict(d["source"], path="/a"),
                            "output": dict(d["output"], path="/b",
                                           nameTemplate="{stem[:3]}_zh.mp4")}])) != [])
    check("接受扩展名占位符写法",
          errors_at("output.nameTemplate",
                    lambda d: d["output"].__setitem__("nameTemplate", "{stem}_zh.{ext}")) == [])
    bad2 = errors_for(lambda d: d["selection"].__setitem__("skipOwnOutputs", "yes"))
    check("拒绝非布尔 skipOwnOutputs",
          any(i["path"] == "selection.skipOwnOutputs" for i in bad2), str(bad2))


def case_defaults_and_host_fields():
    check("引擎 DEFAULTS 有 successWhen 且默认 run",
          config_mod.DEFAULTS["notify"].get("successWhen") == "run")
    check("引擎 DEFAULTS 有 skipOwnOutputs 且默认开",
          config_mod.DEFAULTS["selection"].get("skipOwnOutputs") is True)
    host = open(os.path.join(ENGINE_DIR, "..", "index.js"), encoding="utf-8").read()
    check("宿主 FIELDS 暴露 notify.successWhen", "notify.successWhen" in host)
    check("宿主 FIELDS 暴露 selection.skipOwnOutputs", "selection.skipOwnOutputs" in host)
    check("宿主默认配置带这两个键",
          "successWhen: 'run'" in host and "skipOwnOutputs: true" in host)
    check("宿主 FIELDS 的 successWhen 枚举来自引擎同一份常量",
          "options: NOTIFY_SUCCESS_WHEN" in host and "NOTIFY_SUCCESS_WHEN = ['run', 'anyOutput']" in host)


# ==================================== 审计补充（2026-11-27 独立复核提的 5 个覆盖空洞）

def case_ledger_prefix_multi_dir():
    """多目录时台账键带目录前缀：同一个文件名在不同目录不会互相顶掉。"""
    cfg = make_cfg()
    pipeline.record_translated(cfg, "[a1b2c3d4] 第01话.mp4", "/out/a/第01话_zh.mp4", "burn")
    pipeline.record_translated(cfg, "[e5f6a7b8] 第01话.mp4", "/out/b/第01话_zh.mp4", "burn")
    data = pipeline.load_translated(cfg)
    check("REQ59 多目录台账两个同名片各记一条", len(data) == 2, str(sorted(data)))
    entry = {"source": {"path": "/src/a"}, "output": {"path": "/out/a"}}
    with_prefix = pipeline._ledger_hits(cfg, entry, data, ["第01话.mp4"], "[a1b2c3d4] ")
    check("REQ59 带前缀能命中自己目录那条",
          with_prefix.get("第01话.mp4") == "/out/a/第01话_zh.mp4", str(with_prefix))
    wrong = pipeline._ledger_hits(cfg, entry, data, ["第01话.mp4"], "[ff00ff00] ")
    check("REQ59 前缀不匹配就不命中（不串目录）", wrong == {}, str(wrong))
    single = pipeline._ledger_hits(cfg, entry, data, ["第01话.mp4"], "")
    check("REQ59 无前缀时不命中带前缀的键", single == {}, str(single))


def case_success_when_enum_parity():
    """宿主枚举与引擎枚举必须逐元素一致：任一侧加了值都要被这条抓住。"""
    import re
    host = open(os.path.join(ENGINE_DIR, "..", "index.js"), encoding="utf-8").read()
    match = re.search(r"NOTIFY_SUCCESS_WHEN\s*=\s*\[([^\]]*)\]", host)
    host_values = []
    if match:
        host_values = [v.strip().strip("'\"") for v in match.group(1).split(",") if v.strip()]
    engine_values = list(config_mod.NOTIFY_SUCCESS_WHEN)
    check("REQ46 两端的 successWhen 取值逐元素一致",
          host_values == engine_values, "host=%s engine=%s" % (host_values, engine_values))


def case_verify_partial_beyond_max_dirs():
    """一部分目录在 max_dirs 之内、一部分在外：整体按未核实处理，且不崩。"""
    listings = {}
    hits = {}
    for i in range(11):
        directory = "/d%02d" % i
        listings[directory] = ["m%02d_zh.mp4" % i]
        hits["m%02d.mp4" % i] = "%s/m%02d_zh.mp4" % (directory, i)

    class FakeEndpoint(object):
        def __init__(self, kind, path, ssh=None, logger=None):
            self.path = path

        def probe(self):
            return {"ok": self.path in listings}

        def list_files(self, include=None, recursive=False):
            return [{"name": name} for name in listings.get(self.path, [])]

    original = pipeline.Endpoint
    pipeline.Endpoint = FakeEndpoint
    entry = {"output": {"kind": "local", "path": "/out", "ssh": {}}}
    log = runtime.Log(tempfile.mkdtemp(prefix="v16-log-"), name="t", echo=False)
    try:
        exact, exact_unchecked = pipeline.verify_ledger_hits(
            entry, dict(list(hits.items())[:8]), log, max_dirs=8)
        check("REQ59 目录数正好等于上限时逐个核实",
              len(exact) == 8 and exact_unchecked == 0,
              "%d / %d" % (len(exact), exact_unchecked))
        spilled, spilled_unchecked = pipeline.verify_ledger_hits(entry, hits, log, max_dirs=8)
        check("REQ59 目录数超出上限 -> 没去看的那部分记未核实、不重译",
              len(spilled) == 11 and spilled_unchecked == 11,
              "%d / %d" % (len(spilled), spilled_unchecked))
    finally:
        pipeline.Endpoint = original


def case_mode_switch_keeps_ledger_and_own_output():
    """输出方式从压制切到只出 srt：台账键不变，自家成品两种后缀都排得掉。"""
    cfg = make_cfg()
    files = [{"name": "第02话.mp4", "path": "/src/第02话.mp4",
              "size": 4 * 1024 ** 3, "mtime": 1}]
    for mode in ("burn", "srt"):
        pipeline.record_translated(cfg, "第02话.mp4", "/out/第02话_zh.mp4", mode)
        plan = selection.select(files, cfg, existing_outputs=[], name_template="{stem}_zh.mp4",
                                translated_ledger=pipeline.load_translated(cfg))
        check("REQ59 台账记的是 %s 产物，切到 srt 后同一个键仍命中" % mode,
              plan["counts"]["byReason"].get("translated-ledger") == 1,
              str(plan["counts"]["byReason"]))

    for suffix in ("mp4", "srt"):
        own = [{"name": "第03话_zh." + suffix, "path": "/out/第03话_zh." + suffix,
                "size": 300, "mtime": 2}]
        plan = selection.select(own, cfg, existing_outputs=[], name_template="{stem}_zh.mp4",
                               output_dir="/out")
        check("REQ60 输出目录里的 %s 成品被判为自家成品" % suffix,
              plan["counts"]["byReason"].get("own-output") == 1,
              str(plan["counts"]["byReason"]))


def case_record_only_on_success():
    """失败的一轮绝不能记账 —— 否则那部片子会被永久跳过。"""
    calls = []
    original_record = pipeline.record_translated
    original_skip = pipeline.record_skip

    def spy_record(*args, **kwargs):
        calls.append(("record", args))

    def spy_skip(*args, **kwargs):
        calls.append(("skip", args))

    pipeline.record_translated = spy_record
    pipeline.record_skip = spy_skip
    cfg = make_cfg()
    log = runtime.Log(tempfile.mkdtemp(prefix="v16-log-"), name="t", echo=False)
    entry = {"index": 0, "source": {"path": "/src", "kind": "local"},
             "output": {"path": "/out", "kind": "local", "ssh": {}}}
    pipe = object.__new__(pipeline.Pipeline)
    pipe.cfg = cfg
    pipe.log = log
    pipe.entries = [entry]
    pipe.by_index = {0: {"entry": entry, "source": object(), "output": object()}}

    def boom(*_args, **_kwargs):
        raise pipeline.EngineError(4, "来源不可达（模拟）")

    pipe._download = boom
    try:
        result = pipe.process_one({"name": "第04话.mp4", "path": "/src/第04话.mp4", "dirIndex": 0})
    finally:
        pipeline.record_translated = original_record
        pipeline.record_skip = original_skip
    check("REQ59 处理失败时 result 是 failed", result["status"] == "failed", str(result.get("status")))
    check("REQ59 处理失败时不写台账（不会被永久跳过）",
          [c for c in calls if c[0] == "record"] == [], str(calls))


def main():
    case_events_run()
    case_events_any_output()
    case_gating_is_separate()
    case_execute_notifications()
    case_ledger_roundtrip()
    case_ledger_cap()
    case_select_with_ledger()
    case_verify_ledger_hits()
    case_record_wired_into_process_one()
    case_own_output()
    case_ledger_prefix_multi_dir()
    case_success_when_enum_parity()
    case_verify_partial_beyond_max_dirs()
    case_mode_switch_keeps_ledger_and_own_output()
    case_record_only_on_success()
    case_plan_wiring_real_dirs()
    case_config_validation()
    case_defaults_and_host_fields()
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILURES)))
    if FAILURES:
        print("FAILED:")
        for name in FAILURES:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

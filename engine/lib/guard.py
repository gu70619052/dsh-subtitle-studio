# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 每日守卫（guard）。

守卫回答一个问题：**今天还需要跑吗？** 它是一个幂等的日闸门，同一天被调用
多次最多真跑一轮，跳过时只写一条 state/guard-last.json，不碰用量、不发通知。

判定顺序（先命中先返回）：
1. 已有实例在跑（run.lock 持有者 pid 存活） → skip / already-running
2. 今日用量 count > 0 → skip / already-ran-today
   （count 只统计 ok 与 no-speech，失败不计入，见 pipeline.load_usage）
3. 配了 maxPerRun 且已用完 → skip / quota-exhausted
4. 最近一次运行在今天且全失败 → run / retry-after-failure
5. 今日没有任何记录 → run / no-run-today

守卫本身不含调度：它不写 crontab、不建 LaunchAgent、不 import 任何宿主代码，
launchd / cron / DSH 定时任务谁来调都可以。
"""

import os

from lib.runtime import atomic_write_json, read_json

REASON_TEXT = {
    "already-running": "已有实例在运行",
    "already-ran-today": "今日已处理过",
    "quota-exhausted": "今日配额已用完",
    "retry-after-failure": "今日无成功记录且上次失败，重试",
    "no-run-today": "今日没有任何运行记录",
}


def guard_last_path(cfg):
    return os.path.join(cfg.state_dir, "guard-last.json")


def read_guard_last(cfg):
    data = read_json(guard_last_path(cfg), None)
    return data if isinstance(data, dict) else None


def write_guard_last(cfg, record):
    atomic_write_json(guard_last_path(cfg), record)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def decide(status_data):
    """由 pipeline.status() 的返回值推导本轮动作（纯函数，便于单测）。

    返回 action(ok): {"action": "run"|"skip", "reason": str, "reasonText": str,
                     "today": {...}, "lastRun": {...}}
    """
    today = status_data.get("today") or {}
    last = status_data.get("lastRun") or {}
    count = _int(today.get("count"))
    max_per_run = _int(today.get("maxPerRun"))
    totals = last.get("totals") or {}

    def verdict(action, reason, **extra):
        payload = {
            "action": action,
            "reason": reason,
            "reasonText": REASON_TEXT.get(reason, reason),
            "today": {
                "date": today.get("date"),
                "count": count,
                "maxPerRun": max_per_run,
            },
            "lastRun": {
                "startedAt": last.get("startedAt"),
                "finishedAt": last.get("finishedAt"),
                "totals": totals or None,
            },
        }
        payload.update(extra)
        return payload

    if status_data.get("running"):
        return verdict("skip", "already-running")
    if max_per_run > 0 and count >= max_per_run:
        return verdict("skip", "quota-exhausted")
    if count > 0:
        return verdict("skip", "already-ran-today")
    if last and _int(totals.get("failed")) > 0:
        return verdict("run", "retry-after-failure")
    return verdict("run", "no-run-today")

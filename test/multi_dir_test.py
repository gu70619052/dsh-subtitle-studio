#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕工坊引擎 - 多目录模式自测。

覆盖：
1. 多目录配置通过校验，dir_entries 给出规范化的目录列表；
2. 每个目录都参与选片，候选带 dirIndex / dirName 标签；
3. maxPerRun 是所有目录合计的全局上限；
4. 旧单目录配置零迁移（dirs 为空时回退顶层 source / output）；
5. 非法目录项（缺 source.path、命名模板缺 {stem}）被校验拦下。
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# This script ships inside <package>/test/, so the engine is one level up.
# The fallback covers the historical location under the project's evidence tree.
ENGINE_DIR = os.path.join(HERE, "..", "engine")
if not os.path.isdir(ENGINE_DIR):
    ENGINE_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "dsh-subtitle-studio", "engine"))
ENGINE_DIR = os.path.abspath(ENGINE_DIR)
sys.path.insert(0, ENGINE_DIR)

from lib import config as config_mod  # noqa: E402
from lib import pipeline  # noqa: E402
from lib import select as selection  # noqa: E402
from lib.runtime import Log  # noqa: E402

FAILURES = []
PASSED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("PASS  %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAILURES.append(name)
        print("FAIL  %s%s" % (name, ("  " + detail) if detail else ""))


def write_video(path, size=2048):
    with open(path, "wb") as handle:
        handle.write(b"0" * size)


def base_config(data_dir):
    return {
        "version": 1,
        "engine": {"kind": "dsh", "provider": "fake-provider", "model": "fake-model"},
        "source": {"kind": "local", "path": "", "include": ["*.mp4"], "recursive": False,
                   "ssh": {"host": "", "port": 22, "user": "", "keyPath": ""}},
        "output": {"kind": "local", "path": "", "nameTemplate": "{stem}_zh.mp4",
                   "overwrite": False,
                   "ssh": {"host": "", "port": 22, "user": "", "keyPath": ""}},
        "selection": {"maxPerRun": 2, "skipTranslated": True, "respectSkipList": True,
                      "minSizeGB": 0, "maxSizeGB": 10, "maxAgeDays": 36500,
                      "order": "name-asc", "excludePatterns": []},
        "prompts": {"default": {"system": "翻译成中文", "context": ""}, "profiles": []},
        "subtitles": {"bilingual": False, "trimSilence": True, "maxLineChars": 12,
                      "keepOriginalAsSrt": True, "fontSize": 0, "font": ""},
        "burn": {"enabled": True, "ffmpeg": "", "crf": 20, "preset": "medium",
                 "audio": "copy", "encoder": "libx264", "videoBitrate": "6000k", "fontSize": 0},
        "transcribe": {"whisperModel": "", "language": "", "keepAudio": False,
                       "chunkSeconds": 60, "maxLineMs": 4000, "pauseMs": 500,
                       "silenceDb": -50, "engine": "mlx"},
        "translation": {"rerunThresholdPct": 5, "rerunThresholdCount": 50},
        "notify": {"enabled": False, "channel": "none", "qqAppId": "", "qqOpenId": "",
                   "qqSecretService": "subtitle-studio-qqbot", "qqSendPath": "",
                   "command": "", "webhookUrl": "", "webhookBody": "",
                   "onStart": False, "onSuccess": True, "onFailure": True},
        "schedule": {"enabled": False, "mode": "none", "time": "02:10", "guardTimes": [],
                     "label": "com.subtitle-studio.run"},
        "paths": {"dataDir": data_dir, "keepWorkArtifacts": False},
        "limits": {"minFreeGB": 0},
    }


def main():
    root = tempfile.mkdtemp(prefix="subtitle-multidir-")
    try:
        src_a = os.path.join(root, "lib-a")
        src_b = os.path.join(root, "lib-b")
        out_a = os.path.join(root, "out-a")
        out_b = os.path.join(root, "out-b")
        for path in (src_a, src_b, out_a, out_b):
            os.makedirs(path, exist_ok=True)
        write_video(os.path.join(src_a, "a1.mp4"))
        write_video(os.path.join(src_a, "a2.mp4"))
        write_video(os.path.join(src_b, "b1.mp4"))

        config_path = os.path.join(root, "config.json")
        config = base_config(os.path.join(root, "data"))
        config["dirs"] = [
            {"name": "片库 A",
             "source": {"kind": "local", "path": src_a, "include": ["*.mp4"], "recursive": False},
             "output": {"kind": "local", "path": out_a, "nameTemplate": "{stem}_zh.mp4",
                        "overwrite": False}},
            {"name": "片库 B",
             "source": {"kind": "local", "path": src_b, "include": ["*.mp4"], "recursive": False},
             "output": {"kind": "local", "path": out_b, "nameTemplate": "{stem}_cn.mp4",
                        "overwrite": True}},
        ]
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)

        cfg = config_mod.load_config(config_path)
        check("multi-dir config validates", not cfg.errors, json.dumps(cfg.errors, ensure_ascii=False))

        entries = config_mod.dir_entries(cfg.data)
        check("dir_entries returns both directories", len(entries) == 2, str([e["name"] for e in entries]))
        check("dir entries keep their own output path",
              entries[0]["output"]["path"] == out_a and entries[1]["output"]["path"] == out_b)
        check("dir entries keep their own name template",
              entries[1]["output"]["nameTemplate"] == "{stem}_cn.mp4")

        log = Log(None, "multidir-test", echo=False)
        plan = pipeline.build_plan(cfg, log)
        names = sorted(candidate["name"] for candidate in plan["candidates"])
        check("plan lists files from every directory", names == ["a1.mp4", "a2.mp4", "b1.mp4"], str(names))
        dirs_seen = sorted({candidate["dirName"] for candidate in plan["candidates"]})
        check("candidates carry their directory label", dirs_seen == ["片库 A", "片库 B"], str(dirs_seen))
        check("plan reports multiDir", plan.get("multiDir") is True)
        check("plan reports each directory", len(plan.get("dirs") or []) == 2)
        check("maxPerRun is a global cap across directories",
              len(plan["selectedDetail"]) == 2, str(len(plan["selectedDetail"])))
        expected_output = {c["name"]: c["expectedOutput"] for c in plan["candidates"]}
        check("name template is per directory",
              expected_output.get("b1.mp4") == "b1_cn.mp4"
              and expected_output.get("a1.mp4") == "a1_zh.mp4", json.dumps(expected_output, ensure_ascii=False))

        # 不限额：三个都选中
        config["selection"]["maxPerRun"] = 0
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        cfg_all = config_mod.load_config(config_path)
        plan_all = pipeline.build_plan(cfg_all, log)
        check("maxPerRun=0 selects every file", len(plan_all["selectedDetail"]) == 3,
              str(len(plan_all["selectedDetail"])))

        # 旧单目录配置零迁移
        legacy_path = os.path.join(root, "legacy.json")
        legacy = base_config(os.path.join(root, "data2"))
        legacy["source"]["path"] = src_a
        legacy["output"]["path"] = out_a
        with open(legacy_path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle, ensure_ascii=False, indent=2)
        legacy_cfg = config_mod.load_config(legacy_path)
        legacy_entries = config_mod.dir_entries(legacy_cfg.data)
        check("legacy config falls back to one directory", len(legacy_entries) == 1)
        check("legacy entry keeps top-level source/output",
              legacy_entries[0]["source"]["path"] == src_a
              and legacy_entries[0]["output"]["path"] == out_a)
        legacy_plan = pipeline.build_plan(legacy_cfg, log)
        check("legacy plan still works",
              sorted(c["name"] for c in legacy_plan["candidates"]) == ["a1.mp4", "a2.mp4"])
        check("legacy plan is not flagged as multi-dir", legacy_plan.get("multiDir") is False)

        # 非法目录项被校验拦下
        bad = base_config(os.path.join(root, "data3"))
        bad["dirs"] = [{"name": "坏目录",
                        "source": {"kind": "local", "path": "", "include": ["*.mp4"]},
                        "output": {"kind": "local", "path": out_a, "nameTemplate": "no-stem.mp4"}}]
        issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, bad))
        paths = {issue["path"] for issue in issues if issue["level"] == "error"}
        check("missing source path is rejected", "dirs[0].source.path" in paths, str(sorted(paths)))
        check("name template without {stem} is rejected", "dirs[0].output.nameTemplate" in paths,
              str(sorted(paths)))

        # 两个目录指向同一个输出目录 → warn
        dup = base_config(os.path.join(root, "data4"))
        dup["dirs"] = [
            {"name": "A", "source": {"kind": "local", "path": src_a, "include": ["*.mp4"]},
             "output": {"kind": "local", "path": out_a, "nameTemplate": "{stem}_zh.mp4"}},
            {"name": "B", "source": {"kind": "local", "path": src_b, "include": ["*.mp4"]},
             "output": {"kind": "local", "path": out_a, "nameTemplate": "{stem}_zh.mp4"}},
        ]
        dup_issues = config_mod.validate(config_mod.deep_merge(config_mod.DEFAULTS, dup))
        dup_warns = [i for i in dup_issues
                     if i["level"] == "warn" and i["path"] == "dirs[1].output.path"]
        check("duplicate output directory is warned", len(dup_warns) == 1,
              json.dumps(dup_issues, ensure_ascii=False))

        # work 目录隔离：不同目录的同名文件不能共用工作目录
        same_a = os.path.join(root, "same-a")
        same_b = os.path.join(root, "same-b")
        for path in (same_a, same_b):
            os.makedirs(path, exist_ok=True)
        write_video(os.path.join(same_a, "same.mp4"))
        write_video(os.path.join(same_b, "same.mp4"))
        salt_cfg = base_config(os.path.join(root, "data5"))
        salt_cfg["dirs"] = [
            {"name": "A", "source": {"kind": "local", "path": same_a, "include": ["*.mp4"]},
             "output": {"kind": "local", "path": out_a, "nameTemplate": "{stem}_zh.mp4"}},
            {"name": "B", "source": {"kind": "local", "path": same_b, "include": ["*.mp4"]},
             "output": {"kind": "local", "path": out_b, "nameTemplate": "{stem}_zh.mp4"}},
        ]
        salt_path = os.path.join(root, "salt.json")
        with open(salt_path, "w", encoding="utf-8") as handle:
            json.dump(salt_cfg, handle, ensure_ascii=False, indent=2)
        salt_entries = config_mod.dir_entries(config_mod.load_config(salt_path).data)
        work_a = pipeline.work_dir_for(config_mod.load_config(salt_path), "same.mp4",
                                       pipeline._dir_salt(salt_entries[0]))
        work_b = pipeline.work_dir_for(config_mod.load_config(salt_path), "same.mp4",
                                       pipeline._dir_salt(salt_entries[1]))
        check("work dirs differ across directories for same filename", work_a != work_b,
              "%s vs %s" % (work_a, work_b))

        # 失败名单按目录隔离
        legacy_list = {"same.mp4": {"reason": "no-speech"}}
        check("legacy skip entry still applies to every directory",
              selection._in_skip_list(legacy_list, "same.mp4", "[0] "))
        isolated = {"[1] same.mp4": {"reason": "no-speech"}}
        check("prefixed skip entry only applies to its own directory",
              selection._in_skip_list(isolated, "same.mp4", "[1] ")
              and not selection._in_skip_list(isolated, "same.mp4", "[0] "))

        # ---- Round B：名单键按来源稳定，不随面板增删/重排错位
        bcfg = config_mod.load_config(salt_path)
        os.makedirs(bcfg.state_dir, exist_ok=True)
        skip_path = os.path.join(bcfg.state_dir, "skipped.json")
        tag_first = pipeline.dir_tag(salt_entries[0])
        check("skip tag is derived from the source, not the position",
              tag_first.startswith("[") and len(tag_first) == 11, tag_first)
        with open(skip_path, "w", encoding="utf-8") as handle:
            json.dump({"[0] same.mp4": {"reason": "no-speech", "detail": "x"}}, handle)
        migrated = pipeline.migrate_skip_keys(bcfg, salt_entries)
        check("positional skip key migrated to the stable tag",
              list(migrated.keys()) == [tag_first + "same.mp4"], str(list(migrated.keys())))
        check("migration is idempotent", pipeline.migrate_skip_keys(bcfg, salt_entries) == migrated)
        check("migration rewritten to disk",
              list(json.load(open(skip_path, encoding="utf-8")).keys()) == [tag_first + "same.mp4"])

        reordered_cfg = json.loads(json.dumps(salt_cfg))
        reordered_cfg["dirs"] = list(reversed(reordered_cfg["dirs"]))
        reordered_path = os.path.join(root, "reordered.json")
        with open(reordered_path, "w", encoding="utf-8") as handle:
            json.dump(reordered_cfg, handle, ensure_ascii=False)
        reordered_entries = config_mod.dir_entries(config_mod.load_config(reordered_path).data)
        check("after a reorder the tag still names the same source",
              pipeline.dir_tag(reordered_entries[1]) == tag_first
              and pipeline.dir_tag(reordered_entries[0]) != tag_first)
        check("the recorded failure follows its own directory after a reorder",
              selection._in_skip_list(migrated, "same.mp4", pipeline.dir_tag(reordered_entries[1]))
              and not selection._in_skip_list(migrated, "same.mp4", pipeline.dir_tag(reordered_entries[0])))

        # ---- Round B：单目录不加盐，升级后旧中间产物仍可续跑
        import hashlib
        single_cfg = base_config(os.path.join(root, "data6"))
        single_cfg["source"]["path"] = same_a
        single_cfg["output"]["path"] = os.path.join(root, "out6")
        single_path = os.path.join(root, "single.json")
        with open(single_path, "w", encoding="utf-8") as handle:
            json.dump(single_cfg, handle, ensure_ascii=False)
        single_loaded = config_mod.load_config(single_path)
        single_log = Log(os.path.join(root, "single.log"))
        single_plan = pipeline.build_plan(single_loaded, single_log)
        single_preview = pipeline.dry_run_plan(single_loaded, single_log, single_plan)
        unsalted = "same-%s" % hashlib.md5(b"same.mp4").hexdigest()[:8]
        actions = single_preview.get("actions") or []
        check("single-directory work dir keeps the pre-1.3 unsalted name",
              bool(actions) and os.path.basename(actions[0].get("workDir", "")) == unsalted,
              json.dumps(actions[0].get("workDir") if actions else None))

        # ---- Round B：预演的 download 阶段跟随各目录自己的来源类型
        mixed_cfg = base_config(os.path.join(root, "data7"))
        mixed_cfg["dirs"] = [
            {"name": "本地", "source": {"kind": "local", "path": same_a, "include": ["*.mp4"]},
             "output": {"kind": "local", "path": os.path.join(root, "out7"),
                        "nameTemplate": "{stem}_zh.mp4"}},
        ]
        mixed_path = os.path.join(root, "mixed.json")
        with open(mixed_path, "w", encoding="utf-8") as handle:
            json.dump(mixed_cfg, handle, ensure_ascii=False)
        mixed_loaded = config_mod.load_config(mixed_path)
        mixed_plan = pipeline.build_plan(mixed_loaded, Log(os.path.join(root, "mixed.log")))
        mixed_preview = pipeline.dry_run_plan(mixed_loaded, Log(os.path.join(root, "mixed.log")), mixed_plan)
        local_actions = mixed_preview.get("actions") or []
        check("local directory previews use-local-file, not download",
              bool(local_actions) and "use-local-file" in (local_actions[0].get("stages") or []),
              json.dumps(local_actions[0].get("stages") if local_actions else None))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n%d checks passed, %d failed" % (len(PASSED), len(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

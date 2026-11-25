#!/usr/bin/env python3
"""Probe the DSH relay from the engine side.

The host publishes an endpoint descriptor (baseUrl + token path) and answers
``POST <baseUrl><bridgePath>`` with the model's text. This probe calls the very
same helper the engine uses, so a green run proves the Python half and the
host half agree on the contract.

Usage: python3 bridge_probe.py <engine/vendor/dir> <provider> <model>
Prints one JSON line: {"ok": bool, "text": str}
"""
import json
import os
import sys

VENDOR = sys.argv[1] if len(sys.argv) > 1 else ""
PROVIDER = sys.argv[2] if len(sys.argv) > 2 else "fake-provider"
MODEL = sys.argv[3] if len(sys.argv) > 3 else "fake-model"

sys.path.insert(0, VENDOR)
from translate_srt import _bridge_endpoint, _call_dsh_bridge  # noqa: E402

endpoint = _bridge_endpoint()
if not endpoint:
    print(json.dumps({"ok": False, "text": "", "error": "no endpoint"}, ensure_ascii=False))
    sys.exit(2)

text, usage = _call_dsh_bridge("system prompt", "hello", PROVIDER, MODEL)
print(json.dumps({"ok": bool(text), "text": text or "", "usage": usage}, ensure_ascii=False))
sys.exit(0 if text else 1)

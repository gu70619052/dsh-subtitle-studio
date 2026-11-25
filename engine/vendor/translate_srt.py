#!/usr/bin/env python3
"""
SRT 翻译脚本 — 字幕润色与翻译规则集

润色规则分为 6+2 大类：
  1. 纠正 ASR 识别错误（可选：传 --video-context 让 LLM 看视频帧辅助纠错）
  2. 标点与书面化（去标点、中英之间加空格）
  3. 去冗余（去"那个/呃/嗯"等）
  4. 时间戳对齐（严格保留原始时间戳，禁止合并相邻条目）
  5. 断句与分段（≤12字拆条，按语义自然位置）
  6. 专有名词处理（人名/地名/品牌保留原文，术语保留英文）
  7. 语言风格（口语化、当代年轻用语、避免翻译腔）
  8. 歌词处理（用 ♪ 包裹、注重押韵）

支持单/双语义：
  --bilingual  产出"双语 SRT"（中文上+原文下），供 bilingual_ass.py 转 ASS 烧录

支持双引擎：MiniMax M3（推荐，2026-06-11 起默认）+ DeepSeek
"""
import os
import re
import sys
import time
import json
import subprocess
import tempfile
import shutil

# ============== 字幕润色与翻译规则集 ==============
SYSTEM_PROMPT_ZH = """你是专业日语→简体中文字幕翻译。
输入是 SRT 字幕, 每条有编号、时间戳、日文文本。
你的任务: 逐条翻译成简体中文。

## 绝对规则 (违反视为失败)

1. **必须输出中文**: 每条字幕的"输出文本"必须是简体中文。不能输出日文原文,不能保留日文,不能"修正"日文。
2. **数量严格 1:1**: 输入 N 条, 输出 N 条
3. **保留 idx 和时间戳**: 第 N 条的编号 = 输入的第 N 条编号
4. **占位符原样回传**: 如果输入是 [听不清] / [广告宣番] / [♪ 歌词 ♪], 输出原样, 不要翻译也不要删除
5. **直接输出 SRT, 不要任何解释**

## 翻译风格

- 保留专有名词原文（人名、社团名、作品名等，不音译）
- 单条不超过 12 中文字符
- 歌词用 ♪ 包裹: ♪ 歌词中文翻译 ♪
- 口语自然, 避免翻译腔
- 数字/英文和中文间加英文空格
- 输出无标点
"""

# 双语模式：保留原文 + 译文
SYSTEM_PROMPT_BILINGUAL = SYSTEM_PROMPT_ZH + """

## ⭐ 双语模式特殊规则

每条字幕保留两行内容：中文译文在上、原文（修正 ASR 错误后）在下，时间戳与原文一句对一句完全对齐（不重新断句、不合并条目）。

格式示例：
```
1
00:00:19,239 --> 00:00:21,239
大家好吗
Hello everyone, how are you?
```

英文行保留原文（仅修正 ASR 识别错误，如 Claude 被听成 cloud、MCP 被听成 NCP）。
"""

BILINGUAL_SUFFIX = SYSTEM_PROMPT_BILINGUAL[len(SYSTEM_PROMPT_ZH):]

# ============== 工具函数 ==============
def parse_srt(content):
    blocks = []
    for blk in re.split(r'\n\s*\n', content.strip()):
        lines = blk.strip().split("\n")
        if len(lines) < 2: continue
        if not re.match(r'^\d+$', lines[0].strip()): continue
        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', lines[1])
        if not m: continue
        text = "\n".join(lines[2:]) if len(lines) > 2 else ""
        blocks.append({
            "idx": int(lines[0].strip()),
            "start": m.group(1),
            "end": m.group(2),
            "text": text.strip(),
        })
    return blocks

def _strip_bilingual(text):
    """v2.12: 清洗 LLM 双语输出, 单语模式下只取中文
    场景: LLM 误输出"日+中"两行(或"中+日"两行), 提取中文行
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) <= 1:
        return text
    ja_re = re.compile(r'[぀-ヿ]')
    zh_re = re.compile(r'[一-鿿]')
    # 场景1: 上行日文 + 下行中文 (双语模式约定) → 取中文
    if len(lines) == 2 and ja_re.search(lines[0]) and zh_re.search(lines[1]):
        return lines[1]
    # 场景2: 上行中文 + 下行日文 → 取中文
    if len(lines) == 2 and zh_re.search(lines[0]) and ja_re.search(lines[1]):
        return lines[0]
    # 场景3: 多行, 过滤掉纯日文行, 保留中文行
    zh_lines = [l for l in lines if zh_re.search(l)]
    if zh_lines:
        return '\n'.join(zh_lines)
    # 场景4: 全是日文 (LLM 完全没翻) → 保留原文
    return text

# ===== 生成参数（由 CLI 参数或 SUBTITLE_* 环境变量注入，见 main）=====
# 放在模块级，供 translate_batch / _call_mmx / _call_ollama / _retrain_with_engine 共用，
# 避免把 5 个参数一路透传穿 4 层函数。
GEN_OPTS = {
    "temperature": float(os.environ.get("SUBTITLE_TEMPERATURE") or 0.1),
    "seed": int(os.environ.get("SUBTITLE_SEED") or 42),
    "max_tokens": int(os.environ.get("SUBTITLE_MAX_TOKENS") or 8000),
    "batch_size": int(os.environ.get("SUBTITLE_BATCH_SIZE") or 30),
    "ollama_host": os.environ.get("SUBTITLE_OLLAMA_HOST") or "http://127.0.0.1:11434",
}

def format_time(sec):
    if sec < 0: sec = 0
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def parse_time(ts):
    m = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', ts)
    h, mn, s, ms = map(int, m.groups())
    return h*3600 + mn*60 + s + ms/1000

def translate_batch(batch, key, model="MiniMax-M3", bilingual=False, video_context=None, engine="minimax", provider=""):
    """调 LLM 翻译一批字幕
    engine: "dsh" (经 DSH 宿主中转) | "minimax" (mmx CLI) | "ollama" (本地 ollama)
    provider: 仅 engine="dsh" 用，指定 DSH 侧的模型提供方
    """
    srt_text = "\n\n".join(
        f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in batch
    )
    
    system = SYSTEM_PROMPT_BILINGUAL if bilingual else SYSTEM_PROMPT_ZH
    
    # 视频上下文（ASR 纠错辅助）：来自 video.md 截帧分析
    user_parts = []
    if video_context:
        user_parts.append(f"## 视频上下文（用于 ASR 纠错）\n{video_context}\n\n")
    user_parts.append(f"## 待翻译 SRT 字幕\n{srt_text}\n\n")
    user_parts.append("直接输出 SRT 格式结果，不要任何解释。")
    user_prompt = "".join(user_parts)
    
    if engine == "dsh":
        return _call_dsh_bridge(system, user_prompt, provider, model)
    if engine == "ollama":
        return _call_ollama(system, user_prompt, model)
    if engine in ("deepseek", "openai-compatible"):
        return _call_openai_compatible(system, user_prompt, model, key)
    return _call_mmx(system, user_prompt, model, key)


def _bridge_endpoint():
    """读取宿主发布的中转端点描述文件。

    端点里只有 DSH Web GUI 的地址与令牌文件路径，没有凭据；地址每次调用现读，
    DSH 换端口也不会让引擎拿着旧地址撞墙。
    """
    raw = os.environ.get("SUBTITLE_STUDIO_BRIDGE")
    if not raw:
        base = os.environ.get("SUBTITLE_STUDIO_DATA_DIR") or os.path.join(
            os.path.expanduser("~"), ".dsh", "data", "subtitle-studio")
        raw = os.path.join(base, "state", "bridge.json")
    try:
        with open(raw, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        print("    ⚠️ 读不到 DSH 中转端点(%s): %s" % (raw, exc), flush=True)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _is_loopback_host(host):
    """回环判定：loopback 才允许把字幕正文发出去。"""
    if not host:
        return False
    host = host.strip().lower().strip("[]")
    if host in ("localhost", "::1", "0:0:0:0:0:0:0:1"):
        return True
    if host.startswith("127."):
        parts = host.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    return False


def _call_dsh_bridge(system, user_prompt, provider, model):
    """经 DSH 宿主中转调用模型。

    凭据、路由、计费都在 DSH 那一侧，引擎只发文本；因此这把钥匙只在本机
    DSH 与引擎之间流转，插件不接触任何 API key。
    """
    import urllib.error
    import urllib.request

    endpoint = _bridge_endpoint()
    if not endpoint:
        return None, {}
    base = str(endpoint.get("baseUrl") or "").rstrip("/")
    bridge_path = str(endpoint.get("bridgePath") or "/api/dsh-subtitle-studio/llm")
    token_path = str(endpoint.get("tokenPath") or "")
    try:
        with open(token_path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except Exception as exc:  # noqa: BLE001
        print("    ⚠️ 读不到中转令牌: %s" % exc, flush=True)
        return None, {}
    if not base or not token:
        print("    ⚠️ 中转端点信息不完整", flush=True)
        return None, {}
    if not provider or not model:
        print("    ⚠️ DSH 中转需要 provider 与 model", flush=True)
        return None, {}

    # 端点文件是同用户可写的，万一被改到外部地址，字幕正文（和令牌）就发出去了
    import urllib.parse
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not _is_loopback_host(parsed.hostname or ""):
        print("    ⚠️ 中转端点不是本机回环地址，拒绝发送字幕内容: %s" % base, flush=True)
        return None, {}
    if ".." in bridge_path or bridge_path.startswith("//"):
        print("    ⚠️ 中转路径异常: %s" % bridge_path, flush=True)
        return None, {}

    url = base + bridge_path
    payload = {
        "provider": provider,
        "model": model,
        "system": system,
        "prompt": user_prompt,
        "temperature": GEN_OPTS["temperature"],
        "maxTokens": GEN_OPTS["max_tokens"],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Subtitle-Studio-Token": token,
    }
    # 4xx 里的这些是"再问一遍也一样的"：重试只是三倍计费
    fatal_codes = {"unauthorized", "forbidden", "not-found", "bad-body",
                   "bad-content-type", "payload-too-large", "invalid-request",
                   "unknown-provider", "unknown-model"}
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=300) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
            if body.get("ok"):
                text = str((body.get("data") or {}).get("text") or "")
                if text.strip():
                    return text.strip(), {"total_tokens": 0}
                print("    ⚠️ DSH 返回空内容", flush=True)
            else:
                code = str(body.get("code") or "")
                message = str(body.get("error"))[:200]
                print("    ⚠️ DSH 调用失败: %s" % message, flush=True)
                if code in fatal_codes or "is required" in message or "unknown" in code:
                    print("    ↳ 配置类错误，不再重试", flush=True)
                    return None, {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            print("    ⚠️ DSH 中转返回 %d: %s" % (exc.code, detail), flush=True)
            if exc.code == 429:
                time.sleep(20 + attempt * 20)
                continue
            if 400 <= exc.code < 500:
                print("    ↳ 请求本身被拒绝，不再重试", flush=True)
                return None, {}
        except Exception as exc:  # noqa: BLE001
            print("    ⚠️ DSH 调用异常(%d/3): %s" % (attempt + 1, exc), flush=True)
        if attempt < 2:
            time.sleep(2 + attempt * 2)
    return None, {}


def _call_ollama(system, user_prompt, model):
    """调本地 ollama (http://127.0.0.1:11434/api/generate)"""
    # ollama generate API 用单一 prompt（system + user 拼一起）
    full_prompt = f"{system}\n\n{user_prompt}"
    
    for attempt in range(3):
        try:
            payload = {
                "model": model,
                "prompt": full_prompt,
                "stream": False,
                # 思考模型（qwen3 一类）会把整段预算烧在 thinking 上、response 留空。
                # 新版 ollama 用这个字段关掉思考；老版不认，直接忽略，行为不变。
                "think": False,
                "options": {
                    "temperature": GEN_OPTS["temperature"],
                    "num_predict": GEN_OPTS["max_tokens"],
                    "seed": GEN_OPTS["seed"],
                }
            }
            r = subprocess.run(
                ["curl", "-s", "-X", "POST", GEN_OPTS["ollama_host"].rstrip("/") + "/api/generate",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload, ensure_ascii=False)],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode != 0:
                print(f"    ⚠️ curl failed: {r.stderr[:200]}", flush=True)
                time.sleep(2 + attempt*2)
                continue
            d = json.loads(r.stdout)
            output = d.get("response", "").strip()
            if output:
                eval_count = d.get("eval_count", 0)
                return output, {"total_tokens": eval_count}
            # v2.10 调试: ollama 返回了但 response 字段空 — 查看 error 字段
            err = d.get("error", "")
            if err:
                print(f"    ⚠️ ollama error: {err[:200]}, 重试 ({attempt+1}/3)", flush=True)
            elif d.get("thinking", "").strip():
                # 只有 thinking 没有正文：重试也一定是同样结果，别白烧三遍
                print("    ⚠️ 该模型只输出了 thinking 没有正文（思考模型），换非思考模型或改走 DSH 中转", flush=True)
                return None, {}
            else:
                print(f"    ⚠️ ollama 返回空 (response+error 都空), stdout={r.stdout[:200]}, 重试 ({attempt+1}/3)", flush=True)
            time.sleep(2 + attempt*2)
        except subprocess.TimeoutExpired:
            print(f"    ⚠️ ollama 超时, 重试 ({attempt+1}/3)", flush=True)
            time.sleep(3)
        except Exception as e:
            print(f"    ⚠️ ollama 异常: {e}, 重试 ({attempt+1}/3)", flush=True)
            time.sleep(2 + attempt*2)
    return None, {}


def _call_openai_compatible(system, user_prompt, model, key):
    """调 OpenAI 兼容接口（DeepSeek / 任意 self-host baseUrl）。

    baseUrl 取 SUBTITLE_BASE_URL / DEEPSEEK_BASE_URL / OPENAI_BASE_URL，
    缺省 https://api.deepseek.com；key 为空时直接失败。
    """
    import urllib.error
    import urllib.request

    if not key:
        return None, {}
    base = (os.environ.get("SUBTITLE_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.deepseek.com").rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": GEN_OPTS["temperature"],
        "max_tokens": GEN_OPTS["max_tokens"],
        "stream": False,
    }
    if GEN_OPTS.get("seed") is not None:
        payload["seed"] = GEN_OPTS["seed"]
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + key}
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=180) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if content.strip():
                return content.strip(), {"total_tokens": (body.get("usage") or {}).get("total_tokens", 0)}
            print("    ⚠️ 兼容接口返回空内容", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("    ⚠️ 兼容接口调用失败(%d/3): %s" % (attempt + 1, exc), flush=True)
        time.sleep(2 + attempt * 2)
    return None, {}


def _call_mmx(system, user_prompt, model, key):
    """调 mmx (MiniMax CLI) 翻译"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(messages, tf, ensure_ascii=False)
        msg_file = tf.name
    
    try:
        env = os.environ.copy()
        if key:
            env["MINIMAX_API_KEY"] = key
        for attempt in range(3):
            try:
                r = subprocess.run(
                    ["mmx", "text", "chat", "--messages-file", msg_file,
                     "--model", model, "--max-tokens", str(GEN_OPTS["max_tokens"]),
                     "--temperature", str(GEN_OPTS["temperature"]),
                     "--seed", str(GEN_OPTS["seed"]),
                     "--non-interactive", "--quiet"],
                    env=env, capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    return r.stdout.strip(), {"total_tokens": 0}
                if r.returncode == 4:
                    raise RuntimeError("MiniMax 配额用完")
                time.sleep(3 + attempt*2)
            except subprocess.TimeoutExpired:
                time.sleep(3)
        return None, {}
    finally:
        try: os.unlink(msg_file)
        except: pass

def _retrain_with_engine(blocks, model, bilingual, video_context):
    """v2.10: 用指定 engine 重跑整段翻译（自适应 fallback 调）
    返回 [(idx, start, end, text), ...]
    """
    records = []
    batch_size = GEN_OPTS["batch_size"]  # 与 main 保持一致（由 CLI/env 注入）
    batches = [blocks[i:i+batch_size] for i in range(0, len(blocks), batch_size)]
    for i, batch in enumerate(batches):
        t0 = time.time()
        result, _ = _call_ollama(
            SYSTEM_PROMPT_BILINGUAL if bilingual else SYSTEM_PROMPT_ZH,
            _build_user_prompt(batch, video_context),
            model
        )
        dt = time.time() - t0
        if result is None:
            print(f"    [{i+1}/{len(batches)}] ollama FAILED in {dt:.1f}s, 保留原文", flush=True)
            for b in batch:
                records.append((b['idx'], b['start'], b['end'], b['text']))
        else:
            translated_blocks = parse_srt(result)
            n_t, n_o = len(translated_blocks), len(batch)
            if n_t != n_o:
                print(f"    [{i+1}/{len(batches)}] ollama OK in {dt:.1f}s (n_t={n_t}!=n_o={n_o}, 保留原文)", flush=True)
                for b in batch:
                    records.append((b['idx'], b['start'], b['end'], b['text']))
            else:
                # 严格 1:1 对应（ollama 会重新分配时间戳，但这里强制 1:1 以保持原时序）
                print(f"    [{i+1}/{len(batches)}] ollama OK in {dt:.1f}s (1:1 对应)", flush=True)
                for j, o_block in enumerate(batch):
                    tb = translated_blocks[j]
                    records.append((tb['idx'], o_block['start'], o_block['end'], tb['text']))
    return records


def _build_user_prompt(batch, video_context):
    """拼装 user prompt (从 main 抽出来便于复用)"""
    srt_text = "\n\n".join(
        f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in batch
    )
    parts = []
    if video_context:
        parts.append(f"## 视频上下文（用于 ASR 纠错）\n{video_context}\n\n")
    parts.append(f"## 待翻译 SRT 字幕\n{srt_text}\n\n")
    parts.append("直接输出 SRT 格式结果，不要任何解释。")
    return "".join(parts)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="SRT 翻译（基于内置字幕润色与翻译规则集）")
    parser.add_argument("input", help="输入 SRT")
    parser.add_argument("output", help="输出 SRT")
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("SUBTITLE_BATCH_SIZE") or 30))
    parser.add_argument("--model", default=None,
                        help="LLM 模型名（默认取 SUBTITLE_MODEL / SUBTITLE_DEEPSEEK_MODEL / --ollama-model）")
    parser.add_argument("--temperature", type=float, default=float(os.environ.get("SUBTITLE_TEMPERATURE") or 0.1))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("SUBTITLE_SEED") or 42))
    parser.add_argument("--max-tokens", type=int, default=int(os.environ.get("SUBTITLE_MAX_TOKENS") or 8000),
                        dest="max_tokens")
    parser.add_argument("--ollama-host", default=os.environ.get("SUBTITLE_OLLAMA_HOST") or "http://127.0.0.1:11434")
    parser.add_argument("--system-file", dest="system_file", default=None,
                       help="用文件内容覆盖内置 system 提示词（config.prompts）")
    parser.add_argument("--engine", choices=["dsh", "minimax", "deepseek", "ollama", "openai-compatible"],
                       default="minimax",
                       help="LLM 引擎: dsh (经 DSH 宿主中转) | minimax (mmx CLI) | deepseek | ollama (本地 ollama)")
    parser.add_argument("--provider", default=os.environ.get("SUBTITLE_PROVIDER") or "",
                       help="--engine dsh 时使用的 DSH 模型提供方（填你在 DSH 里配的 provider 名）")
    parser.add_argument("--ollama-model", default=os.environ.get("SUBTITLE_OLLAMA_MODEL") or "",
                       help="--engine ollama 时的覆盖模型名；留空用 --model")
    parser.add_argument("--bilingual", action="store_true", help="产出双语 SRT（中文上+原文下）")
    parser.add_argument("--video-context", help="ASR 纠错上下文（视频前 30 秒的场景描述）")
    parser.add_argument("--series", default=None,
                       help="术语表名字，对应 ~/.dsh/data/subtitle-studio/series/<名字>.json（见 vendor/series.example.json）")
    args = parser.parse_args()
    
    in_path = args.input
    out_path = args.output
    out_tmp = out_path + ".tmp"
    progress_file = out_path + ".progress"
    batch_size = args.batch_size
    GEN_OPTS.update({
        "temperature": args.temperature,
        "seed": args.seed,
        "max_tokens": args.max_tokens,
        "batch_size": args.batch_size,
        "ollama_host": args.ollama_host,
    })
    bilingual = args.bilingual
    video_context = args.video_context
    series = args.series

    # 提示词全部可由 config.prompts 注入（--system-file），内置提示词仅是兜底
    if getattr(args, "system_file", None):
        try:
            override = open(args.system_file, "r", encoding="utf-8").read().strip()
        except OSError as exc:
            print(f"⚠️ 读取 --system-file 失败: {exc}", flush=True)
            override = ""
        if override:
            globals()["SYSTEM_PROMPT_ZH"] = override
            globals()["SYSTEM_PROMPT_BILINGUAL"] = override + BILINGUAL_SUFFIX
            print(f"提示词: 使用 {args.system_file}（{len(override)} 字）", flush=True)

    # 加载可选的系列词表（按 --series 动态加载，不同系列独立维护术语）
    if series:
        glossary = ""
        try:
            import series_context
            glossary = series_context.get_context(series)
        except Exception as exc:  # 词表只是加分项，坏了不能拖垮翻译
            print(f"⚠️ 术语表 {series} 不可用（{exc}）", flush=True)
        if glossary:
            video_context = f"{video_context}\n\n{glossary}" if video_context else glossary
            print(f"已加载术语表 {series}（{series_context.series_dir()}）", flush=True)

    # 选引擎
    engine = args.engine
    provider = str(args.provider or "").strip()
    if engine == "dsh":
        # DSH 中转：凭据在宿主手里，引擎不接触任何 key
        key = ""
        model = args.model or os.environ.get("SUBTITLE_MODEL") or ""
    elif engine == "minimax":
        key = os.environ.get("MINIMAX_API_KEY", "")
        if not key:
            # Only an explicitly named keychain item is read; the plugin
            # ships no default service name.
            ref = os.environ.get("SUBTITLE_API_KEYREF", "").strip()
            if ref:
                try:
                    key = subprocess.check_output(
                        ["security", "find-generic-password", "-s", ref, "-w"],
                        stderr=subprocess.DEVNULL).decode("utf-8").strip()
                except: pass
        model = args.model or os.environ.get("SUBTITLE_MODEL") or ""
    elif engine in ("deepseek", "openai-compatible"):
        key = (os.environ.get("SUBTITLE_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "")
        if not key:
            ref = os.environ.get("SUBTITLE_API_KEYREF") or (
                "DEEPSEEK_API_KEY" if engine == "deepseek" else "SUBTITLE_API_KEY")
            try:
                key = subprocess.check_output(
                    ["security", "find-generic-password", "-s", ref, "-w"],
                    stderr=subprocess.DEVNULL).decode("utf-8").strip()
            except Exception:
                pass
        model = args.model or os.environ.get("SUBTITLE_DEEPSEEK_MODEL") or "deepseek-chat"
    else:  # ollama
        key = ""  # ollama 不需要 key
        model = args.model or args.ollama_model
    
    print(f"引擎: {engine} ({model}), 双语: {bilingual}, 视频上下文: {'有' if video_context else '无'}", flush=True)
    if provider:
        print(f"提供方: {provider}", flush=True)
    print(f"参数: temperature={GEN_OPTS['temperature']} seed={GEN_OPTS['seed']} "
          f"max_tokens={GEN_OPTS['max_tokens']} batch={GEN_OPTS['batch_size']} "
          f"ollama={GEN_OPTS['ollama_host']}", flush=True)
    print(f"Key length: {len(key)}", flush=True)
    
    # 读 SRT
    content = open(in_path, "r", encoding="utf-8").read()
    blocks = parse_srt(content)
    print(f"总块数: {len(blocks)}", flush=True)
    
    # 断点恢复
    start_batch = 0
    translated_records = []
    if os.path.exists(progress_file):
        try:
            data = json.load(open(progress_file))
            start_batch = data.get("next_batch", 0)
            translated_records = data.get("records", [])
            print(f"恢复: 已完成 {start_batch} 批", flush=True)
        except: pass
    
    batches = [blocks[i:i+batch_size] for i in range(0, len(blocks), batch_size)]
    print(f"分 {len(batches)} 批 (size={batch_size}), 从 {start_batch+1} 开始", flush=True)
    
    total_tokens = 0
    for i in range(start_batch, len(batches)):
        batch = batches[i]
        t0 = time.time()
        first_idx, last_idx = batch[0]['idx'], batch[-1]['idx']
        print(f"  [{i+1}/{len(batches)}] blocks {first_idx}-{last_idx} ...", end=" ", flush=True)
        
        result, usage = translate_batch(batch, key, model, bilingual, video_context,
                                        engine=engine, provider=provider)
        dt = time.time() - t0
        
        if result is None:
            print(f"FAILED in {dt:.1f}s, 保留原文", flush=True)
            for b in batch:
                translated_records.append((b['idx'], b['start'], b['end'], b['text']))
        else:
            total_tokens += usage.get('total_tokens', 0)
            print(f"OK in {dt:.1f}s", flush=True)
            
            translated_blocks = parse_srt(result)
            if not translated_blocks:
                print(f"    parse fail, 保留原文", flush=True)
                for b in batch:
                    translated_records.append((b['idx'], b['start'], b['end'], b['text']))
            else:
                # v2.12 修复: 不用 j 索引(LLM 内部洗牌导致错位)
                # 改用 LLM 输出的时间戳反查 src block, 找不到的保留原文
                n_t, n_o = len(translated_blocks), len(batch)
                # 1) 用时间戳反查, LLM 输出有 start 字段且与 batch 某条 start 一致
                tb_by_start = {tb['start']: tb for tb in translated_blocks}
                matched = 0
                for o_block in batch:
                    if o_block['start'] in tb_by_start:
                        tb = tb_by_start[o_block['start']]
                        # v2.12: 双语清洗(单语模式下, 错误输出的双语只取中文行)
                        zh_text = _strip_bilingual(tb['text'])
                        translated_records.append((o_block['idx'], o_block['start'], o_block['end'], zh_text))
                        matched += 1
                    else:
                        # LLM 未输出这一条 → 保留原文(保留 src 时间轴, 不动)
                        translated_records.append((o_block['idx'], o_block['start'], o_block['end'], o_block['text']))
                if matched < n_o:
                    miss = n_o - matched
                    print(f"    ⚠️ LLM 漏 {miss}/{n_o} 条, 已保留原文", flush=True)
                if n_t != n_o:
                    print(f"    ⚠️ n_t={n_t} != n_o={n_o}, 用时间戳反查代替位置对应", flush=True)

        # 写进度 + tmp
        with open(progress_file, "w") as f:
            json.dump({"next_batch": i+1, "records": translated_records}, f, ensure_ascii=False)
        with open(out_tmp, "w", encoding="utf-8") as f:
            for k, (idx, s, e, text) in enumerate(translated_records, 1):
                f.write(f"{k}\n{s} --> {e}\n{text}\n\n")
        time.sleep(0.3)
    
    # ===== v2.12 翻译质量统计 (修复双语误判) =====
    # v2.11 旧逻辑只看"是否含中文字符", 被双语输出误导
    # v2.12 新逻辑: 检测"未翻译" = zh 文本的"第一行"不是中文翻译
    #              (双语也被 _strip_bilingual 清洗过, 所以这是干净的单语中文或保留日文)
    total = len(translated_records)
    ja_re = re.compile(r'[぀-ヿ]')
    zh_re = re.compile(r'[一-鿿]')
    untranslated = 0  # 真正未翻(纯日文)
    bilingual_remain = 0  # 残留双语(双语清洗失败)
    for (_, _, _, t) in translated_records:
        first_line = t.split('\n')[0].strip()
        if not first_line:
            untranslated += 1
            continue
        # 如果第一行是纯日文(不含中文) → 未翻译
        if not zh_re.search(first_line):
            untranslated += 1
        # 如果有 2+ 行且含双语成分 → 残留
        lines = [l for l in t.split('\n') if l.strip()]
        if len(lines) >= 2:
            bilingual_remain += 1
    fail_rate = untranslated / total if total > 0 else 0
    print(f"  📊 翻译质量: 未翻译 {untranslated}/{total} = {fail_rate*100:.1f}% | 残留双语 {bilingual_remain}", flush=True)
    
    # 自适应: minimax 未含中文率 > 阈值 → 自动切 ollama 重跑
    AUTO_FALLBACK_THRESHOLD = float(os.environ.get("SUBTITLE_AUTO_FALLBACK_THRESHOLD", "0.3"))
    if engine == "minimax" and fail_rate > AUTO_FALLBACK_THRESHOLD:
        print(f"  🔄 minimax 未翻率 {fail_rate*100:.1f}% > {AUTO_FALLBACK_THRESHOLD*100:.0f}%, 自动重试用 ollama", flush=True)
        try:
            # 重跑整段用 ollama (args.ollama_model, 不是当前 engine 的 model)
            translated_records = _retrain_with_engine(
                blocks, args.ollama_model or args.model, bilingual, video_context)
            # 重算质量
            total = len(translated_records)
            no_chinese = sum(1 for (_, _, _, t) in translated_records 
                             if len(t) > 5 and not re.search(r'[\u4e00-\u9fff]', t))
            fail_rate = no_chinese / total if total > 0 else 0
            print(f"  📊 重试后质量: 未含中文 {no_chinese}/{total} = {fail_rate*100:.1f}%", flush=True)
        except Exception as e:
            print(f"  ⚠️ ollama fallback 失败: {e}, 保留 minimax 结果", flush=True)
    
    # 收尾
    shutil.move(out_tmp, out_path)
    os.remove(progress_file)
    print(f"\n✅ 翻译完成: {len(translated_records)} 条 → {out_path}", flush=True)

if __name__ == "__main__":
    main()

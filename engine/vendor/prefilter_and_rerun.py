#!/usr/bin/env python3
"""
prefilter_and_rerun.py — v2.16 修复: 全套增强 (timeout/重试/失败可见/备用引擎)

两步修复:
  1) 预过滤: 把 src.srt 里"明显是 BGM 伴唱/广告宣番/纯假名"的条目
     替换为 [听不清] / [广告宣番] 占位符, 让 LLM 知道"这些不要翻译"
  2) 小批量重跑: 对翻译后还没翻的 idx, 单独按 30 条一批重跑
     (避免整批被前 30 条无效内容污染导致 LLM 整体降级)

v2.15 → v2.16 增强 (2026-06-15 教训: 主翻译未译 23.32% 重跑第一批就 180s 超时):
  - timeout 180s → 300s (M3 不稳定首批慢响应)
  - 加单批重试: max_retries=2 (重试 1 次后还失败再 fallback)
  - 失败不再静默吞: 重跑失败 raise RerunError, pipeline 6.2 段能感知
  - 自适应引擎切换: minimax 单批失败 > 50% 自动切 ollama (跟 translate_srt.py 一致)
  - 全部批跑完再统计, 不再"第一批失败就放弃整批"

用法:
  python3 prefilter_and_rerun.py <src.srt> <zh.srt> [output.srt] [--engine minimax] [--series my-glossary]
  
  output 缺省 = zh.srt 原地覆盖 (重跑后)
  返回码: 0=成功(可能部分未译), 1=完全失败

环境变量:
  SUBTITLE_RERUN_TIMEOUT         LLM 单次调用超时秒数 (默认 300)
  SUBTITLE_RERUN_MAX_RETRIES     单批内最大重试次数 (默认 2, 不含首次)
  SUBTITLE_RERUN_FALLBACK_ENGINE minimax 失败后切到什么引擎 (默认 ollama)
  SUBTITLE_RERUN_FALLBACK_MODEL  备用引擎用的模型 (默认 Sakura-7B-Qwen2.5)
  SUBTITLE_RERUN_FALLBACK_THRESHOLD  单批失败率阈值超过则切引擎 (默认 0.5)
"""
import sys
import os
import re
import json
import subprocess
import tempfile
import argparse
from pathlib import Path

# 自相对路径：脚本已随引擎 vendored，不再依赖旧项目目录
VENDOR_DIR = os.path.dirname(os.path.abspath(__file__))
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

# 预过滤规则 (跟 06-14 修复同步)
AA_PURE = re.compile(r'^[あ-ん\s　]+$')
SHORT_PURE = re.compile(r'^[ぁ-んァ-ヴ]{1,3}$')
AD_KEYWORDS = [
    'Blu-ray', 'ブルーレイ', '発売', '劇場版', 'TVアニメ', '配信',
    'BD ', 'DVD', 'ムビチケ', '公開', '主題歌', '配信開始',
    'ジオラマ', 'フィギュア', 'コラボ', '展示', '告知',
    '予約', '購入', '発売中', 'iPhone', '商品', '描き下ろし',
]
PLACEHOLDER_RE = re.compile(r'^\[(听不清|广告宣番|♪ 歌词 ♪)\]$')
JA_KANA = re.compile(r'[぀-ヿ]')

def parse_srt(path):
    with open(path) as f:
        text = f.read()
    blocks = re.split(r'\n\s*\n', text.strip())
    out = []
    for b in blocks:
        lines = b.split('\n')
        if len(lines) < 2: continue
        if not re.match(r'^\d+$', lines[0].strip()): continue
        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', lines[1])
        if not m: continue
        text = '\n'.join(lines[2:]).strip()
        out.append({
            'idx': int(lines[0].strip()),
            'start': m.group(1), 'end': m.group(2),
            'text': text,
        })
    return out

def classify_invalid(text):
    """返回 (is_invalid, reason, placeholder)"""
    if AA_PURE.match(text): return True, 'BGM/伴唱', '[听不清]'
    if SHORT_PURE.match(text): return True, '短假名', '[听不清]'
    if any(kw in text for kw in AD_KEYWORDS): return True, '广告', '[广告宣番]'
    return False, '', text

def prefilter(src_blocks):
    """生成 (1) 预过滤版 src (2) invalid_map (idx -> reason)"""
    out = []
    invalid_map = {}
    for b in src_blocks:
        inv, reason, placeholder = classify_invalid(b['text'])
        if inv:
            invalid_map[b['idx']] = {'reason': reason, 'original': b['text']}
            out.append({**b, 'text': placeholder})
        else:
            out.append(b)
    return out, invalid_map

def write_srt(blocks, path):
    with open(path, 'w', encoding='utf-8') as f:
        for b in blocks:
            f.write(f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}\n\n")

def find_untranslated(zh_blocks, src_by_start):
    """找 zh 里"含日文假名且不是占位符"的条目"""
    untranslated = []
    for z in zh_blocks:
        if JA_KANA.search(z['text']) and not PLACEHOLDER_RE.match(z['text']):
            s = src_by_start.get(z['start'])
            if s: untranslated.append(s)
    untranslated.sort(key=lambda x: x['idx'])
    return untranslated

class RerunError(Exception):
    """v2.16: 重跑失败 (单批全部引擎都救不回来) — 之前静默 return {} 导致 23% 未译假装成功"""
    pass


GLOSSARY_BLOCK = ""


def _with_glossary(system):
    """重跑时也要带上专有名词表，否则重跑的批次比首轮更瞎。"""
    return system + GLOSSARY_BLOCK if GLOSSARY_BLOCK else system


def _parse_llm_output(stdout, batch):
    """从 LLM stdout 解析 {start: text}, 失败返回 None (不抛, 由调用方决定)"""
    out_blocks = re.split(r'\n\s*\n', stdout.strip())
    by_time = {}
    for ob in out_blocks:
        lines = ob.split('\n')
        if len(lines) < 3: continue
        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', lines[1])
        if not m: continue
        text = '\n'.join(lines[2:]).strip()
        by_time[m.group(1)] = text
    
    result = {}
    for b in batch:
        if b['start'] in by_time:
            result[b['start']] = by_time[b['start']]
    return result


def _call_minimax(batch, model, api_key, timeout):
    """单次 LLM 调用, 返回 (ok, parsed_dict_or_None, error_str)"""
    batch_text = "\n\n".join(
        f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in batch
    )
    system = """你是专业日语→简体中文字幕翻译。
输入是 SRT 字幕, 每条有编号、时间戳、日文文本。
你的任务: 逐条翻译成简体中文。

## 绝对规则 (违反视为失败)

1. **必须输出中文**: 即使日文是 BGM 歌词、广告、伴唱, 你也要尽力翻译
2. **数量严格 1:1**: 输入 N 条, 输出 N 条
3. **保留 idx 和时间戳**: 第 N 条的编号 = 输入的第 N 条编号
4. **直接输出 SRT, 不要任何解释**

## 翻译风格
- 口语自然, 避免翻译腔
- 数字/英文和中文间加英文空格
- 输出无标点
- BGM 歌词用 ♪ 包裹
"""
    system = _with_glossary(system)
    user = f"## 待翻译 SRT 字幕\n{batch_text}\n\n直接输出 SRT。"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(messages, tf, ensure_ascii=False)
        msg_file = tf.name

    env = os.environ.copy()
    if api_key: env["MINIMAX_API_KEY"] = api_key
    try:
        try:
            r = subprocess.run(
                ["mmx", "text", "chat", "--messages-file", msg_file,
                 "--model", model, "--max-tokens", "8000",
                 "--temperature", "0.1", "--seed", "42",
                 "--non-interactive", "--quiet"],
                env=env, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return False, None, f"timeout after {timeout}s"
    finally:
        try: os.unlink(msg_file)
        except: pass
    
    if r.returncode != 0:
        return False, None, f"rc={r.returncode}: {(r.stderr or '')[:200]}"
    
    parsed = _parse_llm_output(r.stdout, batch)
    if parsed is None:
        return False, None, "no parseable SRT in output"
    
    # 检查"翻译率": 含中文的比例 (避免 LLM 输出乱字符)
    zh_re = re.compile(r'[一-鿿]')
    n_translated = sum(1 for t in parsed.values() if zh_re.search(t))
    rate = n_translated / len(parsed) if parsed else 0
    return True, parsed, f"rate={rate*100:.0f}%"


def rerun_batch(batch, api_key, model="MiniMax-M3", engine="minimax", provider=""):
    """v2.16: 单批 LLM 重跑, 带重试 + 自适应 fallback

    返回:
      ({start: text}, engine_used, error_str_or_None)

    失败时返回 ({}, 'minimax', error_str) — 不再 raise (避免破坏主循环)
    主循环收集所有批的 error_str, 跑完再统一报告
    """
    timeout = int(os.environ.get("SUBTITLE_RERUN_TIMEOUT", "300"))
    max_retries = int(os.environ.get("SUBTITLE_RERUN_MAX_RETRIES", "2"))
    fallback_engine = os.environ.get("SUBTITLE_RERUN_FALLBACK_ENGINE", "ollama")
    fallback_model = os.environ.get("SUBTITLE_RERUN_FALLBACK_MODEL", "Sakura-7B-Qwen2.5")
    fallback_threshold = float(os.environ.get("SUBTITLE_RERUN_FALLBACK_THRESHOLD", "0.5"))

    # 0) 非 minimax 主引擎：直接走对应实现，不做 minimax→ollama 回落
    if engine == "dsh":
        parsed, err = _rerun_batch_dsh(batch, model, provider, timeout)
        return parsed, "dsh", err
    if engine == "ollama":
        return _rerun_batch_ollama(batch, model, timeout), "ollama", None
    if engine in ("deepseek", "openai-compatible"):
        parsed, err = _rerun_batch_openai(batch, model, api_key, timeout)
        return parsed, engine, err

    # 1) minimax: 重试 max_retries+1 次
    last_err = None
    for attempt in range(max_retries + 1):
        ok, parsed, info = _call_minimax(batch, model, api_key, timeout)
        if ok:
            # 解析成功, 检查翻译率
            zh_re = re.compile(r'[一-鿿]')
            n_translated = sum(1 for t in parsed.values() if zh_re.search(t))
            rate = n_translated / len(parsed) if parsed else 0
            if rate >= fallback_threshold:
                return parsed, 'minimax', None
            # 翻译率太低, 触发 fallback
            last_err = f"minimax rate {rate*100:.0f}% < {fallback_threshold*100:.0f}%"
            print(f"    ⚠️ {last_err}, 切 {fallback_engine} ({fallback_model})", flush=True)
            break  # 跳出重试, 走 fallback
        else:
            last_err = info
            if attempt < max_retries:
                wait = 2 + attempt * 2
                print(f"    ⚠️ minimax {info}, {wait}s 后重试 ({attempt+1}/{max_retries})", flush=True)
                import time as _t
                _t.sleep(wait)
            else:
                print(f"    ⚠️ minimax {info}, 重试 {max_retries} 次仍失败, 切 {fallback_engine}", flush=True)
    
    # 2) fallback engine (ollama)
    if fallback_engine == "ollama":
        return _rerun_batch_ollama(batch, fallback_model, timeout), fallback_engine, last_err
    elif fallback_engine == "deepseek":
        # TODO: 接 deepseek 跟 translate_srt.py 一样的实现
        return {}, fallback_engine, f"{fallback_engine} not implemented yet"
    else:
        return {}, 'minimax', last_err or "no fallback configured"


def _rerun_batch_dsh(batch, model, provider, timeout):
    """经 DSH 宿主中转重跑一批（凭据在 DSH 那边，引擎只发文本）。"""
    try:
        from translate_srt import _call_dsh_bridge
    except ImportError as exc:
        return {}, "translate_srt 不可用: %s" % exc
    if not provider or not model:
        return {}, "dsh 引擎缺少 provider 或 model"
    batch_text = "\n\n".join(
        f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in batch
    )
    system = """你是专业日语→简体中文字幕翻译。
逐条翻译为简体中文, 保留 idx 和时间戳, 输出 SRT 格式, 不要解释。"""
    system = _with_glossary(system)
    user = f"## 待翻译 SRT 字幕\n{batch_text}\n\n直接输出 SRT。"
    content, _ = _call_dsh_bridge(system, user, provider, model)
    if not content:
        return {}, "DSH 中转未返回内容"
    parsed = _parse_llm_output(content, batch)
    if parsed is None:
        return {}, "no parseable SRT in output"
    return parsed, None


def _rerun_batch_openai(batch, model, api_key, timeout):
    """通过 OpenAI 兼容接口（DeepSeek / 自建 baseUrl）重跑一批。"""
    import urllib.error
    import urllib.request

    base = (os.environ.get("SUBTITLE_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.deepseek.com").rstrip("/")
    key = (api_key or os.environ.get("SUBTITLE_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or "")
    if not key:
        return {}, "no api key"
    batch_text = "\n\n".join(
        f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in batch
    )
    system = """你是专业日语→简体中文字幕翻译。
逐条翻译为简体中文, 保留 idx 和时间戳, 输出 SRT 格式, 不要解释。"""
    system = _with_glossary(system)
    user = f"## 待翻译 SRT 字幕\n{batch_text}\n\n直接输出 SRT。"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 8000,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    try:
        request = urllib.request.Request(
            base + "/chat/completions", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_llm_output(content, batch)
        if parsed is None:
            return {}, "no parseable SRT in output"
        return parsed, None
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s" % (type(exc).__name__, exc)


def _rerun_batch_ollama(batch, model, timeout):
    """通过 ollama 本地模型重跑 (Sakura-7B 等), 慢但稳定不超时"""
    import time as _t
    import urllib.request
    
    # ollama HTTP API
    OLLAMA_URL = (os.environ.get("SUBTITLE_OLLAMA_HOST")
                  or "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
    
    batch_text = "\n\n".join(
        f"{b['idx']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in batch
    )
    system = """你是专业日语→简体中文字幕翻译。
逐条翻译为简体中文, BGM/广告也要翻, 保留 idx 和时间戳, 输出 SRT 格式, 不要解释。"""
    system = _with_glossary(system)
    user = f"## 待翻译 SRT 字幕\n{batch_text}\n\n直接输出 SRT。"
    
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "seed": 42, "num_predict": 8000},
    }).encode("utf-8")
    
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("message", {}).get("content", "")
        return _parse_llm_output(content, batch) or {}
    except Exception as e:
        print(f"    ⚠️ ollama fallback 失败: {e}", flush=True)
        return {}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("src_srt", help="原始 src.srt 路径")
    p.add_argument("zh_srt", help="已经翻译过的 zh.srt 路径")
    p.add_argument("output", nargs='?', help="输出路径, 缺省原地覆盖 zh_srt")
    p.add_argument("--engine", default="minimax",
                   choices=["dsh", "minimax", "ollama", "deepseek", "openai-compatible"])
    p.add_argument("--provider", default=os.environ.get("SUBTITLE_PROVIDER") or "",
                   help="--engine dsh 时使用的 DSH 模型提供方")
    p.add_argument("--model", default="MiniMax-M3")
    p.add_argument("--batch-size", type=int, default=30)
    p.add_argument("--no-rerun", action="store_true", help="只做预过滤不重跑 (调试用)")
    p.add_argument("--series", default=os.environ.get("SUBTITLE_SERIES") or "",
                   help="术语表名字，重跑时一并给模型（见 vendor/series.example.json）")
    args = p.parse_args()
    
    global GLOSSARY_BLOCK
    if getattr(args, "series", ""):
        try:
            import series_context
            loaded = series_context.get_context(args.series)
            if loaded:
                GLOSSARY_BLOCK = "\n\n## 专有名词表\n" + loaded + "\n"
                print(f"  术语表: {args.series}（{series_context.series_dir()}）", flush=True)
        except Exception as exc:  # 词表是加分项，坏了不能拖垮重跑
            print(f"  ⚠️ 术语表 {args.series} 不可用（{exc}）", flush=True)

    src_srt = args.src_srt
    zh_srt = args.zh_srt
    output = args.output or zh_srt
    
    # 1) 预过滤
    print(f"[1/3] 预过滤: {src_srt}", flush=True)
    src_blocks = parse_srt(src_srt)
    pre_blocks, invalid_map = prefilter(src_blocks)
    print(f"  共 {len(src_blocks)} 条, 标记 {len(invalid_map)} 条无效 (BGM/广告/短假名)", flush=True)
    
    # 写预过滤版 (供主 translate_srt.py 使用, 跟 v2.14 流程一致)
    prefilter_path = src_srt + '.prefilter'
    write_srt(pre_blocks, prefilter_path)
    print(f"  预过滤版: {prefilter_path}", flush=True)
    
    # 2) 检查 zh.srt 是否已经翻译
    if not os.path.exists(zh_srt):
        print(f"  [2/3] 跳过重跑: {zh_srt} 不存在 (请先跑 translate_srt.py)", flush=True)
        return 0
    
    zh_blocks = parse_srt(zh_srt)
    src_by_start = {s['start']: s for s in src_blocks}
    
    # 3) 找未翻译的 + 小批量重跑
    untranslated = find_untranslated(zh_blocks, src_by_start)
    print(f"\n[2/3] 未翻译条数: {len(untranslated)} (含占位符以外的日文条目)", flush=True)
    
    if args.no_rerun or len(untranslated) == 0:
        print("  跳过重跑", flush=True)
        if output and output != zh_srt and output != '/dev/null' and os.path.exists(zh_srt):
            import shutil
            shutil.copy(zh_srt, output)
        return 0
    
    # 读 API key：进程环境优先（引擎注入），其次 macOS keychain
    api_key = (os.environ.get("SUBTITLE_API_KEY")
               or os.environ.get("MINIMAX_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY") or "")
    if not api_key and args.engine == "minimax":
        # Only an explicitly named keychain item is read; the plugin ships no
        # default service name.
        ref = os.environ.get("SUBTITLE_API_KEYREF", "").strip()
        if ref:
            try:
                api_key = subprocess.check_output(
                    ["security", "find-generic-password", "-s", ref, "-w"],
                    stderr=subprocess.DEVNULL).decode("utf-8").strip()
            except Exception:
                api_key = ""
    
    # 分批
    batches = [untranslated[i:i+args.batch_size] for i in range(0, len(untranslated), args.batch_size)]
    print(f"  分 {len(batches)} 批 (size={args.batch_size})", flush=True)
    
    new_records = {}
    batch_failures = []  # v2.16: 收集失败信息, 最后统一报告 (不再静默吞)
    engine_used_count = {}  # 统计主用引擎 vs fallback
    for i, batch in enumerate(batches, 1):
        result, engine_used, err = rerun_batch(batch, api_key, args.model,
                                               engine=args.engine, provider=args.provider)
        new_records.update(result)
        engine_used_count[engine_used] = engine_used_count.get(engine_used, 0) + 1
        if err:
            batch_failures.append((i, engine_used, err))
            print(f"  [{i}/{len(batches)}] {engine_used} 失败: {err} (获得 {len(result)} 条)", flush=True)
        else:
            print(f"  [{i}/{len(batches)}] {engine_used} OK (获得 {len(result)} 条)", flush=True)
    
    # v2.16: 失败聚合报告
    if batch_failures:
        print(f"\n  ⚠️ 共 {len(batch_failures)}/{len(batches)} 批有失败:", flush=True)
        for i, eng, err in batch_failures:
            print(f"    - 批 {i}: engine={eng} err={err}", flush=True)
    if engine_used_count:
        print(f"  引擎使用统计: {engine_used_count}", flush=True)
    
    print(f"\n[3/3] 合并: 共获得 {len(new_records)} 条新翻译", flush=True)
    
    # 用新翻译替换 zh_blocks 中对应条目
    final = []
    replaced = 0
    for z in zh_blocks:
        if z['start'] in new_records:
            new_text = new_records[z['start']]
            if new_text and new_text != z['text']:
                final.append({**z, 'text': new_text})
                replaced += 1
                continue
        final.append(z)
    
    print(f"  替换 {replaced} 条", flush=True)
    
    # 写输出
    write_srt(final, output)
    print(f"  输出: {output}", flush=True)
    
    # 统计
    zh_re = re.compile(r'[一-鿿]')
    c = {'纯中文':0, '占位符':0, '日文':0, '空':0, '其他':0}
    for b in final:
        text = b['text']
        if PLACEHOLDER_RE.match(text): c['占位符'] += 1
        elif not text: c['空'] += 1
        elif JA_KANA.search(text): c['日文'] += 1
        elif zh_re.search(text): c['纯中文'] += 1
        else: c['其他'] += 1
    print(f"\n=== 最终统计 ===", flush=True)
    for k, v in c.items():
        print(f"  {k}: {v} ({v/len(final)*100:.1f}%)", flush=True)
    final_pct = (c['纯中文']+c['占位符'])/len(final)*100 if final else 0
    print(f"  翻译+占位: {final_pct:.1f}%", flush=True)
    
    # v2.16: 翻译+占位 < 90% 返回 1 让 pipeline 知道 (之前永远返 0)
    if final_pct < 90 and batch_failures:
        print(f"\n  ❌ 重跑仍有 {len(batch_failures)} 批失败, 最终翻译率 {final_pct:.1f}% < 90%", flush=True)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())

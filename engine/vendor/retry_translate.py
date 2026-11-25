#!/usr/bin/env python3
"""重试 parse fail 的批 - 用更宽松的 prompt（不强调 SRT 格式，避免模型又把 SRT 原样返回）"""
import os
import re
import sys
import time
import json
import subprocess
import tempfile
import shutil

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

def translate_batch(batch, model="MiniMax-M3", engine="minimax", provider=""):
    """更宽松的 prompt"""
    items = "\n".join(f"[{b['idx']}] {b['start']} --> {b['end']} {b['text']}" for b in batch)
    
    system = "你是字幕翻译。把下面日文短语翻译成简体中文口语（无标点、单行12字内、人名地名保留日文）。对每条单独输出，格式: [序号] 时间戳 译文"
    user = f"逐条翻译为简体中文：\n{items}"
    
    # DSH 中转：凭据与路由都在宿主那边，引擎只发文本
    if engine == "dsh":
        try:
            from translate_srt import _call_dsh_bridge
        except ImportError as exc:
            print(f"    ⚠️ translate_srt 不可用: {exc}", flush=True)
            return None
        content, _ = _call_dsh_bridge(system, user, provider, model)
        return content

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(messages, tf, ensure_ascii=False)
        msg_file = tf.name

    
    try:
        for attempt in range(3):
            try:
                r = subprocess.run(
                    ["mmx", "text", "chat", "--messages-file", msg_file, "--model", model,
                     "--max-tokens", "4000", "--temperature", "0.1", "--seed", "42",
                     "--non-interactive", "--quiet"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    return r.stdout.strip()
                time.sleep(2 + attempt*2)
            except subprocess.TimeoutExpired:
                time.sleep(3)
        return None
    finally:
        try: os.unlink(msg_file)
        except: pass

def main():
    import argparse
    parser = argparse.ArgumentParser(description="对未译条目用更宽松的 prompt 重试")
    parser.add_argument("input", help="输入 SRT")
    parser.add_argument("output", help="输出 SRT")
    parser.add_argument("--engine", default=os.environ.get("SUBTITLE_ENGINE") or "minimax",
                        choices=["dsh", "minimax", "ollama", "deepseek", "openai-compatible"])
    parser.add_argument("--provider", default=os.environ.get("SUBTITLE_PROVIDER") or "",
                        help="--engine dsh 时使用的 DSH 模型提供方")
    parser.add_argument("--model", default=os.environ.get("SUBTITLE_MODEL") or "MiniMax-M3")
    args = parser.parse_args()

    in_path = args.input
    out_path = args.output
    work_path = out_path + ".work"
    
    # 找未翻译的（纯日文、无中文）
    content = open(in_path, "r", encoding="utf-8").read()
    blocks = parse_srt(content)
    zh_re = re.compile(r'[\u4e00-\u9fff]')
    ja_re = re.compile(r'[\u3040-\u309f\u30a0-\u30ff]')
    
    untranslated = []
    for b in blocks:
        text = b['text']
        if not text: continue
        ja_count = len(ja_re.findall(text))
        zh_count = len(zh_re.findall(text))
        total = len(text.replace(" ", "").replace("\n", ""))
        if ja_count > total * 0.4 and zh_count == 0:
            untranslated.append(b)
    
    print(f"未翻译条数: {len(untranslated)}", flush=True)
    if not untranslated:
        shutil.copy(in_path, out_path)
        return
    
    # 分批 15 条
    batch_size = 15
    translations = {}  # idx -> text
    
    for i in range(0, len(untranslated), batch_size):
        batch = untranslated[i:i+batch_size]
        ids = [b['idx'] for b in batch]
        print(f"  [{i//batch_size + 1}/{(len(untranslated) + batch_size - 1)//batch_size}] 翻译 {ids[0]}-{ids[-1]} ({len(batch)} 条)", flush=True)
        
        result = translate_batch(batch, model=args.model, engine=args.engine, provider=args.provider)
        if result is None:
            print(f"    失败，保留原文", flush=True)
            continue
        
        # 解析输出（[idx] 时间戳 文本）
        # 简化：按行解析，匹配 [数字]
        cur_idx = None
        cur_text = []
        for line in result.split("\n"):
            m = re.match(r'^\[(\d+)\]\s+(\S+)\s+-->\s+(\S+)\s+(.*)', line)
            if m:
                # 保存上一条
                if cur_idx is not None and cur_text:
                    translations[cur_idx] = "\n".join(cur_text).strip()
                cur_idx = int(m.group(1))
                cur_text = [m.group(4)]
            elif cur_idx is not None and line.strip():
                cur_text.append(line.strip())
        if cur_idx is not None and cur_text:
            translations[cur_idx] = "\n".join(cur_text).strip()
        
        print(f"    解析出 {len(translations)} 条翻译", flush=True)
        time.sleep(0.5)
    
    # 写新 SRT
    new_blocks = []
    failed_kept_as_ja = []  # 记录保留为原文日文的 idx
    for b in blocks:
        if b['idx'] in translations:
            new_blocks.append({**b, "text": translations[b['idx']]})
        else:
            # 兜底：retry 仍失败的 → 保留原文日文 + 警告标记
            # 不让 LLM 自由发挥产生幻觉（v1 行为：会乱填如"会消失的 是的"）
            failed_kept_as_ja.append(b['idx'])
            new_blocks.append(b)
    
    with open(out_path, "w", encoding="utf-8") as f:
        for i, b in enumerate(new_blocks, 1):
            f.write(f"{i}\n{b['start']} --> {b['end']}\n{b['text']}\n\n")
    
    # 统计
    still_ja = sum(1 for b in new_blocks if ja_re.search(b['text']) and not zh_re.search(b['text']))
    print(f"\n✅ 重试完成: {len(blocks)} 条, 其中 {len(translations)} 条重新翻译", flush=True)
    print(f"   仍为纯日文: {still_ja} 条 (保留原文，不产生幻觉)", flush=True)
    if failed_kept_as_ja:
        print(f"   ⚠️ 保留日文的 idx: {failed_kept_as_ja}", flush=True)
    print(f"   输出: {out_path}", flush=True)

if __name__ == "__main__":
    main()

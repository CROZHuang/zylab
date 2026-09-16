#!/usr/bin/env python3
"""从 HuggingFace 的 config.json 采集开源模型的原生上下文窗口。

为什么用它：Boyue 网关不给 max_model_len，而专门发百万 token 的探测请求要
真金白银（二分 3 次 × 136 个模型）。HF 的 config.json 是免费且权威的。

**重要限制**（写进 context_source，别让后人误当实测值）：
  - max_position_embeddings 是**原生**窗口。Qwen2.5 这类模型服务时常用 YaRN
    外推到 4 倍，所以这个值可能**低估**实际可用窗口。
  - 网关也可能为了省显存把窗口**砍到原生值以下**。
  两头都不确定，所以它只是个先验，真值仍靠 note_context_ok/reject 在使用中学。

续跑安全：已经有 context 的条目直接跳过，中断后重跑不会重复请求。
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import models as M

# 网关自己加的前缀，不是模型身份的一部分（保留大小写，HF 仓库名区分大小写）
PREFIX = re.compile(r"^(Pro/|USD-guiji/|bailian/|vanchin/|kimi/|deepinfer/|or/)+")
UA = {"User-Agent": "zylab-spec-harvest/1.0"}


def repo_of(model_id):
    """模型 id → HF 仓库名；不像开源仓库就返回 None。"""
    r = PREFIX.sub("", model_id)
    return r if r.count("/") == 1 else None


def fetch_ctx(repo, timeout=60):
    """返回 (窗口, 说明)。失败时窗口为 None。"""
    url = f"https://huggingface.co/{repo}/raw/main/config.json"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cfg = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}"
    n = (cfg.get("max_position_embeddings")
         or (cfg.get("text_config") or {}).get("max_position_embeddings"))
    if not n:
        return None, "config 里没有 max_position_embeddings"
    # **不要**再乘 rope_scaling.factor。实测 DeepSeek-V3.2：
    #   original_max_position_embeddings(4096) × factor(40) = 163,840
    #   = max_position_embeddings
    # 也就是说 max_position_embeddings 已经是外推后的结果，再乘一次会得到
    # 6,553,600 这种荒谬值 —— 而那会让压缩永远不触发、会话必定撑爆。
    rs = cfg.get("rope_scaling") or {}
    note = f"native+{rs.get('type')}" if rs.get("type") else "native"
    n = int(n)
    # 护栏：荒谬的大值比没有值更危险 —— 它会让压缩永远不触发。
    # 上面那个 ×40 的 bug 正是这么写进去 6,553,600 的。
    if not 1_000 <= n <= 2_000_000:
        return None, f"值不可信({n:,})，拒绝写入"
    return n, note


def main():
    db = M.load()
    todo = {}          # repo -> [模型key, ...]
    for k, rec in db["models"].items():
        if rec.get("context") or rec.get("status") != "ok":
            continue
        if rec.get("supports_tools") is not True:
            continue
        repo = repo_of(rec.get("id", ""))
        if repo:
            todo.setdefault(repo, []).append(k)

    print(f"待查 {len(todo)} 个 HF 仓库，覆盖 {sum(len(v) for v in todo.values())} 个模型条目",
          flush=True)
    hit = miss = 0
    for i, (repo, keys) in enumerate(sorted(todo.items()), 1):
        n, note = fetch_ctx(repo)
        if n:
            db = M.load()                       # 每次重读，避免覆盖并发写入
            for k in keys:
                db["models"][k]["context"] = n
                db["models"][k]["context_source"] = f"hf-config:{note}"
            M.save(db)
            hit += 1
            print(f"[{i}/{len(todo)}] {repo:<46} {n:>9,}  {note}", flush=True)
        else:
            miss += 1
            print(f"[{i}/{len(todo)}] {repo:<46}     ——    {note}", flush=True)
        time.sleep(1)
    print(f"\n完成：命中 {hit}，未获得 {miss}", flush=True)


if __name__ == "__main__":
    main()

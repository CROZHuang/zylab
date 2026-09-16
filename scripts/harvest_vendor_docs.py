#!/usr/bin/env python3
"""从厂商官网文档采集闭源 API 模型的上下文窗口。

补 HF 采集的缺口：闭源模型没有公开权重，config.json 查不到，但官网文档写着。
免费，不消耗任何模型调用额度。

覆盖：智谱（按模型一页）、Moonshot、DeepSeek。
不覆盖阿里百炼 —— 实测 help.aliyun.com 返回 1.19 MB 却只命中 1 处关键词，
是 JS 渲染的空壳，curl 拿不到表格。

写入时标记 context_source=doc:<厂商>，与实测值(probed)、网关自报、HF 值区分。
"""
import json
import os
import re
import sys
import time
import html as _html
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import models as M

UA = {"User-Agent": "Mozilla/5.0 (zylab-doc-harvest)"}
# 「上下文窗口 200K」「上下文长度 128K」「context window 256K」
CTX_RE = re.compile(
    r"(?:上下文(?:窗口|长度)|context\s*(?:window|length))\s*[:：]?\s*"
    r"(\d[\d,\.]*)\s*([KkMm])?", re.I)


def text_of(url, timeout=45):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}"
    s = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))), None


def parse_ctx(text):
    """返回文档里出现的最大上下文值（token 数）。"""
    best = None
    for m in CTX_RE.finditer(text or ""):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        u = (m.group(2) or "").lower()
        n = int(n * (1_000_000 if u == "m" else 1_024 if u == "k" else 1))
        # K 在文档里通常指 1000 的整数倍语义，但两种都落在同一量级，取 1024 偏保守
        if 1_000 <= n <= 2_000_000 and (best is None or n > best):
            best = n
    return best


# **只收「按模型一页」的文档源。**
#
# 单页汇总型的文档（DeepSeek 的 pricing 页、Moonshot 的 api/chat 页）在这里是
# 结构性错误：整页只有一个 "CONTEXT LENGTH 1M"，按前缀套用就会把这一个数字
# 抹到该厂商全部模型上 —— 实测把 deepseek-r1 和 deepseek-v3 写成了 1M，
# 而它们的 HF config 明确是 163,840。已撤销并改为按证据强度回填别名。
#
# 判据：url 模板里必须含 {m}，即每个模型有独立页面。
SOURCES = [
    ("zhipu", "https://docs.bigmodel.cn/cn/guide/models/text/{m}", ("glm-",)),
]
for _v, _u, _p in SOURCES:
    assert "{m}" in _u, f"{_v}: 单页汇总源会把一个数字抹到全部模型上"


def main():
    db = M.load()
    need = {}
    for k, rec in db["models"].items():
        if rec.get("context") or rec.get("status") != "ok":
            continue
        if rec.get("supports_tools") is not True:
            continue
        mid = (rec.get("id") or "").split("/")[-1].lower()
        need.setdefault(mid, []).append(k)
    print(f"待补 {len(need)} 个不同模型\n", flush=True)

    # 逐厂商：智谱按模型一页，其余是单页汇总
    page_cache = {}
    hit = 0
    for mid, keys in sorted(need.items()):
        vendor = url = None
        for v, tpl, prefixes in SOURCES:
            if any(mid.startswith(p) for p in prefixes):
                vendor, url = v, tpl.format(m=mid)
                break
        if not url:
            continue
        if url not in page_cache:
            page_cache[url] = text_of(url)
            time.sleep(1)
        text, err = page_cache[url]
        if err:
            print(f"  {mid:<34} —— {err}", flush=True)
            continue
        n = parse_ctx(text)
        if not n:
            print(f"  {mid:<34} —— 文档里没找到上下文数字", flush=True)
            continue
        db = M.load()
        for k in keys:
            db["models"][k]["context"] = n
            db["models"][k]["context_source"] = f"doc:{vendor}"
        M.save(db)
        hit += 1
        print(f"  {mid:<34} {n:>9,}  ← {vendor}", flush=True)
    print(f"\n完成：补上 {hit} 个", flush=True)


if __name__ == "__main__":
    main()

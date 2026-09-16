#!/usr/bin/env python3
"""从本机 ~/.zylab/models.json 生成随仓库分发的能力表 seed。

**为什么需要它.** 模型能力（尤其 context window）来自两处昂贵的来源：逐档
探测（每档一次真实请求）和逐个模型翻厂商文档。这些结果一直只存在用户态的
`~/.zylab/models.json` 里，仓库中没有 —— 于是任何一份新 clone 都从零开始，
`/model` 里看不到 context，压缩阈值只能落回保守默认值。转交给其他研究者时
这批数据会整个丢失。

**分发什么、不分发什么.** seed 只带**对任何 pod 都成立**的事实：模型 id、
家族、上下文长度及其来源、能力开关。以下逐项剔除 ——

  error / error_kind / http_status / error_code / provider_request_id
      网关针对**本机账号**返回的报错（含 request id）。别人的 key 分组不同，
      结论也不同；带过去只会误导。
  first_token_s / headers_s / response_s / probe_total_s / probe_timing_version
      本机网络下量到的延迟。别的 pod 出口不同，照搬等于假数据。
  catalog_fingerprint / probe_attempts / temperature_retried / retryable
      本机某一次探测过程的中间状态，不是模型属性。
  gateway == "test"
      测试污染残留（见 AGENTS.md §3 那次 mk() fixture 事故）。

用法：
    python3 scripts/build_model_seed.py                 # 写入 core/model_seed.json
    python3 scripts/build_model_seed.py --dry-run       # 只报告，不写
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = Path(os.path.expanduser("~/.zylab/models.json"))
TARGET = REPO / "core" / "model_seed.json"

# 只有这些字段是「模型属性」，其余都是某台机器某一次探测的副产品。
KEEP = (
    "id", "gateway", "family", "listed", "status",
    "context", "context_source", "context_seen_ok", "context_upper_bound",
    "supports_tools", "supports_reasoning", "supports_image",
    "supports_temperature", "endpoints", "probed",
)
DROP_GATEWAYS = ("test",)


def build(source=SOURCE):
    raw = json.loads(source.read_text(encoding="utf-8"))
    out, dropped = {}, {"gateway": 0, "no_id": 0}
    for key, record in sorted(raw.get("models", {}).items()):
        if record.get("gateway") in DROP_GATEWAYS:
            dropped["gateway"] += 1
            continue
        if not record.get("id"):
            dropped["no_id"] += 1
            continue
        out[key] = {f: record[f] for f in KEEP if f in record}
    return {
        "version": raw.get("version"),
        "seed": True,
        "source": "maintainer probe + vendor docs",
        "generated_from_updated": raw.get("updated"),
        "models": out,
    }, dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(SOURCE))
    ap.add_argument("--target", default=str(TARGET))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    source = Path(os.path.expanduser(args.source))
    if not source.is_file():
        sys.exit(f"找不到源能力表：{source}")
    seed, dropped = build(source)

    models = seed["models"]
    with_ctx = sum(1 for r in models.values() if r.get("context"))
    by_gateway = {}
    for record in models.values():
        by_gateway[record.get("gateway", "?")] = by_gateway.get(
            record.get("gateway", "?"), 0) + 1
    print(f"  源: {source}")
    print(f"  收录 {len(models)} 个模型（{with_ctx} 个有 context）")
    for gateway, count in sorted(by_gateway.items()):
        print(f"    {gateway:12} {count}")
    print(f"  剔除: gateway=test {dropped['gateway']} · 无 id {dropped['no_id']}")

    blob = json.dumps(seed, ensure_ascii=False, indent=1, sort_keys=True)
    for needle in ("sk-", "Bearer", "Authorization", "request id"):
        if needle in blob:
            sys.exit(f"seed 中出现可疑内容 {needle!r}，已中止")
    print("  敏感扫描: 通过")

    if args.dry_run:
        print("  (--dry-run，未写入)")
        return 0
    Path(args.target).write_text(blob + "\n", encoding="utf-8")
    print(f"  已写入 {args.target}（{len(blob):,} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

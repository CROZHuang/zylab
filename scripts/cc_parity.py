#!/usr/bin/env python3
"""跑 SPEC-CC-parity 记分板：每条契约对应的测试跑一遍，打印 parity = 通过/总数。

    python3 scripts/cc_parity.py            # 表格
    python3 scripts/cc_parity.py --json     # 机器可读

退出码：有「已钉住却失败」的契约 → 1（那是回归）；未钉住的契约只扣分不报错。
"""
import argparse
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.parity_manifest import CONTRACTS  # noqa: E402


def run_one(test_id):
    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromName(test_id)
    except Exception as exc:  # noqa: BLE001 - 名字写错也要报成一行
        return "missing", f"{type(exc).__name__}: {exc}"
    if suite.countTestCases() == 0:
        return "missing", "no such test"
    buf = io.StringIO()
    result = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
    if result.wasSuccessful():
        return "pass", ""
    detail = (result.failures or result.errors)[0][1].strip().splitlines()[-1]
    return "fail", detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = []
    for cid, group, title, test in CONTRACTS:
        if test is None:
            rows.append({"id": cid, "group": group, "title": title, "status": "unpinned", "detail": ""})
            continue
        status, detail = run_one(test)
        rows.append({"id": cid, "group": group, "title": title, "status": status, "detail": detail, "test": test})
    passed = sum(r["status"] == "pass" for r in rows)
    failed = [r for r in rows if r["status"] in ("fail", "missing")]
    total = len(rows)
    if args.json:
        print(json.dumps({"parity": f"{passed}/{total}", "rows": rows}, ensure_ascii=False, indent=2))
    else:
        mark = {"pass": "✅", "fail": "❌", "missing": "⁉", "unpinned": "·"}
        for r in rows:
            print(f"  {mark[r['status']]} {r['id']:3} {r['group']:4} {r['title']:34} {r['detail'][:60]}")
        print(f"\nparity = {passed}/{total}   （· = 未钉住，不计分；❌/⁉ = 已钉住但失败/找不到，属回归）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

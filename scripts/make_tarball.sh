#!/usr/bin/env bash
# 出包：dist/zylab-<短sha>.tgz（git archive：只含已提交内容，天然不带 .git / __pycache__ / logs / .zylab）。
# 出包前的发布闸门跑的是**真正的测试**，不是裸 grep —— 代码里的报错文案本来就要
# 提到受保护路径，裸 grep 会把自己拒绝出包（§2.5.4 的矛盾就是这么解的）。
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO"
OUT_DIR="${1:-$REPO/dist}"
SHA="$(git rev-parse --short HEAD)"
NAME="zylab-$SHA"
if ! python3 -m unittest tests.test_paths tests.test_cold_start >/dev/null 2>&1; then
    echo "发布闸门红，拒绝出包。复现：python3 -m unittest tests.test_paths tests.test_cold_start" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "警告：工作树有未提交改动，tarball 只含已提交内容（HEAD=$SHA）" >&2
fi
mkdir -p "$OUT_DIR"
git archive --format=tar --prefix="$NAME/" HEAD | gzip -9 > "$OUT_DIR/$NAME.tgz"
echo "$OUT_DIR/$NAME.tgz ($(du -h "$OUT_DIR/$NAME.tgz" | cut -f1))"
echo "同事侧：tar xzf $NAME.tgz && cd $NAME && ./zylab init"

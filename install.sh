#!/usr/bin/env bash
# zylab install.sh —— 在 ~/.local/bin/zylab 生成一个**薄 stub**（普通文件，不是 symlink，也不是副本）。
# 仓库绝对路径固化在 stub 里；stub 只做 runpy，永远不会与 zylab.py 产生偏斜（BACKLOG-rename-zylab §2.5）。
# 幂等：已存在则比对内容，不同才覆盖并提示。  用法：./install.sh [--bin-dir DIR] [--uninstall]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN_DIR="${HOME}/.local/bin"
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) UNINSTALL=1 ;;
        --bin-dir) BIN_DIR="$2"; shift ;;
        -h|--help) echo "用法: ./install.sh [--bin-dir DIR] [--uninstall]"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done
STUB="$BIN_DIR/zylab"
if [ "$UNINSTALL" = 1 ]; then
    if [ -f "$STUB" ]; then rm -f "$STUB"; echo "已删除 $STUB"; else echo "没有 $STUB，无需卸载"; fi
    exit 0
fi
[ -f "$REPO/zylab.py" ] || { echo "找不到 $REPO/zylab.py —— install.sh 必须留在仓库根目录里运行" >&2; exit 1; }
mkdir -p "$BIN_DIR"
CONTENT="$(cat <<EOF
#!/usr/bin/env python3
# 由 zylab/install.sh 生成的薄 stub —— 不是副本（不会过期），不是 symlink（解压路径不同链接必断）。
import os
import runpy
import sys

REPO = "$REPO"
sys.path.insert(0, REPO)
runpy.run_path(os.path.join(REPO, "zylab.py"), run_name="__main__")
EOF
)"
if [ -f "$STUB" ] && [ "$(cat "$STUB")" = "$CONTENT" ]; then
    echo "已是最新：$STUB → $REPO"
else
    printf '%s\n' "$CONTENT" > "$STUB"
    chmod 755 "$STUB"
    echo "已写入 $STUB → $REPO"
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "提示：$BIN_DIR 不在 PATH 里，加一行：export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
echo "下一步：zylab init   （或直接 zylab）"

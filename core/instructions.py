"""指令记忆：`AGENTS.md` / `CLAUDE.md` 的层级合并与 `@import`（路线图 P4）。

以前只有一条规则：从 cwd 往上找，**找到第一个 `CLAUDE.md` 就停**。三个后果：

1. **不认 `AGENTS.md`。** 这个仓库自己的项目规则就写在 `AGENTS.md` 里——
   zylab 在自己的仓库里干活时读不到自己的规矩。agents.md 已经是跨工具的事实约定。
2. **单体仓库只能生效一层。** 根上写「全仓禁止 git add -A」、包目录里写「这个包
   用 pnpm」，两条永远只有一条能被读到，而且是哪条取决于 cwd 在哪。
3. **没有 `@import`。** 规则一长就得整块复制，复制出来的副本一定会漂移。

现在：根 → cwd 逐层收集**全部**命中，根在前、最近的在后（与 system_prompt 里
「用户级在前、项目级在后，后者更具体」同一套顺序约定）；`@import` 递归展开。

## 信任边界（这部分不是锦上添花）

项目级指令文件**来自 clone，是不可信数据**。`@import` 能读文件，于是一份恶意的
`CLAUDE.md` 只要写上 `@~/.ssh/id_rsa`，密钥就会被塞进 system prompt 发给网关，
而界面上什么都看不出来。所以：

- **项目级的 import 必须落在 workspace 根以内**（与 `tools._guard_read` 同一条边界）；
- 任何 scope 的 import 都过 `attachments.is_sensitive_path`（与 @file、repo-map
  共用那一道闸——分头各写一套清单必然漂移，REVIEW-repo-map-20260828 F1 就是这么
  把 credentials.json 送出去的）；
- 被拒的 import **留一行可见的说明**，不静默丢掉：用户要看得见规则生效了，
  模型也要知道这里本来该有东西。

## 为什么一路爬到文件系统根，而不是停在 git 根

停在 git 根更"干净"：载进来的全是仓库数据，信任故事也更简单。但那会**减少**
今天已经在生效的东西——旧规则是"往上找到第一个 CLAUDE.md 就停"，仓库外面那份
（放在工作区父目录里、写着这台机器的规矩：只读挂载、网络路由之类）本来就命中得到。
停在 git 根等于把它从 zylab 眼前拿走，而没有人要求过这件事。
所以继续爬，用体量提示（`WARN_TOTAL_CHARS`）让人自己决定要不要减。
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import attachments

# 同一目录下两个都在就都读：内容多半不同（一个给 Claude Code、一个给别的工具），
# 而把一个软链到另一个也很常见——按 realpath 与内容哈希各去重一次。
NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_IMPORT_DEPTH = 5
# 预算是**字符**不是 token：这一层不该为了省一次估算去 import tokenizer。
# 按中英混排约 1.6 字符/token 折，40,000 字符 ≈ 25K token，已经是一个
# 该被提醒的体量了。
MAX_TOTAL_CHARS = 40_000
WARN_TOTAL_CHARS = 20_000

# `@path`：只认**整行以 @ 开头**（允许前面有空白或列表符号）的形式。
# Claude Code 认行内任意位置的 `@`，那会把 `联系 @someone`、Python 装饰器、
# 邮箱地址都当成 import。收窄成整行形式，规则可预期，也写得进文档。
_IMPORT = re.compile(r"^\s*(?:[-*+]\s+)?@(?P<path>\S+)\s*$")


@dataclass
class Block:
    """一份被读进来的指令文件。"""

    path: str
    text: str
    scope: str                       # "user" | "project"
    imports: list = field(default_factory=list)


@dataclass
class Bundle:
    blocks: list = field(default_factory=list)
    notes: list = field(default_factory=list)      # 给用户看的：拒绝、截断、超量

    @property
    def total_chars(self):
        return sum(len(b.text) for b in self.blocks)


def _up(start):
    """[cwd, parent, …, root]。与 agent.Path_up 同义，这里自带一份避免环依赖。"""
    p = os.path.abspath(start)
    out = []
    while True:
        out.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            return out
        p = parent


def discover(cwd=None, *, names=NAMES):
    """从文件系统根到 cwd 的全部指令文件，**根在前**（最近的排最后 = 最具体）。"""
    found, seen = [], set()
    for directory in reversed(_up(cwd or os.getcwd())):
        for name in names:
            candidate = os.path.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            key = os.path.realpath(candidate)
            if key in seen:            # CLAUDE.md 软链到 AGENTS.md 是常见写法
                continue
            seen.add(key)
            found.append(candidate)
    return found


def _under(root, target):
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:                 # 跨盘符（Windows）：一定在外面
        return False


def scope_of(path, root):
    """这份指令文件算不算「跟着 clone 来的」。

    **不可信的是仓库里的东西，不是「项目级」这个位置。** 工作目录**之上**的
    那些（`~/CLAUDE.md`、`/srv/CLAUDE.md`）是这台机器的主人自己写的，与
    `<state_home>/ZYLAB.md` 同级；工作目录**以内**的才来自 clone。
    分不开的话，要么把用户自己写的 import 误拒，要么把恶意仓库的放行。
    """
    return "project" if _under(root, os.path.realpath(path)) else "ancestor"


def _import_refusal(target, *, scope, root):
    """这个 import 该不该拒？拒就返回给用户看的理由，否则 None。"""
    reason = attachments.is_sensitive_path(target)
    if reason:
        return f"疑似凭据（{reason}）"
    if scope != "project":
        return None
    if not _under(root, target):
        return "指向工作目录之外——仓库里的指令文件跟着 clone 来，不可信"
    return None


def expand(path, *, scope, root, depth=0, seen=None, notes=None):
    """读一个文件并递归展开它的 `@import`。返回 (正文, 被 import 的路径列表)。"""
    seen = set() if seen is None else seen
    notes = [] if notes is None else notes
    real = os.path.realpath(path)
    if real in seen:
        return "", []
    seen.add(real)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(f"指令文件读不了：{path}（{exc.__class__.__name__}）")
        return "", []

    out, imported = [], []
    base = os.path.dirname(real)
    for line in raw.splitlines():
        match = _IMPORT.match(line)
        if match is None:
            out.append(line)
            continue
        spec = match.group("path")
        target = os.path.realpath(
            os.path.join(base, os.path.expanduser(spec))
            if not os.path.isabs(os.path.expanduser(spec))
            else os.path.expanduser(spec))
        if depth >= MAX_IMPORT_DEPTH:
            out.append(f"[@{spec} 未展开：import 深度超过 {MAX_IMPORT_DEPTH} 层]")
            notes.append(f"import 太深，已停在 {spec}（{path}）")
            continue
        refusal = _import_refusal(target, scope=scope, root=root)
        if refusal:
            out.append(f"[@{spec} 已拒绝：{refusal}]")
            notes.append(f"拒绝 import {spec}（{refusal}）—— 来自 {path}")
            continue
        if not os.path.isfile(target):
            out.append(f"[@{spec} 不存在]")
            continue
        body, nested = expand(
            target, scope=scope, root=root, depth=depth + 1,
            seen=seen, notes=notes)
        if not body.strip():
            continue
        imported.append(target)
        imported.extend(nested)
        out.append(f"<imported src=\"{target}\">")
        out.append(body)
        out.append("</imported>")
    return "\n".join(out), imported


def _fit(bundle):
    """总量超预算时，**从最不具体的那头开始丢**——离 cwd 最近的规则最不能丢。"""
    while bundle.total_chars > MAX_TOTAL_CHARS and len(bundle.blocks) > 1:
        dropped = bundle.blocks.pop(0)
        bundle.notes.append(
            f"指令总量超过 {MAX_TOTAL_CHARS} 字符，已丢掉最外层的 "
            f"{dropped.path}（{len(dropped.text)} 字符）")
    if bundle.total_chars > MAX_TOTAL_CHARS and bundle.blocks:
        only = bundle.blocks[0]
        only.text = only.text[:MAX_TOTAL_CHARS]
        bundle.notes.append(
            f"{only.path} 单份就超过 {MAX_TOTAL_CHARS} 字符，已截断")
    elif bundle.total_chars > WARN_TOTAL_CHARS:
        bundle.notes.append(
            f"指令共 {bundle.total_chars} 字符，每一次请求都要带上它们——"
            "考虑把不常用的部分移进 skill 或用 @import 按需拆分")
    return bundle


def collect(cwd=None, *, names=NAMES, root=None):
    """项目级指令：层级合并 + import 展开 + 预算。"""
    cwd = os.path.abspath(cwd or os.getcwd())
    root = os.path.realpath(root or cwd)
    bundle = Bundle()
    hashes = set()
    for path in discover(cwd, names=names):
        text, imported = expand(
            path, scope=scope_of(path, root), root=root, notes=bundle.notes)
        if not text.strip():
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in hashes:           # 内容一模一样的两份（复制而非软链）
            continue
        hashes.add(digest)
        bundle.blocks.append(
            Block(path=path, text=text, scope=scope_of(path, root),
                  imports=imported))
    return _fit(bundle)


def render(bundle, *, tag="project-instructions"):
    """拼成 system prompt 里的那几段。空 bundle 返回空串。"""
    parts = []
    for block in bundle.blocks:
        parts.append(f"<{tag} src=\"{block.path}\">\n{block.text}\n</{tag}>")
    return "\n\n".join(parts)

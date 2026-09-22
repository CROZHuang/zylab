"""Hook 体系 —— 把「守卫、检查、自动化」从硬编码变成可配置。

设计沿用 Claude Code 的形状（PreToolUse / PostToolUse + JSON over stdio），
但有两条**本项目特有的硬约束**：

1. **hook 只能来自用户级配置。** hook 执行任意命令；如果项目级
   `<repo>/.zylab/settings.json` 也能注册 hook，那么任何 clone 下来的仓库
   都能在你机器上静默执行代码 —— 这比「项目级放宽权限」危险得多。
   项目级出现 hooks 字段一律忽略并告警。

2. **hook 不能绕过受保护路径守卫。** 守卫仍然硬编码在工具层，在 hook 之后
   独立执行。hook 只能让规则**更严**（额外拦截），不能让它更松。

退出码约定（同 Claude Code）：
    0        放行；stdout 若是 JSON 可改写参数
    2        **拦截**；stderr 作为拒绝理由回给模型
    其他      hook 自身坏了 —— 记一条告警然后放行。
             理由：一个写错的 lint hook 不该让整个 CLI 停摆。
             真正要拦的场景请显式 exit 2。
    超时      同「其他」，告警后放行。
"""
from . import paths
import json
import os
import subprocess
import time

DEFAULT_TIMEOUT = 15
BLOCK_EXIT = 2


class HookBlocked(RuntimeError):
    """某个 PreToolUse hook 明确拒绝了这次调用。"""


def _matches(matcher, tool):
    """matcher 为空或 '*' 匹配全部；否则按 | 分隔的名字精确匹配。"""
    if not matcher or matcher == "*":
        return True
    return tool in [m.strip() for m in str(matcher).split("|") if m.strip()]


def _bash():
    """与 Bash 工具同一个解析器。裸 `"bash"` 在 Windows 上会命中 System32 的
    WSL 垫片，hook 就跑进了另一个文件系统命名空间（延迟 import 避免环）。"""
    try:
        from . import tools
        return tools.bash_executable()
    except Exception:          # noqa: BLE001 —— 解析不出来仍按老样子试一次
        return "bash"


def _run_one(spec, payload, timeout=None):
    """跑一个 hook。返回 (放行?, 改写后的参数或 None, 说明)。"""
    cmd = spec.get("command")
    if not cmd:
        return True, None, ""
    env = dict(os.environ)
    # 常用字段也塞进环境变量，方便写 shell 单行 hook 而不必解析 JSON
    fields = {"HOOK": payload.get("hook", ""), "TOOL": payload.get("tool", ""),
              "CWD": payload.get("cwd", "")}
    for k in ("path", "command"):
        v = (payload.get("args") or {}).get(k)
        if isinstance(v, str):
            fields[f"TOOL_{k.upper()}"] = v
    for suffix, value in fields.items():
        env[paths.env_name(suffix)] = value
    try:
        r = subprocess.run(
            [_bash(), "-lc", cmd], input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, encoding="utf-8", errors="replace", text=True, env=env,
            timeout=timeout or spec.get("timeout") or DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return True, None, f"hook 超时（{cmd[:40]}），已放行"
    except OSError as e:
        return True, None, f"hook 无法执行（{e}），已放行"

    if r.returncode == BLOCK_EXIT:
        reason = (r.stderr or r.stdout or "").strip() or "hook 未给出理由"
        return False, None, reason
    if r.returncode != 0:
        return True, None, (f"hook 退出码 {r.returncode}（非 2，视为 hook 自身故障"
                            f"，已放行）：{(r.stderr or '').strip()[:160]}")
    # 退出码 0：stdout 若是 JSON 且带 args，就用它改写参数
    out = (r.stdout or "").strip()
    if out.startswith("{"):
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return True, None, ""
        if d.get("decision") == "deny":
            return False, None, d.get("reason") or "hook 拒绝"
        if isinstance(d.get("args"), dict):
            return True, d["args"], d.get("reason") or ""
    return True, None, ""


def run_hooks(event, tool, args, cfg, *, session="", result=None, on_note=None):
    """依次执行匹配的 hook。

    PreToolUse 有 hook 拒绝就抛 HookBlocked；否则返回（可能被改写过的）参数。
    PostToolUse 只做观察，拒绝无意义，一律忽略拒绝语义。
    """
    specs = [h for h in (cfg.get("hooks") or {}).get(event, [])
             if _matches(h.get("matcher"), tool)]
    if not specs:
        return args
    payload = {"hook": event, "tool": tool, "args": args,
               "cwd": os.getcwd(), "session": session,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if result is not None:
        payload["result"] = result[:4000]      # 别把巨大的工具输出塞进 hook
    for spec in specs:
        ok, new_args, note = _run_one(spec, payload)
        if note and on_note:
            on_note(note)
        if not ok and event == "PreToolUse":
            raise HookBlocked(note)
        if new_args is not None:
            payload["args"] = args = new_args
    return args


def sanitize_project_hooks(project_cfg, on_note=None):
    """把项目级配置里的 hooks 剔掉。

    这是安全边界，不是洁癖：项目目录常常来自 clone，允许它注册 hook
    等于允许任意仓库在本机执行代码。
    """
    if "hooks" in project_cfg:
        project_cfg.pop("hooks")
        if on_note:
            on_note("项目级配置里的 hooks 已忽略 —— hook 只能在用户级配置注册")
    return project_cfg

"""全部路径与环境事实的单点（BACKLOG-rename-zylab §2.1，批次 1）。

`APP_NAME` 一个字符串带动四类派生量：状态目录 `~/.zylab`、项目目录 `.zylab/`、
用户级指令 `ZYLAB.md`、环境变量前缀 `ZYLAB_`。批次 2 改名只改这里。

**选项 (b)**：其它模块保留 import 时求值的模块级常量，只是改成在 import 时调这里的函数。
代价是 `$ZYLAB_HOME` 只在进程启动时生效——必须在启动前 export，进程内改无效
（与 `ZYLAB_STATE_DB` 之类调用时求值的变量不同，README 要写明）。

受保护路径是**声明出来的，不是猜出来的**：默认为空，由 `ZYLAB_PROTECTED_PATHS`
或 settings 的 `sandbox.protected_paths` 声明。早先的版本把一个具体挂载点写死成
常量，理由是"同事的机器长得一样"——这个前提是错的，同一个集群的机器挂载也不同。
写死的默认值既守不到真正需要守的路径，又会在别人的机器上凭空多出一条规则。
启动路径上不发任何网络请求。
"""
import json
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "zylab"


def _env_list(suffix, sep):
    return [item.strip()
            for item in str(env_get(suffix) or "").split(sep)
            if item.strip()]


def _user_sandbox_cfg():
    """只读用户级 settings.json 的 `sandbox` 段。

    **刻意不 import core.settings**：那是个循环 —— settings 的 DEFAULTS 反过来
    要调这里的 protected_paths()。这里只做一次 json.load，读不到就当没有：
    配置损坏不该让路径解析本身失败。

    为什么非读不可：词法守卫（core/tools.py 的 PROTECTED）和沙箱只读挂载是
    两条独立的路。如果只有沙箱读配置，用户在 settings 里声明了受保护路径，
    挂载会只读、而 `_guard_bash` 一律放行 —— 配了但半边不生效，**界面上看不出来**。
    把读取放在这一层，两条路才共用同一份声明。
    """
    try:
        with open(state_home() / "settings.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    sandbox = data.get("sandbox") if isinstance(data, dict) else None
    return sandbox if isinstance(sandbox, dict) else {}


def env_prefix():
    return APP_NAME.upper() + "_"


def env_name(suffix):
    """`env_name("HOME")` → `ZYLAB_HOME`。改名后自动变 `ZYLAB_HOME`。"""
    return env_prefix() + str(suffix)


def env_get(suffix, default=None, *, environ=None):
    """运行时环境变量 ZYLAB_X 的唯一读法（所有读点都经这里，方便日后统一改名或加回落）。"""
    environ = os.environ if environ is None else environ
    return environ.get(env_name(suffix), default)


def project_dir(root):
    """项目级目录 <root>/.zylab/。"""
    return Path(root) / project_dirname()


# 自包含（便携）模式：状态目录放在应用根目录里，整个目录可整体拷贝。
# 不叫 .zylab/——那是项目级配置目录的名字，在应用目录里工作时会撞成同一个文件。
PORTABLE_DIRNAME = f".{APP_NAME}-home"


def app_root():
    """应用根目录（zylab.py 所在目录）。测试用 ZYLAB_APP_ROOT 指到临时目录，别在仓库里造便携目录。"""
    override = os.environ.get(env_name("APP_ROOT"))
    if override:
        return Path(os.path.expanduser(override))
    return Path(__file__).resolve().parents[1]


def portable_home():
    return app_root() / PORTABLE_DIRNAME


def default_home():
    return Path(os.path.expanduser("~")) / f".{APP_NAME}"


def is_portable():
    return not os.environ.get(env_name("HOME")) and portable_home().is_dir()


def state_home():
    """状态目录，三级优先：`$ZYLAB_HOME` 显式改道 → 应用目录里的 `.zylab-home/`（自包含）→ `~/.zylab`。
    core/homemigrate.py 里有同一套判断（它不能 import 这里），改一处要改两处。"""
    override = os.environ.get(env_name("HOME"))
    if override:
        return Path(os.path.expanduser(override))
    portable = portable_home()
    if portable.is_dir():
        return portable
    return default_home()


def project_dirname():
    return f".{APP_NAME}"


def user_md_name():
    return f"{APP_NAME.upper()}.md"


def user_config_key_file():
    """XDG 风格的 `~/.config/zylab/keys.env`：换 HOME 即生效，不探测。"""
    return Path(os.path.expanduser("~")) / ".config" / APP_NAME / "keys.env"


def protected_paths():
    """声明为只读的绝对路径，默认为空 —— 不预设任何一台机器的挂载布局。

    想让某个归档目录永不被写入，**启动前** export
    `ZYLAB_PROTECTED_PATHS=/data/archive:/mnt/readonly`（`os.pathsep` 分隔），
    或写进用户级 settings 的 `sandbox.protected_paths`；两者取并集。
    声明了就是硬边界：项目配置、permission、hook 都不能放宽。
    """
    declared = list(_env_list("PROTECTED_PATHS", os.pathsep))
    declared += [str(x) for x in (_user_sandbox_cfg().get("protected_paths") or [])
                 if str(x).strip()]
    out = []
    for item in declared:
        item = os.path.expanduser(item.strip())
        item = item.rstrip("/") or "/"
        if item and item not in out:
            out.append(item)
    return out


def protected_remotes():
    """受保护的 rclone 远端前缀，如 `archive:bucket`；默认为空。

    `ZYLAB_PROTECTED_REMOTES` 用逗号分隔而不是 `os.pathsep` —— 远端名里的 `:`
    是 rclone 语法的一部分；配置里是 `sandbox.protected_remotes`。
    同一份数据常有两道门（文件系统挂载 + 对象存储远端），只守一道等于没守。
    """
    declared = list(_env_list("PROTECTED_REMOTES", ","))
    declared += [str(x) for x in (_user_sandbox_cfg().get("protected_remotes") or [])
                 if str(x).strip()]
    out = []
    for item in declared:
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def legacy_key_files():
    """老安装遗留的 keys.env（`ZYLAB_LEGACY_KEYS_FILE`），存在才返回。"""
    return tuple(p for p in (os.path.expanduser(env_get("LEGACY_KEYS_FILE") or ""),)
                 if p and os.path.exists(p))


def legacy_env_files():
    """老安装遗留的 shell 环境文件（`ZYLAB_LEGACY_ENV_FILE`），存在才返回。"""
    return tuple(p for p in (os.path.expanduser(env_get("LEGACY_ENV_FILE") or ""),)
                 if p and os.path.exists(p))


def under_protected(path):
    """path 是否落在任一受保护路径内（先 resolve，再比较）。

    与 `is_protected` 的区别：那个是纯字符串前缀比较，给已经 realpath 过的
    调用方用；这个自己 resolve，给拿到任意用户输入路径的调用方用。
    受保护路径为空时恒为 False —— 没声明就没有这条边界。
    """
    resolved = Path(path).resolve(strict=False)
    for root in protected_paths():
        root_path = Path(root).resolve(strict=False)
        if resolved == root_path or root_path in resolved.parents:
            return True
    return False


def is_protected(path):
    """path 是否落在任一受保护路径内（字符串比较；调用方自己决定要不要先 realpath）。"""
    value = str(path)
    return any(value == root or value.startswith(root + "/")
               for root in protected_paths())


def machine_facts():
    """只有两项（09-01 砍到两项）：Python 版本、有没有 rg。不探测配额、swap、镜像。"""
    return {
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "has_rg": shutil.which("rg") is not None,
    }

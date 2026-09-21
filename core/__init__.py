"""zylab core。

这个文件里唯一的运行时逻辑是一道闸：**测试进程绝不能落到真实状态目录**。

为什么放在这里：`core.store` 在 import 时就把状态路径绑死，所以隔离必须发生在任何
`from core import …` 之前——而包的 `__init__` 正是最先执行的地方。以前的兜底在
`tests/__init__.py`，但基线命令 `python3 -m unittest discover -s tests` 把测试当顶层模块
加载，根本不会 import 那个文件；没自己隔离的测试于是直接写进真实目录。2026-09-20 实查：
实时 `models.json` 里躺着夹具造的 `test/m`、`test/model-a`、`test/chat-only`，
`usage.jsonl` 里有 4 条 gateway="test" 的记录，记忆库里有 `echo test` / `hi`；当天一次
失败的测试运行还把一条**假能力**（「deepinfer/kimi-k3-256k 不许关思考」）持久化了，
之后每次运行都受它影响。靠「每个测试记得自己隔离」守不住，得是结构性的。
"""
import os as _os
import sys as _sys


def _isolate_test_process():
    from . import paths                       # 只有函数，没有 import 时绑定的路径

    home_var = paths.env_name("HOME")
    if _os.environ.get(home_var):
        return None                           # 显式指定过：尊重它
    main = _sys.modules.get("__main__")
    runner = (getattr(getattr(main, "__spec__", None), "name", "") or "").split(".")[0]
    script = _os.path.basename(getattr(main, "__file__", "") or "")
    under_test = (runner in ("unittest", "pytest") or "pytest" in _sys.modules
                  or (script.startswith("test_") and script.endswith(".py")))
    if not under_test:
        return None
    import atexit
    import shutil
    import tempfile

    root = tempfile.mkdtemp(prefix="zylab-tests-")
    _os.environ[home_var] = _os.path.join(root, ".zylab")
    _os.environ.setdefault(paths.env_name("APP_ROOT"), root)
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    return root


TEST_ISOLATION_ROOT = _isolate_test_process()

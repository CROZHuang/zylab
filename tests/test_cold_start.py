"""冷启动验收（BACKLOG-rename-zylab §2.0.4）：把入口放进一个"别人的 pod"里，失败必须自己说人话。

子进程用 env -i 语义（只有 HOME/PATH/TERM），ZYLAB_KEYS_FILE 指向不存在的文件 ——
否则 client 的候选列表里有维护者机器的绝对路径，本机永远"有 key"。
"""
import ast
import contextlib
import glob
import json
import io
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "zylab.py")
sys.path.insert(0, ROOT)
from tests.platform_support import assert_mode  # noqa: E402
import zylab  # noqa: E402


# 子进程环境是刻意精简的（陌生人的第一分钟），但 CPython 在 Windows 上缺了这几个就起不来：
# 没有 SystemRoot，初始化随机数就失败 —— `Fatal Python error: … Python runtime state:
# preinitialized`，Windows 3.10 那列冷启动 6 个失败的共同根因（2026-09-24 CI）。
# 按「有就透传」写、不问平台：Linux / macOS 上本来就没有这些变量，行为不变。
OS_ESSENTIALS = ("SystemRoot", "WINDIR", "PATHEXT", "COMSPEC")


def run(args, home, extra=None, timeout=90):
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "TERM": "dumb",
           "PYTHONIOENCODING": "utf-8",
           "ZYLAB_APP_ROOT": home,            # 别让仓库里的 .zylab-home/ 把测试引到真实状态
           "ZYLAB_KEYS_FILE": os.path.join(home, "keys.env")}
    for name in OS_ESSENTIALS:
        value = os.environ.get(name)          # Windows 上 os.environ 不分大小写
        if value:
            env[name] = value
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, ENTRY, *args], env=env, cwd=ROOT, encoding="utf-8", errors="replace", text=True,
        capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)


class ChildEnvironment(unittest.TestCase):
    def test_the_child_keeps_what_windows_needs_to_start(self):
        """精简环境不能精简掉解释器自己起来要的东西（Windows：SystemRoot）。"""
        from unittest import mock
        seen = {}

        def fake_run(argv, env=None, **kwargs):
            seen.update(env or {})
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows",
                                          "PATHEXT": ".COM;.EXE"}), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            run(["--help"], "/tmp/cold-home")
        self.assertEqual(seen.get("SystemRoot"), r"C:\Windows")
        self.assertEqual(seen.get("PATHEXT"), ".COM;.EXE")
        self.assertEqual(seen["PATH"], "/usr/bin:/bin", "其余照旧精简")

    def test_nothing_extra_leaks_in_where_those_variables_do_not_exist(self):
        from unittest import mock
        seen = {}

        def fake_run(argv, env=None, **kwargs):
            seen.update(env or {})
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            for name in OS_ESSENTIALS:
                os.environ.pop(name, None)
            run(["--help"], "/tmp/cold-home")
        self.assertFalse(set(OS_ESSENTIALS) & set(seen))


class ColdStartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="zylab-cold-")
        self.home = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_help_exits_zero_without_traceback(self):
        p = run(["--help"], self.home)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("init", p.stdout)

    def test_version_answers_without_key_and_names_a_commit(self):
        """同事报 bug 时要能一句话说清跑的是哪一版（2026-09-15 之前 --version
        直接 argparse 报错退出 2，仓库还 0 个 tag）。"""
        p = run(["--version"], self.home)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertRegex(p.stdout.strip(), r"^zylab (unknown|[0-9a-f]{12})")
        self.assertIn("python ", p.stdout)

    def test_tarball_version_stamp_stays_wired(self):
        """git archive 靠 .gitattributes 的 export-subst 把提交号烤进 _build.py；
        克隆出来的工作树里它必须还是占位符，否则 tarball 会报错版本。"""
        attrs = os.path.join(ROOT, ".gitattributes")
        self.assertTrue(os.path.isfile(attrs))
        with open(attrs, encoding="utf-8") as handle:
            self.assertIn("core/_build.py export-subst", handle.read())
        with open(os.path.join(ROOT, "core", "_build.py"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("$Format:%H$", body)
        self.assertIn("$Format:%cI$", body)

    def test_init_without_key_fails_with_the_next_command(self):
        p = run(["init", "--gateway", "deepinfer", "--base", "https://127.0.0.1:9/v1",
                 "--key-env", "FAKE_KEY", "--yes"], self.home)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("export FAKE_KEY=", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_init_writes_key_privately_and_reads_unreachable_gateway_as_not_a_key_problem(self):
        p = run(["init", "--gateway", "deepinfer", "--key-env", "FAKE_KEY", "--yes"], self.home,
                extra={"FAKE_KEY": "sk-not-a-real-key", "ZYLAB_BASE": "https://127.0.0.1:9"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("不可达", p.stdout)
        self.assertIn("与 key 无关", p.stdout)
        self.assertNotIn("sk-not-a-real-key", p.stdout + p.stderr, "key 永不回显")
        keys = os.path.join(self.home, "keys.env")
        assert_mode(self, keys, 0o600)
        with open(keys, encoding="utf-8") as f:
            self.assertEqual(f.read(), "DEEPINFER_API_KEY=sk-not-a-real-key\n")

    # ---- 「从 GitHub 下载 → 填自己的 key → 模型跟着刷新」这条路 ----------
    # 2026-09-21 实测过的冷启动：`zylab init --gateway openai` 回「未知网关 openai，
    # 可选：deepinfer, boyue」，而要新增网关得先手写 settings.json —— 可 init 本来
    # 就是写它的那一步。`zylab --models` 更直接吐 Python traceback。

    def test_a_public_vendor_needs_no_endpoint_from_the_user(self):
        """陌生人只有一把 OpenAI key：不该还要去查 endpoint 填进来。"""
        p = run(["init", "--gateway", "openai", "--yes"], self.home)
        self.assertIn("api.openai.com", p.stdout, p.stdout + p.stderr)
        self.assertNotIn("未知网关", p.stdout)
        self.assertEqual(p.returncode, 2, "没有 key 时仍然该停在收 key 这一步")
        self.assertIn("OPENAI_API_KEY", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_an_unknown_gateway_is_created_instead_of_refused(self):
        """自建/公司内部网关：给个地址就地建 profile，而不是「未知网关」打回。"""
        p = run(["init", "--gateway", "mycorp", "--base", "https://127.0.0.1:9/v1",
                 "--key-env", "FAKE_KEY", "--yes"], self.home,
                extra={"FAKE_KEY": "sk-not-a-real-key"})
        self.assertNotIn("未知网关", p.stdout + p.stderr)
        self.assertIn("已新建网关 profile mycorp", p.stdout)
        self.assertEqual(p.returncode, 1, "建完 profile 后该走到探测并报不可达")
        self.assertIn("不可达", p.stdout)
        settings = os.path.join(self.home, ".zylab", "settings.json")
        with open(settings, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["gateways"]["mycorp"]["base"],
                         "https://127.0.0.1:9/v1")
        self.assertEqual(saved["gateways"]["mycorp"]["keys"], ["FAKE_KEY"])

    def seed_settings(self, gateways):
        os.makedirs(os.path.join(self.home, ".zylab"), exist_ok=True)
        path = os.path.join(self.home, ".zylab", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"gateways": gateways}, f)
        return path

    def test_init_for_one_gateway_keeps_the_other_gateways(self):
        """2026-09-24 实测：`write_user({"gateways": {名字: …}})` 是浅合并 —— 给一个网关
        写配置，settings 里其它网关的地址整段被冲掉。维护者自己就存着两个。"""
        path = self.seed_settings({"deepinfer": {"base": "https://a.invalid/v1"},
                                   "boyue": {"base": "https://b.invalid/v1"}})
        p = run(["init", "--gateway", "mycorp", "--base", "https://127.0.0.1:9/v1",
                 "--key-env", "FAKE_KEY", "--yes"], self.home,
                extra={"FAKE_KEY": "sk-not-a-real-key"})
        self.assertNotIn("Traceback", p.stderr)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)["gateways"]
        self.assertEqual(saved.get("deepinfer"), {"base": "https://a.invalid/v1"})
        self.assertEqual(saved.get("boyue"), {"base": "https://b.invalid/v1"})
        self.assertEqual(saved["mycorp"]["base"], "https://127.0.0.1:9/v1")

    def test_init_saves_a_declared_proxy_for_that_gateway_only(self):
        """代理存进 zylab 自己的配置、只挂在这一个网关下；任何输出都不带凭据。"""
        path = self.seed_settings({"deepinfer": {"base": "https://a.invalid/v1"}})
        p = run(["init", "--gateway", "mycorp", "--base", "https://127.0.0.1:9/v1",
                 "--proxy", "http://someone:secret-pass@127.0.0.1:9/",
                 "--key-env", "FAKE_KEY", "--yes"], self.home,
                extra={"FAKE_KEY": "sk-not-a-real-key"})
        self.assertNotIn("Traceback", p.stderr)
        self.assertNotIn("secret-pass", p.stdout + p.stderr)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)["gateways"]
        self.assertEqual(saved["mycorp"]["proxy"], "http://someone:secret-pass@127.0.0.1:9/")
        self.assertNotIn("proxy", saved["deepinfer"])

    def test_an_unconfigured_private_gateway_names_the_ready_made_ones(self):
        """自建网关没地址是正常的；但得告诉人「其实可以直接挑一个内置的」。"""
        p = run(["init", "--gateway", "boyue", "--yes"], self.home)
        self.assertEqual(p.returncode, 2)
        self.assertIn("openai", p.stdout, "要列出地址已内置的网关")
        self.assertNotIn("不预置任何网关地址", p.stdout,
                         "这句话在公开厂商内置之后就不成立了")

    def test_listing_models_before_any_setup_is_not_a_traceback(self):
        p = run(["--models"], self.home)
        blob = p.stdout + p.stderr
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        # 断的是「给了能直接照做的下一步」，不是某个词。原来钉的是 "endpoint"，
        # 而全新安装那条路后来改成了「还没选网关 + 可选清单」——同样可执行，
        # 词不一样。钉命令比钉措辞稳。
        self.assertIn("zylab init --gateway", blob, blob[:300])

    def test_the_auto_picked_model_prefers_a_workhorse_over_alphabetical_order(self):
        """出厂默认是按维护者的 key 定的，对别人不成立；自动换时别挑到视觉/OCR 模型。

        实测过的坏结果：按目录顺序取第一个 → 挑中 Intern-S2-Preview-397B。
        """
        rows = [
            {"id": "Intern-S2-Preview-397B", "max_model_len": 262144},
            {"id": "qwen-vl-ocr", "max_model_len": 1048576},
            {"id": "deepseek-v4-flash", "max_model_len": 1048576},
            {"id": "text-embedding-3", "max_model_len": 8192},
        ]
        picked = zylab._init_pick_candidates(rows)
        self.assertEqual(picked[0], "deepseek-v4-flash")
        self.assertNotIn("qwen-vl-ocr", picked, "视觉/OCR 模型不该当编码默认")
        self.assertNotIn("text-embedding-3", picked)

    def test_the_factory_default_model_is_only_a_last_resort(self):
        """写死一个模型名只对定它的那把 key 成立。

        2026-09-21 实测：出厂默认 `kimi-k3-256k` 在维护者自己的目录里都已经是 delisted
        （2026-09-16 下架）。陌生人不跑 init 直接 `zylab -p "hi"` 撞上的是一个不存在的模型。
        """
        from core import agent as A                    # noqa: PLC0415
        from unittest import mock                      # noqa: PLC0415

        rows = [
            {"id": "qwen-vl-ocr", "context": 1_048_576, "status": "ok"},
            {"id": "big-coder", "context": 1_000_000, "status": "ok"},
            {"id": "gone", "context": 2_000_000, "status": "delisted"},
            {"id": "unlisted", "context": 3_000_000, "listed": False},
            {"id": "small", "context": 8_192, "status": "ok"},
        ]
        with mock.patch.object(A.models_db, "probed_rows", return_value=rows):
            self.assertEqual(A.default_model("g"), "big-coder")
        with mock.patch.object(A.models_db, "probed_rows", return_value=[]):
            self.assertEqual(A.default_model("g"), A.MODEL, "目录空了才退到常量")
        with mock.patch.object(A.models_db, "probed_rows",
                               side_effect=RuntimeError("目录坏了")):
            self.assertEqual(A.default_model("g"), A.MODEL, "目录坏了不该让人起不来")
        with mock.patch.dict(os.environ, {"ZYLAB_MODEL": "explicit-one"}):
            self.assertEqual(A.default_model("g"), "explicit-one")

    def test_print_mode_without_key_says_so_within_the_temp_home(self):
        p = run(["-p", "hi"], self.home)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = p.stdout + p.stderr
        self.assertIn("API key", out)
        self.assertIn(os.path.join(self.home, "keys.env"), out, "补救路径要落在这个 HOME 里")
        self.assertNotIn("Traceback", p.stderr)

    def test_entry_and_package_parse_under_python_3_7_grammar(self):
        # 闸门的前提：老解释器得先能把文件编译过，才轮得到闸门说话
        for path in [ENTRY] + sorted(glob.glob(os.path.join(ROOT, "core", "*.py"))):
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), filename=path, feature_version=(3, 7))

    def test_version_gate_exits_2_and_names_the_minimum(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            zylab._python_gate((3, 9, 7))
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("3.10", err.getvalue())
        self.assertIn("3.9", err.getvalue())
        zylab._python_gate((3, 10, 0))   # 刚好达标不抛

    def test_preamble_order_is_gate_then_syspath_then_kc_import(self):
        with open(ENTRY, encoding="utf-8") as f:
            src = f.read()
        gate, syspath, core = (src.index("\n_python_gate()\n"),
                             src.index("sys.path.insert(0, os.path.dirname"),
                             src.index("\nfrom core import"))
        self.assertLess(gate, syspath)
        self.assertLess(syspath, core)


class FirstScreenNamesNoPrivateGateway(unittest.TestCase):
    """陌生人第一次跑 zylab 时，第一屏不该是一个他没听说过的网关名。

    出厂常量是 `deepinfer`（维护者的自建网关）。他没选过它；看到
    「网关 deepinfer 还没有 endpoint」只会先问「这是什么、zylab 凭什么要连它」。
    名字本身留着——换成别家等于替他做一个可能要花钱的选择；改的是**提示认出
    「这是出厂值，不是你选的」**。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="zylab-first-")
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_a_brand_new_install_is_told_to_pick_one(self):
        done = run(["--models"], self.home)
        blob = done.stdout + done.stderr
        self.assertIn("还没选网关", blob, blob[:400])
        self.assertNotIn("deepinfer", blob, "第一屏不该出现出厂常量的名字")
        self.assertIn("openai", blob, "得把能选的列出来")
        self.assertNotIn("Traceback", blob)

    def test_an_unknown_gateway_still_gets_the_address_hint(self):
        """用户自己起的名字：那条路不变，该说「地址自己填」。"""
        from core import client
        hint = client.base_missing_hint("mycorp")
        self.assertIn("mycorp", hint)
        self.assertIn("--base", hint)
        self.assertNotIn("还没选网关", hint)

    def test_a_configured_factory_gateway_is_a_real_choice(self):
        """维护者自己把 deepinfer 配上了地址——那是选过的，不走新分支。"""
        from unittest import mock
        from core import client
        merged = dict(client.GATEWAYS)
        merged[client.FACTORY_GATEWAY] = dict(
            merged.get(client.FACTORY_GATEWAY, {}), base="https://x/v1")
        with mock.patch.object(client, "GATEWAYS", merged):
            self.assertFalse(client.is_unchosen_default(client.FACTORY_GATEWAY))

    def test_an_explicit_env_choice_is_a_real_choice(self):
        from unittest import mock
        from core import client
        with mock.patch.dict(
                os.environ, {"ZYLAB_GATEWAY": client.FACTORY_GATEWAY}):
            self.assertFalse(client.is_unchosen_default(client.FACTORY_GATEWAY))


class _Tty(io.StringIO):
    def isatty(self):
        return True


class SilenceWatchTests(unittest.TestCase):
    def test_speaks_on_a_tty_after_silence_and_resets_on_events(self):
        out = _Tty()
        watch = zylab._SilenceWatch("g/m", stream=out)
        watch.FIRST, watch.EVERY = 0.3, 0.3
        watch.start()
        try:
            time.sleep(0.9)
            first = out.getvalue()
            self.assertIn("没有响应", first)
            watch.touch()
            time.sleep(0.2)
            self.assertEqual(out.getvalue(), first, "有事件后立刻闭嘴")
        finally:
            watch.stop()

    def test_stays_silent_when_stderr_is_a_pipe(self):
        out = io.StringIO()
        watch = zylab._SilenceWatch("g/m", stream=out)
        watch.FIRST = 0.1
        watch.start()
        time.sleep(0.4)
        watch.stop()
        self.assertEqual(out.getvalue(), "")
        self.assertIsNone(watch._thread)


if __name__ == "__main__":
    unittest.main()

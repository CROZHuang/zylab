#!/usr/bin/env python3
"""Measure managed-tool key-to-paint latency through a real PTY.

The child runs the real zylab REPL. A local fake provider requests an
actual managed bash -lc 'sleep N' tool call, so the measured path includes
Session._turn_managed, TaskManager polling, InputPump, TerminalRenderer, and
the pseudo-terminal. No network request is made.

This is an acceptance benchmark, not a normal unit-test assertion: shared pod
scheduler noise can be real. It exits non-zero when aggregate p95 misses the
requested threshold or when the tool ends during sampling.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SENTINELS = (b"#", b"%", b"&", b"+", b"=", b"@", b"^", b"_")
JITTER_SECONDS = (0.000, 0.001, 0.003, 0.005, 0.007)

CHILD = r'''
import json
import os
import time

import zylab
from core import agent as A, client, store

tool_seconds = float(os.environ["ZYLAB_BENCH_TOOL_SECONDS"])
task_id = os.environ["ZYLAB_BENCH_TASK_ID"]
store.ensure_home()

agent = A.Agent.__new__(A.Agent)
agent.model = "m"
agent.gateway = "local-benchmark"
agent.session_id = "m3-key-paint"
agent.load_md = False
agent.messages = [{"role": "system", "content": "benchmark"}]
agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
agent.cache_read = agent.cache_write = 0
agent.cache_reported = False
agent.compact_failed = None
agent.ctx_limit = 100000
agent.compact_at = 70000
agent.ctx_known = True
agent.ctx_limit_source = "benchmark"
agent.context_summary = None
agent.context_invalid_reason = None
agent._compact_failed_key = None
agent._last_age_notice_key = None
agent._seen_ok_hi = 0
agent.started = time.time()

calls = {"n": 0}

def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    if calls["n"] == 1:
        yield {
            "t": "tool",
            "v": [{
                "id": "call-latency",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({
                        "command": f"sleep {tool_seconds}",
                        "timeout": max(5, int(tool_seconds) + 2),
                    }),
                },
            }],
        }
    else:
        yield {"t": "text", "v": "AFTER_TOOL"}
    yield {"t": "done", "reason": "stop", "usage": {}}

client.stream_chat = fake_stream
session = zylab.Session(agent)
session.auto = True
session.task_manager.id_factory = lambda: task_id
agent.confirm = session.confirm
zylab.repl(session, "m")
print("BENCH_CHILD_RESULT:" + json.dumps({"calls": calls["n"]}))
'''


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def cgroup_cpu_stat() -> dict[str, int]:
    path = Path("/sys/fs/cgroup/cpu.stat")
    try:
        rows = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return {}
    out = {}
    for row in rows:
        parts = row.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def read_more(fd: int, buffer: bytearray, timeout: float) -> None:
    ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
    if not ready:
        return
    try:
        chunk = os.read(fd, 65536)
    except OSError:
        return
    if chunk:
        buffer.extend(chunk)


def consume_until(fd: int, buffer: bytearray, needle: bytes, timeout: float,
                  *, stop_marker: bytes | None = None) -> int:
    deadline = time.perf_counter() + timeout
    while True:
        found = buffer.find(needle)
        stopped = (
            buffer.find(stop_marker)
            if stop_marker is not None else -1)
        if stopped >= 0 and (found < 0 or stopped < found):
            raise RuntimeError(
                f"{stop_marker!r} appeared before paint marker {needle!r}")
        if found >= 0:
            del buffer[:found + len(needle)]
            return time.perf_counter_ns()
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                f"waiting {needle!r}; tail={bytes(buffer[-600:])!r}")
        read_more(fd, buffer, min(0.02, remaining))


def consume_line(fd: int, buffer: bytearray, timeout: float) -> bytes:
    deadline = time.perf_counter() + timeout
    while True:
        for ending in (b"\r\n", b"\n"):
            found = buffer.find(ending)
            if found >= 0:
                line = bytes(buffer[:found])
                del buffer[:found + len(ending)]
                return line
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                f"waiting child result line; tail={bytes(buffer[-600:])!r}")
        read_more(fd, buffer, min(0.02, remaining))


def run_trial(args: argparse.Namespace, trial_number: int) -> dict:
    values: list[float] = []
    before_cpu = cgroup_cpu_stat()
    task_id = f"lat{trial_number:05d}"
    with tempfile.TemporaryDirectory(
            prefix="zylab-m3-pty-",
            dir=str(args.temp_root)) as bench_home:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", args.rows, args.cols, 0, 0),
        )
        env = os.environ.copy()
        env["HOME"] = bench_home
        env["ZYLAB_BENCH_TOOL_SECONDS"] = str(args.tool_seconds)
        env["ZYLAB_BENCH_TASK_ID"] = task_id
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=REPO_ROOT,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        buffer = bytearray()
        try:
            consume_until(master, buffer, "空闲".encode(), 5.0)
            time.sleep(0.05)
            os.write(master, b"go\r")
            consume_until(
                master, buffer, f"task {task_id}".encode(), 5.0)
            foreground_at = consume_until(
                master, buffer, b"foreground", 1.0)
            time.sleep(args.settle_ms / 1000.0)

            total = args.warmup + args.samples
            blocks = math.ceil(total / args.block_size)
            if blocks > len(SENTINELS):
                raise ValueError(
                    f"samples require {blocks} marker blocks; "
                    f"maximum is {len(SENTINELS)}")
            first_send = None
            last_paint = None
            for index in range(total):
                block = index // args.block_size
                position = index % args.block_size + 1
                time.sleep(JITTER_SECONDS[index % len(JITTER_SECONDS)])
                key = SENTINELS[block]
                marker = key * position
                sent = time.perf_counter_ns()
                if first_send is None:
                    first_send = sent
                os.write(master, key)
                painted = consume_until(
                    master,
                    buffer,
                    marker,
                    0.5,
                    stop_marker=b"AFTER_TOOL",
                )
                last_paint = painted
                if index >= args.warmup:
                    values.append((painted - sent) / 1_000_000)
                if (position == args.block_size
                        and index + 1 < total):
                    os.write(master, b"\x15")  # Ctrl+U
                    time.sleep(0.04)

            os.write(master, b"\x15")
            consume_until(master, buffer, b"AFTER_TOOL", 5.0)
            time.sleep(0.08)
            os.write(master, b"/exit\r")
            consume_until(
                master, buffer, b"BENCH_CHILD_RESULT:", 4.0)
            result_line = consume_line(master, buffer, 1.0)
            child_result = json.loads(result_line.decode("utf-8"))
            proc.wait(timeout=3.0)
            if proc.returncode != 0:
                raise RuntimeError(f"benchmark child exited {proc.returncode}")
            if child_result.get("calls") != 2:
                raise RuntimeError(
                    f"fake provider call count was {child_result.get('calls')}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3.0)
            os.close(master)

    after_cpu = cgroup_cpu_stat()
    throttled_usec = (
        after_cpu.get("throttled_usec", 0)
        - before_cpu.get("throttled_usec", 0))
    return {
        "values": values,
        "n": len(values),
        "p50_ms": nearest_rank(values, 0.50),
        "p95_ms": nearest_rank(values, 0.95),
        "p99_ms": nearest_rank(values, 0.99),
        "max_ms": max(values),
        "sample_window_ms": (
            (last_paint - first_send) / 1_000_000),
        "first_sample_after_foreground_ms": (
            (first_send - foreground_at) / 1_000_000),
        "provider_calls": child_result["calls"],
        "nr_throttled_delta": (
            after_cpu.get("nr_throttled", 0)
            - before_cpu.get("nr_throttled", 0)),
        "throttled_ms_delta": throttled_usec / 1000.0,
    }


def rounded(summary: dict) -> dict:
    return {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in summary.items()
        if key != "values"
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=45)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--block-size", type=int, default=10)
    parser.add_argument(
        "--tool-seconds", "--quiet-seconds",
        dest="tool_seconds", type=float, default=2.0)
    parser.add_argument("--settle-ms", type=float, default=150.0)
    parser.add_argument("--threshold-ms", type=float, default=50.0)
    parser.add_argument(
        "--temp-root", type=Path, default=Path.home())
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--cols", type=int, default=120)
    args = parser.parse_args()
    if (args.samples < 1 or args.trials < 1 or args.warmup < 0
            or args.block_size < 1 or args.tool_seconds <= 0
            or args.settle_ms < 0):
        parser.error(
            "counts/tool-seconds must be positive; "
            "warmup/settle non-negative")
    args.temp_root = args.temp_root.expanduser().resolve()
    if not args.temp_root.is_dir():
        parser.error(f"temp root does not exist: {args.temp_root}")
    return args


def main() -> int:
    args = parse_args()
    trials = [
        run_trial(args, number)
        for number in range(1, args.trials + 1)
    ]
    values = [
        value for trial in trials for value in trial["values"]
    ]
    aggregate = {
        "n": len(values),
        "p50_ms": nearest_rank(values, 0.50),
        "p95_ms": nearest_rank(values, 0.95),
        "p99_ms": nearest_rank(values, 0.99),
        "max_ms": max(values),
    }
    passed = aggregate["p95_ms"] < args.threshold_ms
    result = {
        "method": (
            "PTY key -> InputPump -> Session._turn_managed -> "
            "TaskManager bash -> TerminalRenderer -> PTY painted bytes"
        ),
        "repo_root": str(REPO_ROOT),
        "temp_root": str(args.temp_root),
        "pty": {"rows": args.rows, "cols": args.cols},
        "tool_seconds": args.tool_seconds,
        "settle_ms": args.settle_ms,
        "warmup_per_trial": args.warmup,
        "trials": [rounded(trial) for trial in trials],
        "aggregate": rounded(aggregate),
        "threshold_p95_ms": args.threshold_ms,
        "pass": passed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

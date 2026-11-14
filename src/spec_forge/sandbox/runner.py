"""Isolated execution harness.

Target code never runs in the orchestrator process. ``SandboxRunner`` keeps a persistent
worker subprocess (spawned with ``-s -B``, empty temp cwd, scrubbed environment, POSIX rlimits on
CPU / address space / file size / core dumps) and exchanges newline-delimited JSON jobs with it.
Inside the worker, target modules are executed with *restricted builtins* (no open/eval/exec/
compile/input/globals/vars ...) and a *guarded importer* that only admits allowlisted modules;
every target call is wrapped in a SIGALRM wall-clock budget, and each job has a hard timeout
after which the worker is killed and respawned.

``isolation="inprocess"`` runs the identical job handlers in-process (same restricted module
loading, no process boundary) for fast unit tests.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from .. import codec
from ..config import SandboxPolicy

TARGET_FILENAME = "<specforge-target>"
_SRC_ROOT = str(Path(__file__).resolve().parents[2])

REMOVED_BUILTINS = {"open", "eval", "exec", "compile", "input", "breakpoint", "globals", "locals", "vars", "help",
                    "exit", "quit", "memoryview", "copyright", "credits", "license", "setattr", "delattr"}


class SandboxViolation(ImportError):
    pass


def restricted_builtins(allowed_imports: tuple[str, ...]) -> dict[str, Any]:
    real_import = builtins.__import__
    allowed_full = set(allowed_imports)
    allowed_roots = {m.split(".")[0] for m in allowed_imports}

    def guarded_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0):
        if level != 0 or (name not in allowed_full and name.split(".")[0] not in allowed_roots):
            raise SandboxViolation(f"import of `{name}` blocked by sandbox policy")
        return real_import(name, globals_, locals_, fromlist, level)

    safe = {k: v for k, v in vars(builtins).items() if k not in REMOVED_BUILTINS}
    safe["__import__"] = guarded_import
    return safe


def load_module(source: str, policy: SandboxPolicy) -> dict[str, Any]:
    code = compile(source, TARGET_FILENAME, "exec")
    ns: dict[str, Any] = {"__name__": "specforge_target", "__builtins__": restricted_builtins(policy.allowed_imports)}
    exec(code, ns)  # noqa: S102 - the whole point of the sandbox worker
    return ns


# --------------------------------------------------------------------------- job handlers (worker side)


def _context(job: dict[str, Any], policy: SandboxPolicy):
    from ..symbolic.invariants import CheckContext, TargetSpec, build_properties

    ns = load_module(job["source"], policy)
    spec = TargetSpec.from_dict(job["spec"])
    timeout = float(job.get("fuzz", {}).get("per_call_timeout_s", 0.5))
    ctx = CheckContext(ns, spec, timeout_s=timeout)
    return ctx, build_properties(ctx)


def handle(job: dict[str, Any]) -> dict[str, Any]:
    from ..config import FuzzConfig, ShrinkConfig

    policy = SandboxPolicy(**{k: tuple(v) if isinstance(v, list) else v for k, v in job.get("policy", {}).items()})
    op = job.get("op")
    t0 = time.perf_counter()
    if op == "ping":
        return {"ok": True, "pid": os.getpid()}
    try:
        ctx, props = _context(job, policy)
    except Exception as exc:  # noqa: BLE001 - load failures are results, not crashes
        return {"ok": False, "load_error": f"{type(exc).__name__}: {exc}"}
    if ctx.target is None:
        return {"ok": False, "load_error": f"target `{ctx.spec.name}` not defined after patching"}
    fuzz = FuzzConfig(**{k: tuple(v) if isinstance(v, list) else v for k, v in job.get("fuzz", {}).items()})
    shrink_cfg = ShrinkConfig(**job.get("shrink_cfg", {}))
    if op == "campaign":
        from ..agents.breaker_agent import run_campaign

        res = run_campaign(ctx, props, fuzz, shrink_cfg, trials=job.get("trials"), seed=job.get("seed"),
                           shrink=job.get("shrink", True), stop_early=job.get("stop_early", True))
        out = res.to_dict()
    elif op == "evaluate":
        from ..agents.builder_agent import evaluate_candidate

        out = evaluate_candidate(ctx, props, fuzz, job["cases"], job.get("quick_trials", 150), job.get("seed", 0))
    elif op == "trace":
        from ..agents.builder_agent import trace_cases

        out = trace_cases(ctx, props, job["failing"], job["passing"])
    elif op == "replay":
        from ..symbolic.invariants import check_case

        out = {"results": [check_case(ctx, props, c) for c in job["cases"]]}
    else:
        return {"ok": False, "error": f"unknown op {op!r}"}
    from ..symbolic.invariants import TAINTED

    out["ok"] = True
    out["tainted"] = TAINTED["stray_threads"] > 0
    out["job_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return out


def worker_main() -> None:  # pragma: no cover - executed in the subprocess
    proto = sys.stdout
    for line in sys.stdin:
        if not line.strip():
            continue
        sink = io.StringIO()
        sys.stdout = sink  # target prints must not corrupt the protocol channel
        try:
            job = codec.loads(line)
            result = handle(job)
        except BaseException as exc:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]}
        finally:
            sys.stdout = proto
        proto.write(codec.dumps(result) + "\n")
        proto.flush()


_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
         "from spec_forge.sandbox.runner import worker_main; worker_main()")


def _apply_limits(policy: SandboxPolicy):  # pragma: no cover - runs in the child before exec
    def fn() -> None:
        try:
            import resource

            os.setsid()
            for lim, val in ((resource.RLIMIT_CPU, policy.cpu_seconds), (resource.RLIMIT_CORE, 0),
                             (resource.RLIMIT_FSIZE, 1 << 20)):
                try:
                    _soft, hard = resource.getrlimit(lim)
                    soft = val if hard == resource.RLIM_INFINITY else min(val, hard)
                    resource.setrlimit(lim, (soft, hard))
                except (ValueError, OSError):
                    pass
            if sys.platform.startswith("linux"):
                try:
                    mem = policy.memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
                except (ValueError, OSError):
                    pass
        except Exception:
            pass

    return fn


class SandboxRunner:
    """Persistent sandbox worker pool of size one (serialised jobs, restart on fault)."""

    RECYCLE_AFTER = 400

    def __init__(self, policy: Optional[SandboxPolicy] = None) -> None:
        self.policy = policy or SandboxPolicy()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._jobs = 0
        self._tmp: Optional[tempfile.TemporaryDirectory[str]] = None
        self.restarts = 0
        self.timeouts = 0
        self.memory_kills = 0

    # ------------------------------------------------------------------ lifecycle
    def _spawn(self) -> subprocess.Popen[str]:
        self._tmp = self._tmp or tempfile.TemporaryDirectory(prefix="specforge-sbx-")
        env = {"PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
        self._proc = subprocess.Popen(
            [sys.executable, "-s", "-B", "-c", _BOOT, _SRC_ROOT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            cwd=self._tmp.name, env=env, preexec_fn=_apply_limits(self.policy) if os.name == "posix" else None,
            bufsize=1, encoding="utf-8",
        )
        self._jobs = 0
        return self._proc

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
            self._proc = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self) -> "SandboxRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None
            self.restarts += 1

    # ------------------------------------------------------------------ watchdog
    @staticmethod
    def _rss_mb(pid: int) -> float:
        """Current resident set size of the worker (macOS ignores RLIMIT_AS, so we police it here)."""
        statm = f"/proc/{pid}/statm"
        try:
            if os.path.exists(statm):
                with open(statm) as fh:
                    return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=2).stdout.strip()
            return int(out) / 1024 if out else 0.0
        except Exception:
            return 0.0

    def _await(self, proc: subprocess.Popen[str], timeout: float) -> Optional[str]:
        """Wait for a response line; enforce wall-clock and memory caps. Returns an error or None."""
        deadline = time.monotonic() + timeout
        poll = 0.05
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.timeouts += 1
                return f"sandbox job exceeded {timeout:.1f}s and was killed"
            ready, _, _ = select.select([proc.stdout], [], [], min(poll, remaining))
            if ready:
                return None
            if self._rss_mb(proc.pid) > self.policy.memory_mb:
                self.memory_kills += 1
                return f"sandbox worker exceeded memory cap ({self.policy.memory_mb} MB) and was killed"
            poll = min(0.2, poll * 1.5)

    # ------------------------------------------------------------------ jobs
    def submit(self, job: dict[str, Any], timeout_s: Optional[float] = None) -> dict[str, Any]:
        job = dict(job)
        job.setdefault("policy", {k: list(v) if isinstance(v, tuple) else v for k, v in self.policy.__dict__.items()})
        payload = codec.dumps(job)
        if self.policy.isolation == "inprocess":
            try:
                return codec.loads(codec.dumps(handle(codec.loads(payload))))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        timeout = timeout_s or self.policy.job_timeout_s
        for attempt in range(2):
            if self._proc is None or self._proc.poll() is not None or self._jobs >= self.RECYCLE_AFTER:
                if self._proc is not None:
                    self._kill()
                self._spawn()
            proc = self._proc
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._kill()
                continue
            self._jobs += 1
            verdict = self._await(proc, timeout)
            if verdict is not None:
                self._kill()
                return {"ok": False, "error": verdict}
            line = proc.stdout.readline()
            if not line:
                code = proc.poll()
                self._kill()
                if attempt == 0:
                    continue
                return {"ok": False, "error": f"sandbox worker died (exit={code})"}
            try:
                result = codec.loads(line)
            except json.JSONDecodeError:
                self._kill()
                return {"ok": False, "error": "sandbox protocol corruption"}
            if isinstance(result, dict) and result.get("tainted"):
                self._kill()  # stray threads from a hung target: recycle the worker
            return result
        return {"ok": False, "error": "sandbox worker unavailable"}

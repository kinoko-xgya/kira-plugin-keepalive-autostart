"""Autostart the keepalive supervisor after container boot.

Why this plugin exists
----------------------
keepalive is the in-container supervisor for long-running scripts (no cron / no
systemd in the image). Nothing supervises keepalive itself: on 2026-11-09 a power
cut brought the container back up, but keepalive stayed down, and gemai_daemon plus
its watchdog were left dead for nearly two hours until startup_check.sh was run by
hand.

The framework offers no entry point for keepalive -- the image ENTRYPOINT is
`python main.py`, so wiring it there would mean touching Dockerfile/compose. Plugins
do have ON_LOADED, which fires on every container start, so that is the hook we use.

Design notes
------------
* Idempotent: scan /proc for keepalive.py before spawning. On container restart the
  old process is gone anyway, but on a plugin hot reload it is still alive and must
  not be duplicated.
* Non-blocking: delay a few seconds, do the work in a worker thread, and start the
  child with start_new_session so it outlives a framework restart.
* Quiet on failure: exceptions are logged, never raised, so a broken supervisor can
  not take the whole plugin (and therefore the bot) down.
* Own log file: <scripts_dir>/keepalive_autostart.log, kept separate from
  keepalive.log so it stays obvious which process started what.

Note: ON_LOADED does not replay on a single-plugin hot reload, so initialize() runs
the same check as a fallback. The plugin guarantees "started once at boot", not
"resurrected whenever it dies" -- the latter is the watchdog's job.
"""

import asyncio
import os
import subprocess
import time

from core.plugin import BasePlugin, logger, on

LOG_NAME = "keepalive_autostart.log"
DEFAULT_SCRIPTS_DIR = "/app/data/files/scripts"
DEFAULT_PYTHON = "python3"


class KeepaliveAutostartPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        enabled = cfg.get("enabled")
        self._enabled = True if enabled is None else bool(enabled)

        scripts_dir = cfg.get("scripts_dir")
        self._scripts_dir = str(scripts_dir) if scripts_dir else DEFAULT_SCRIPTS_DIR

        python_bin = cfg.get("python_bin")
        self._python = str(python_bin) if python_bin else DEFAULT_PYTHON

        delay = cfg.get("startup_delay")
        self._delay = max(0, int(10 if delay is None else delay))

        run_once = cfg.get("run_once_after_start")
        self._run_once = True if run_once is None else bool(run_once)

        watch = cfg.get("watch_interval")
        self._watch_interval = max(0, int(600 if watch is None else watch))

        self._check_started = False
        self._watch_task: asyncio.Task | None = None
        self._fail_streak = 0

    # ---------- logging ----------

    def _log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        logger.info(f"[keepalive_autostart] {msg}")
        try:
            path = os.path.join(self._scripts_dir, LOG_NAME)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # a failed log write must never break the actual work

    # ---------- liveness ----------

    @staticmethod
    def _keepalive_pid() -> int | None:
        """Return the pid of a running keepalive.py, or None.

        Reads /proc/*/cmdline instead of the pidfile on purpose: the pidfile is
        written by keepalive itself and goes stale after a kill -9. One-shot
        invocations (--once / --status) are excluded so they never mask a dead
        supervisor.
        """
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except Exception:
            return None
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if "keepalive.py" in cmd and "--once" not in cmd and "--status" not in cmd:
                return int(pid)
        return None

    # ---------- actions ----------

    def _start_keepalive(self) -> str:
        """Run in a worker thread; returns a human-readable outcome."""
        script = os.path.join(self._scripts_dir, "keepalive.py")
        if not os.path.exists(script):
            return f"skipped: {script} not found"

        pid = self._keepalive_pid()
        if pid:
            return f"already running pid={pid}, spawn skipped"

        try:
            # start_new_session detaches the child from this process group so a
            # framework restart does not SIGHUP it away.
            subprocess.Popen(
                [self._python, script],
                cwd=self._scripts_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            return f"spawn failed: {type(e).__name__}: {e}"

        # Popen succeeding only means fork worked. Poll briefly for a real pid,
        # because keepalive can exit immediately on a stale pidfile.
        for _ in range(6):
            time.sleep(0.5)
            pid = self._keepalive_pid()
            if pid:
                msg = f"started pid={pid}"
                if self._run_once:
                    msg += "; " + self._run_once_check()
                return msg
        return "no process seen 3s after spawn; check keepalive.log"

    def _run_once_check(self) -> str:
        """Run one supervisor pass so already-dead children come back now.

        keepalive waits a full interval before its first check, so without this a
        crashed child would stay down for up to a minute after boot.
        """
        script = os.path.join(self._scripts_dir, "keepalive.py")
        try:
            p = subprocess.run(
                [self._python, script, "--once"],
                cwd=self._scripts_dir,
                capture_output=True,
                timeout=120,
            )
            out = (p.stdout or b"").decode("utf-8", "ignore").strip()
            tail = out.splitlines()[-1] if out else "(no output)"
            return f"first pass: {tail}"
        except subprocess.TimeoutExpired:
            return "first pass timed out (120s)"
        except Exception as e:
            return f"first pass failed: {type(e).__name__}"

    # ---------- lifecycle ----------

    async def initialize(self):
        """Required by BasePlugin; also the hot-reload fallback for ON_LOADED."""
        self._log(f"init enabled={self._enabled} scripts_dir={self._scripts_dir}")
        if not self._enabled:
            return
        self._spawn_check("initialize")
        self._start_watcher()

    def _spawn_check(self, source: str) -> None:
        """Kick off the boot check in the background. Runs once per instance."""
        if self._check_started:
            return
        self._check_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._log(f"[{source}] no event loop, running inline: " + self._start_keepalive())
            return
        loop.create_task(self._delayed_check(source))

    async def _delayed_check(self, source: str) -> None:
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            result = await asyncio.to_thread(self._start_keepalive)
            self._log(f"[{source}] {result}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(f"[{source}] error: {type(e).__name__}: {e}")

    @on.loaded()
    async def autostart(self, *_):
        """Fires on container start. Hot reloads are covered by initialize()."""
        if not self._enabled:
            return
        self._spawn_check("loaded")

    # ---------- watchdog ----------

    def _start_watcher(self) -> None:
        if self._watch_interval <= 0:
            self._log("watchdog disabled (watch_interval=0)")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # Write a heartbeat line immediately; otherwise the first interval after a
        # restart looks identical to "the watchdog never started".
        self._touch_state(self._keepalive_pid())
        self._watch_task = loop.create_task(self._watch_loop())
        self._log(f"watchdog started interval={self._watch_interval}s")

    def _touch_state(self, pid) -> None:
        """Overwrite a one-line heartbeat so the last check time is always readable."""
        try:
            path = os.path.join(self._scripts_dir, "keepalive_watch.state")
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"alive={bool(pid)} pid={pid or '-'} interval={self._watch_interval}\n"
                )
        except Exception:
            pass

    async def _watch_loop(self) -> None:
        """Scan /proc once per interval and respawn keepalive when it is gone.

        keepalive writes no heartbeat of its own (the heartbeat files belong to the
        tasks it supervises), so process presence is the only observable signal.
        A hung-but-alive keepalive is invisible here by design; that belongs to
        keepalive's own loop.
        """
        try:
            while True:
                await asyncio.sleep(self._watch_interval)
                pid = await asyncio.to_thread(self._keepalive_pid)
                # Always leave a trace: staying silent while healthy makes
                # "watchdog alive" and "watchdog dead" look the same from outside.
                self._touch_state(pid)
                if pid:
                    if self._fail_streak:
                        self._log(f"[watch] recovered pid={pid} after {self._fail_streak} failed rounds")
                        self._fail_streak = 0
                    continue

                self._log("[watch] keepalive gone, respawning")
                result = await asyncio.to_thread(self._start_keepalive)
                if "started" in result or "already running" in result:
                    self._log(f"[watch] {result}")
                    if self._fail_streak:
                        self._log(f"[watch] recovered after {self._fail_streak} failed rounds")
                    self._fail_streak = 0
                else:
                    self._fail_streak += 1
                    self._log(f"[watch] respawn failed (round {self._fail_streak}): {result}")
        except asyncio.CancelledError:
            self._log("[watch] watchdog cancelled")
            raise
        except Exception as e:
            self._fail_streak += 1
            self._log(f"[watch] unexpected error: {type(e).__name__}: {e}")

    async def terminate(self):
        # keepalive is deliberately left running: it is an independent daemon and a
        # plugin unload must not kill it. The watchdog, however, must be cancelled
        # or every hot reload leaks another never-ending coroutine.
        task, self._watch_task = self._watch_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        self._log("watchdog stopped")

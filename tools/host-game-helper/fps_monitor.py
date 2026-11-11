"""Read per-game frame timings from the optional PresentMon console executable."""

import csv
import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


class FpsMonitor:
    def __init__(self):
        self._process = None
        self._pid = None
        self._samples = deque(maxlen=5000)
        self._lock = threading.Lock()
        self._last_attempt = 0.0

    def _executable(self):
        configured = os.environ.get("PRESENTMON_PATH", "")
        local = Path(__file__).resolve().parent / "PresentMon.exe"
        return configured or (str(local) if local.is_file() else shutil.which("PresentMon.exe"))

    def _read(self, process):
        try:
            reader = csv.DictReader(process.stdout)
            for row in reader:
                fields = {str(k).lower(): v for k, v in row.items() if k}
                try:
                    ms = float(fields.get("msbetweenpresents", ""))
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(ms) or ms <= 0:
                    continue
                with self._lock:
                    if self._process is not process:
                        break
                    self._samples.append((time.monotonic(), ms))
        finally:
            process.stdout.close()

    def stop(self):
        with self._lock:
            process, self._process = self._process, None
            self._pid = None
            self._samples.clear()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def sample(self, pid):
        pid = int(pid or 0)
        if not pid:
            self.stop()
            return None
        if self._pid and self._pid != pid:
            self._last_attempt = 0.0
        if self._pid != pid or (self._process and self._process.poll() is not None):
            self.stop()
        if self._process is None:
            now = time.monotonic()
            if now - self._last_attempt < 10:
                return None
            self._last_attempt = now
            executable = self._executable()
            if not executable:
                return None
            try:
                process = subprocess.Popen(
                    [executable, "--process_id", str(pid), "--output_stdout", "--v1_metrics",
                     "--no_track_display", "--no_track_gpu",
                     "--session_name", f"TuringHost-{os.getpid()}", "--stop_existing_session"],
                    stdout=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                print(f"FPS: cannot start PresentMon: {exc}", flush=True)
                return None
            self._process, self._pid = process, pid
            threading.Thread(target=self._read, args=(process,), daemon=True).start()
        with self._lock:
            cutoff = time.monotonic() - 2.0
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            samples = list(self._samples)
        if not samples:
            return None
        return 1000.0 * len(samples) / sum(ms for _, ms in samples)

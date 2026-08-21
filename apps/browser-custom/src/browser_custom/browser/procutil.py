"""Find and terminate only browser processes belonging to an exact user-data-dir."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict, cast

import psutil  # type: ignore[import-untyped]

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

_MACOS_ACTIVATE_SCRIPT = """
ObjC.import("AppKit");
function run(argv) {
  const pid = Number(argv[0]);
  const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(pid);
  if (!app) return "false";
  const options = $.NSApplicationActivateAllWindows | $.NSApplicationActivateIgnoringOtherApps;
  return app.activateWithOptions(options) ? "true" : "false";
}
"""


class ProcessStats(TypedDict):
    mainPids: list[int]
    processCount: int


def cloak_seed(acc: str) -> int:
    return int(hashlib.sha1(acc.encode("utf-8")).hexdigest()[:8], 16)


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _cmdline_data_dir(cmdline: list[str]) -> str | None:
    for index, token in enumerate(cmdline):
        if token.startswith("--user-data-dir="):
            return token.split("=", 1)[1]
        if token == "--user-data-dir" and index + 1 < len(cmdline):
            return cmdline[index + 1]
    return None


def _is_main_process(cmdline: list[str]) -> bool:
    return not any(token.startswith("--type=") for token in cmdline)


def process_stats_many(data_dirs: list[Path]) -> list[ProcessStats]:
    """Take one process-table snapshot and group Chromium trees by profile."""
    if not data_dirs:
        return []

    targets = [_norm(path) for path in data_dirs]
    wanted = set(targets)
    main_pids: dict[str, list[int]] = {target: [] for target in wanted}
    children: dict[int, list[int]] = {}
    try:
        for process in psutil.process_iter(["pid", "ppid", "cmdline"]):
            try:
                pid = int(process.info["pid"])
                ppid = int(process.info.get("ppid") or 0)
                children.setdefault(ppid, []).append(pid)
                command = cast(list[str], process.info["cmdline"] or [])
                found = _cmdline_data_dir(command)
                target = _norm(found) if found else None
                if target in wanted and _is_main_process(command):
                    main_pids[target].append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except (psutil.Error, PermissionError, OSError):
        # Some managed macOS environments deny the sysctl process listing.
        # Status refresh should degrade to "stopped" instead of failing the API.
        return [{"mainPids": [], "processCount": 0} for _path in data_dirs]

    by_target: dict[str, ProcessStats] = {}
    for target in wanted:
        roots = main_pids[target]
        seen: set[int] = set()
        pending = list(roots)
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            pending.extend(children.get(pid, []))
        by_target[target] = {"mainPids": roots, "processCount": len(seen)}
    return [by_target[target] for target in targets]


def find_main_pids_for(data_dir: Path) -> list[int]:
    return process_stats_many([data_dir])[0]["mainPids"]


def activate_for_data_dir(data_dir: Path) -> bool:
    """Activate the exact macOS app process that owns this browser profile."""
    if not IS_MACOS:
        return False
    for pid in reversed(find_main_pids_for(data_dir)):
        try:
            result = subprocess.run(
                [
                    "/usr/bin/osascript", "-l", "JavaScript",
                    "-e", _MACOS_ACTIVATE_SCRIPT, str(pid),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip() == "true":
            return True
    return False


def tree_kill(pid: int, timeout: float = 5.0) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    processes = parent.children(recursive=True) + [parent]
    for process in processes:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def kill_for_data_dir(data_dir: Path) -> list[int]:
    pids = find_main_pids_for(data_dir)
    for pid in pids:
        tree_kill(pid)
    return pids


def wait_for_exit(data_dir: Path, timeout: float = 3.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = find_main_pids_for(data_dir)
        if not remaining:
            return []
        time.sleep(0.1)
    return find_main_pids_for(data_dir)


def process_stats(data_dir: Path) -> ProcessStats:
    return process_stats_many([data_dir])[0]

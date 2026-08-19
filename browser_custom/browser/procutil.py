"""Find and terminate only browser processes belonging to an exact user-data-dir."""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import psutil

IS_WINDOWS = sys.platform.startswith("win")


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


def find_main_pids_for(data_dir: Path) -> list[int]:
    target = _norm(data_dir)
    result: list[int] = []
    try:
        for process in psutil.process_iter(["pid", "cmdline"]):
            try:
                command = process.info["cmdline"] or []
                found = _cmdline_data_dir(command)
                if found and _norm(found) == target and _is_main_process(command):
                    result.append(int(process.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except (psutil.Error, PermissionError, OSError):
        # Some managed macOS environments deny the sysctl process listing.
        # Status refresh should degrade to "stopped" instead of failing the API.
        return []
    return result


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


def process_stats(data_dir: Path) -> dict:
    main_pids = find_main_pids_for(data_dir)
    seen: set[int] = set()
    rss = 0
    for pid in main_pids:
        try:
            root = psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for process in processes:
            if process.pid in seen:
                continue
            seen.add(process.pid)
            try:
                rss += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return {
        "mainPids": main_pids,
        "processCount": len(seen),
        "rssBytes": rss,
    }

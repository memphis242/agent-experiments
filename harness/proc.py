"""Subprocess helper that owns a whole process group."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_group(cmd: Sequence[str], *, cwd: Path | None, env: Mapping[str, str] | None,
              timeout: float | None) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run(capture_output, text) but in a new session.

    On timeout (or any interruption) the whole process group is killed and
    reaped before the exception propagates, so nothing is orphaned.
    """
    proc = subprocess.Popen(list(cmd), cwd=cwd, env=None if env is None else dict(env),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        kill_group(proc.pid)
        proc.communicate()
        raise
    # Anything still in the group after the leader exited is a leftover.
    kill_group(proc.pid)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)

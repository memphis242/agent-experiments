"""Per-check isolation of live checks in nested unprivileged namespaces (see harness/ACCEPTANCE_CONTRACT.md).

Every group of live checks runs in its own
  user namespace (root inside, used only to set up the others),
  network namespace with its own loopback (brought up here), so the fixed node ports never collide,
  mount namespace with a private tmpfs on /dev/shm (the OSAL RAM disk) and its own /proc,
  IPC namespace (POSIX message queues: OSAL names them after the PID, and PIDs repeat across namespaces),
  PID namespace (unshare --kill-child): everything the group started dies with it, and pgrep/pkill see only it,
and the group's code then runs in a further user namespace that maps the caller's uid and gid back, so the cFS
nodes never run as uid 0 (OSAL skips its message-queue depth adaptation for root and cFS then fails to boot).
Standard library and util-linux (unshare, setpriv) only.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

# Bring lo up from inside the new network namespace without depending on iproute2.
LO_UP = ("import fcntl, socket, struct; s = socket.socket(); "
         "r = fcntl.ioctl(s, 0x8913, struct.pack('16sH14x', b'lo', 0)); "
         "f = struct.unpack('16sH14x', r)[1] | 1; "
         "fcntl.ioctl(s, 0x8914, struct.pack('16sH14x', b'lo', f))")


def wrap(argv: list[str]) -> list[str]:
    """`argv` run inside a fresh isolated group (see the module docstring). The group is killed when the process
    that started it exits, even by SIGKILL (setpriv --pdeathsig, then unshare --kill-child on the group's PID 1)."""
    assert argv
    uid, gid = os.getuid(), os.getgid()
    inner = shlex.join(["unshare", "-U", f"--map-user={uid}", f"--map-group={gid}", *argv])
    queue_setup = ("from pathlib import Path; "
                   "Path('/proc/sys/fs/mqueue/msg_max').write_text('512'); "
                   "print('queue namespace msg_max=512')")
    steps = [shlex.join([sys.executable, "-I", "-c", LO_UP]), "mount -t tmpfs tmpfs /dev/shm",
             shlex.join([sys.executable, "-I", "-c", queue_setup]), inner]
    # sh stays PID 1 of the group (the trailing exit stops it exec'ing the last command), so the group's
    # processes are reaped while it runs and all of them die with it.
    script = " && ".join(steps) + "; exit $?"
    return ["setpriv", "--pdeathsig", "KILL", "unshare", "-U", "-r", "-n", "-m", "--propagation", "private", "-i",
            "-p", "-f", "--mount-proc", "--kill-child", "sh", "-c", script]


def available() -> tuple[bool, str]:
    """Whether isolated groups can be created here (nested unprivileged namespaces, lo, tmpfs)."""
    probe = (f"import socket, os; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('127.0.0.1', 1234)); "
             f"assert os.getuid() == {os.getuid()}, os.getuid(); open('/dev/shm/t5-probe', 'w').write('x'); print('ok')")
    try:
        proc = subprocess.run(wrap([sys.executable, "-I", "-c", probe]), capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    ok = proc.returncode == 0 and proc.stdout.strip().endswith("ok")
    return ok, (proc.stdout + proc.stderr).strip()[-400:]

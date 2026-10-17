#!/usr/bin/env python3
"""Prepare a fresh network and mount namespace, then exec the given command.

Usage: ns_setup.py -- <command> [args...]

Run as the first process of `unshare -rnm`: brings the loopback interface up
and gives the process a private tmpfs on /dev/shm (where cFS keeps its RAM
disk), so concurrent cFS instances on the host cannot collide with this one.
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import socket
import struct
import sys

SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: ns_setup.py -- command [args...]", file=sys.stderr)
        return 2
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        req = struct.pack("16sH14s", b"lo", 0, b"\0" * 14)
        flags = struct.unpack("16sH14s", fcntl.ioctl(s, SIOCGIFFLAGS, req))[1]
        fcntl.ioctl(s, SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", flags | IFF_UP, b"\0" * 14))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.mount(b"tmpfs", b"/dev/shm", b"tmpfs", 0, b"mode=1777") != 0:
        err = ctypes.get_errno()
        print(f"cannot mount tmpfs on /dev/shm: {os.strerror(err)}", file=sys.stderr)
        return 3
    os.execvp(argv[2], argv[2:])
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Linux POSIX queue prerequisites, measured as the final node identity.

OSAL unlinks queues immediately. The inventory therefore uses process file
descriptors rather than directory listings. Allocation probes are authoritative
for kernel bookkeeping costs and queues charged to this uid in other namespaces.
"""
from __future__ import annotations

import ctypes
import os
import resource
import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from common import EnvironmentFail, Fail

REQUIRED_MSG_MAX = 512
PIPE_MINIMA = (("TLMO", 128), ("MODE", 128), ("SFI", 256))
# Linux msg_msg has six native words; posix_msg_tree_node has six as well.
# Allocation probes remain authoritative for the running kernel's accounting.
MQ_STRUCT_OVERHEAD = 12 * struct.calcsize("P")


@dataclass(frozen=True)
class Queue:
    name: str
    depth: int
    message_size: int
    pid: int = 0

    @property
    def reserved_bytes(self) -> int:
        # Conservative allowance for msg_msg and priority-tree nodes on Linux.
        return self.depth * (self.message_size + MQ_STRUCT_OVERHEAD)


class MqAttr(ctypes.Structure):
    _fields_ = (("flags", ctypes.c_long), ("maxmsg", ctypes.c_long),
                ("msgsize", ctypes.c_long), ("curmsgs", ctypes.c_long),
                ("reserved", ctypes.c_long * 4))


def _libc():  # noqa: ANN202
    lib = ctypes.CDLL(None, use_errno=True)
    lib.mq_open.restype = ctypes.c_int
    lib.mq_getattr.argtypes = (ctypes.c_int, ctypes.POINTER(MqAttr))
    lib.mq_close.argtypes = (ctypes.c_int,)
    lib.mq_unlink.argtypes = (ctypes.c_char_p,)
    return lib


def validate_limits(msg_max: int, soft_limit: int, inventory: tuple[Queue, ...], *, uid: int) -> int:
    """Validate evidence; never substitute a truncated capacity for the request."""
    if uid == 0:
        raise EnvironmentFail("ENVIRONMENT queue preflight: expected final non-root node identity; observed uid 0")
    if msg_max < REQUIRED_MSG_MAX:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight: expected msg_max >=512; observed {msg_max}")
    required = sum(q.reserved_bytes for q in inventory)
    if soft_limit != resource.RLIM_INFINITY and soft_limit < required:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight: expected RLIMIT_MSGQUEUE >= {required} bytes "
                              f"including kernel overhead; observed {soft_limit}; queues={len(inventory)}")
    return required


def allocation_probe(inventory: tuple[Queue, ...]) -> tuple[Queue, ...]:
    """Allocate every requested queue concurrently, inspect attrs, then release.

    Existing same-user allocations remain charged during the probe. This tests
    usable headroom, including allocations invisible outside the IPC namespace.
    """
    lib = _libc()
    opened: list[tuple[int, bytes]] = []
    measured: list[Queue] = []
    try:
        for i, q in enumerate(inventory):
            name = f"/t5-{os.getpid()}-{uuid.uuid4().hex[:10]}-{i}".encode("ascii")
            attr = MqAttr(0, q.depth, q.message_size, 0)
            fd = lib.mq_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, ctypes.byref(attr))
            if fd < 0:
                error = ctypes.get_errno()
                raise EnvironmentFail(f"ENVIRONMENT queue preflight: allocation {q.name} depth={q.depth} "
                                      f"size={q.message_size} failed: errno {error} ({os.strerror(error)}); "
                                      "raise msg_max/queues_max and RLIMIT_MSGQUEUE for the node uid")
            opened.append((fd, name))
            got = MqAttr()
            if lib.mq_getattr(fd, ctypes.byref(got)) != 0:
                raise EnvironmentFail(f"ENVIRONMENT queue preflight: mq_getattr failed for {q.name}")
            if got.maxmsg != q.depth or got.msgsize != q.message_size:
                raise EnvironmentFail(f"ENVIRONMENT queue preflight: {q.name} requested {q.depth}/{q.message_size}, "
                                      f"observed {got.maxmsg}/{got.msgsize}; truncation cannot satisfy depth")
            measured.append(Queue(q.name, got.maxmsg, got.msgsize))
    finally:
        errors = []
        for fd, name in opened:
            if lib.mq_close(fd) != 0:
                errors.append(f"mq_close({name.decode()})")
            if lib.mq_unlink(name) != 0:
                errors.append(f"mq_unlink({name.decode()})")
        if errors:
            raise EnvironmentFail("ENVIRONMENT queue preflight cleanup failed: " + ", ".join(errors))
    return tuple(measured)


def namespace_preflight(nodes: int = 2) -> dict[str, object]:
    assert nodes in (1, 2)
    try:
        msg_max = int(Path("/proc/sys/fs/mqueue/msg_max").read_text())
        soft, hard = resource.getrlimit(resource.RLIMIT_MSGQUEUE)
    except (OSError, ValueError) as exc:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight cannot read namespace limits: {exc}") from exc
    inventory = tuple(Queue(f"cpu{n}-{app}", depth, struct.calcsize("P"))
                      for n in range(1, nodes + 1) for app, depth in PIPE_MINIMA)
    required = validate_limits(msg_max, soft, inventory, uid=os.geteuid())
    measured = allocation_probe(inventory + (Queue("kernel-depth-512", 512, struct.calcsize("P")),))
    return {"ipc_namespace": os.readlink("/proc/self/ns/ipc"), "uid": os.geteuid(),
            "msg_max": msg_max, "rlimit_soft": soft, "rlimit_hard": hard,
            "minimum_reserved_bytes": required,
            "probes": tuple((q.name, q.depth, q.message_size) for q in measured)}


def process_inventory(pid: int) -> tuple[Queue, ...]:
    """Inspect unlinked OSAL queue descriptors inside this process's IPC namespace."""
    own = os.readlink("/proc/self/ns/ipc")
    if os.readlink(f"/proc/{pid}/ns/ipc") != own:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight: pid {pid} is in a different IPC namespace")
    status = Path(f"/proc/{pid}/status").read_text()
    match = re.search(r"(?m)^Uid:\s+\d+\s+(\d+)", status)
    if match is None or int(match.group(1)) != os.geteuid() or int(match.group(1)) == 0:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight: expected pid {pid} to use final non-root uid {os.geteuid()}")
    node_soft, _node_hard = resource.prlimit(pid, resource.RLIMIT_MSGQUEUE)
    lib = _libc()
    inventory = []
    try:
        paths = tuple(Path(f"/proc/{pid}/fd").iterdir())
        for path in paths:
            try:
                name = os.readlink(path)
                if not re.match(r"^/\d+\.", name):
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            try:
                attr = MqAttr()
                if lib.mq_getattr(fd, ctypes.byref(attr)) == 0:
                    inventory.append(Queue(name, attr.maxmsg, attr.msgsize, pid))
            finally:
                os.close(fd)
    except OSError as exc:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight: cannot inventory pid {pid}: {exc}") from exc
    if not inventory:
        raise EnvironmentFail(f"ENVIRONMENT queue preflight: no readable OSAL queues for pid {pid}")
    validate_limits(int(Path("/proc/sys/fs/mqueue/msg_max").read_text()), node_soft,
                    tuple(inventory), uid=os.geteuid())
    return tuple(inventory)


def verify_capacities(inventory: tuple[Queue, ...], node: str) -> None:
    for app, minimum in PIPE_MINIMA:
        found = tuple(q for q in inventory if app in q.name.upper())
        if not found:
            raise Fail(f"{node} queue capacity: expected an identifiable {app} pipe; observed none")
        # cFE can create additional command pipes; at least its main data pipe
        # must meet the required depth.
        depth = max(q.depth for q in found)
        if depth < minimum:
            raise Fail(f"{node} {app} pipe: expected depth >= {minimum}; observed mq_getattr depth {depth}; "
                       "OSAL truncation does not satisfy the requirement")


def verify_nodes(pids: tuple[tuple[str, int], ...]) -> tuple[Queue, ...]:
    inventory = []
    for name, pid in pids:
        queues = process_inventory(pid)
        verify_capacities(queues, name)
        inventory.extend(queues)
    result = tuple(inventory)
    soft, _hard = resource.getrlimit(resource.RLIMIT_MSGQUEUE)
    validate_limits(int(Path("/proc/sys/fs/mqueue/msg_max").read_text()), soft, result, uid=os.geteuid())
    return result

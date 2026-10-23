"""Pack-side prerequisites and timing decisions without OS queues or flight nodes."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import resource
import struct
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import frames as fr
import preflight as pf
import timing
from common import EnvironmentFail, Fail
from node import Node
from types import SimpleNamespace


class SerialTests(unittest.TestCase):
    def test_sources_nodes_and_parallel_producers(self) -> None:
        serials = fr.SerialAllocator()
        with ThreadPoolExecutor(max_workers=16) as pool:
            got = tuple(pool.map(lambda _i: serials.next(1, 2), range(512)))
        self.assertEqual(sorted(got), list(range(1, 513)))
        self.assertEqual(serials.next(1, 1), 1)
        self.assertEqual(serials.next(2, 2), 1)
        self.assertEqual(serials.next(1, 2), 513)

    def test_frame_builders_share_allocator_and_explicit_does_not_consume(self) -> None:
        serials = fr.SerialAllocator()
        nodes = []
        for _ in range(2):
            node = Node.__new__(Node)
            node.serials, node.id, node.key = serials, 1, fr.DEFAULT_KEY
            node.onboard_seconds = lambda: 100
            nodes.append(node)
        command = fr.build_command(0x1882, 0)
        self.assertEqual(fr.parse_frame(nodes[0].frame(command)).sequence, 1)
        self.assertEqual(fr.parse_frame(nodes[1].frame(command)).sequence, 2)
        explicit = fr.parse_frame(nodes[0].frame(command, source_id=1, sequence=100))
        self.assertEqual((explicit.source_id, explicit.sequence), (1, 100))
        self.assertEqual(fr.parse_frame(nodes[0].frame(command)).sequence, 3)


class QueueTests(unittest.TestCase):
    INVENTORY = (pf.Queue("cpu1-TLMO", 128, 8), pf.Queue("cpu1-MODE", 128, 8),
                 pf.Queue("cpu1-SFI", 256, 8))

    def test_limits_include_overhead_and_both_nodes(self) -> None:
        inventory = self.INVENTORY * 2
        expected = 1024 * (8 + pf.MQ_STRUCT_OVERHEAD)
        self.assertEqual(pf.validate_limits(512, expected, inventory, uid=1000), expected)
        for msg_max, limit, uid in ((511, expected, 1000), (512, expected - 1, 1000), (512, expected, 0)):
            with self.subTest(msg_max=msg_max, limit=limit, uid=uid):
                with self.assertRaises(EnvironmentFail):
                    pf.validate_limits(msg_max, limit, inventory, uid=uid)
        pf.validate_limits(512, resource.RLIM_INFINITY, inventory, uid=1000)

    def test_truncation_is_never_a_depth_pass(self) -> None:
        pf.verify_capacities(self.INVENTORY, "cpu1")
        short = tuple(pf.Queue(q.name, 10, q.message_size) for q in self.INVENTORY)
        with self.assertRaises(Fail) as caught:
            pf.verify_capacities(short, "cpu1")
        self.assertNotIsInstance(caught.exception, EnvironmentFail)
        self.assertIn("observed mq_getattr depth 10", str(caught.exception))
        with self.assertRaises(Fail):
            pf.verify_capacities(self.INVENTORY[:-1], "cpu1")

    def test_allocation_probe_holds_inventory_and_cleans_on_limit_failure(self) -> None:
        class Lib:
            def __init__(self) -> None:
                self.opened = []
                self.closed = []
                self.unlinked = []
                self.attrs = {}

            def mq_open(self, name, _flags, _mode, pointer):
                if len(self.opened) == 2:
                    ctypes.set_errno(errno.EMFILE)
                    return -1
                fd = len(self.opened) + 10
                self.opened.append(name)
                self.attrs[fd] = (pointer._obj.maxmsg, pointer._obj.msgsize)
                return fd

            def mq_getattr(self, fd, pointer):
                pointer._obj.maxmsg, pointer._obj.msgsize = self.attrs[fd]
                return 0

            def mq_close(self, fd):
                self.closed.append(fd)
                return 0

            def mq_unlink(self, name):
                self.unlinked.append(name)
                return 0

        lib = Lib()
        with patch.object(pf, "_libc", return_value=lib):
            with self.assertRaises(EnvironmentFail) as caught:
                pf.allocation_probe(self.INVENTORY)
        self.assertEqual(lib.closed, [10, 11])
        self.assertEqual(lib.opened, lib.unlinked)
        self.assertIn("cpu1-SFI", str(caught.exception))


class TimingTests(unittest.TestCase):
    def test_synthetic_exact_deadlines(self) -> None:
        for duration in (2, 5, 6, 10, 20, 30):
            self.assertFalse(timing.expired(100 + duration - 0.000001, 100, duration))
            self.assertTrue(timing.expired(100 + duration, 100, duration))
            self.assertTrue(timing.expired(100 + duration + 0.000001, 100, duration))
        timing.LIVE_WINDOWS["mode-recovery"].require(20, "recovery")
        timing.LIVE_WINDOWS["mode-recovery"].require(25, "recovery")
        with self.assertRaises(Fail):
            timing.LIVE_WINDOWS["mode-recovery"].require(25.000001, "recovery")

    def test_stalls_and_normal_samples(self) -> None:
        self.assertEqual(timing.scheduling_stalls((0, .05, .10, .15)), ())
        self.assertEqual(timing.scheduling_stalls((0, .05, .55)), ())
        self.assertEqual(timing.scheduling_stalls((0, .05, .60, .65)), (.5499999999999999,))

    def test_live_lane_requires_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch.object(timing, "LOCK_PATH", Path(root) / "lock"), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(EnvironmentFail):
                    with timing.host_lane(live=True):
                        self.fail("unreserved lane ran")

    def test_one_clean_rerun_only_for_scheduling_stalls(self) -> None:
        import check
        class Context:
            def __init__(self, work):
                self.work, self.nodes, self.shutdowns = work, {}, 0

            def shutdown_session(self):
                self.shutdowns += 1

        class Watchdog:
            evidence_list = []

            def __enter__(self):
                self.evidence = self.evidence_list.pop(0)
                return self

            def __exit__(self, *_args):
                return None

        with tempfile.TemporaryDirectory() as root:
            ctx = Context(Path(root))
            calls = []
            spec = check.CheckSpec("n-targets", "N", "target", "expected target", lambda _ctx: calls.append(1),
                                   "isolated", live=True)
            with patch.object(timing, "host_lane", return_value=contextlib.nullcontext()), patch.object(timing, "Watchdog", Watchdog):
                Watchdog.evidence_list = [{"stalls_s": (1,)}, {"stalls_s": ()}]
                result = check.Results()
                check.run_one(ctx, spec, result)
                self.assertTrue(result.items["n-targets"]["passed"])
                self.assertEqual((len(calls), ctx.shutdowns), (2, 1))
                Watchdog.evidence_list = [{"stalls_s": (1,)}, {"stalls_s": (1,)}]
                check.run_one(ctx, spec, result)
                self.assertIn("ENVIRONMENT", result.items["n-targets"]["detail"])
                self.assertNotIn("n-targets", result.ran)


class PacingTests(unittest.TestCase):
    def test_injection_bounds_and_cross_call_spacing(self) -> None:
        node = Node.__new__(Node)
        node.last_burst = float("-inf")
        node.frame = lambda packet, **_kw: packet
        sent = []
        node.send_raw = lambda packet: sent.append(packet)
        clock = [0.0]
        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            clock[0] += seconds
        packet = fr.build_verification(0x18E8, 1024, counter=1)
        with patch("node.time.monotonic", side_effect=lambda: clock[0]), patch("node.time.sleep", side_effect=sleep):
            node.inject([packet] * 9, gap=0)
            self.assertEqual(len(sent), 9)
            self.assertGreaterEqual(clock[0], 1.2)
            node.inject([packet], gap=0)
            self.assertGreaterEqual(node.last_burst, 1.2)
            with self.assertRaises(AssertionError):
                node.inject([packet + b"x"])
            with self.assertRaises(AssertionError):
                node.inject([packet], burst=9)

    def request_fixture(self, length: int = 2068):
        node = Node.__new__(Node)
        node.cpu, node.id = "cpu1", 1
        node.large_request_lock = threading.Lock()
        node.last_large_reply = float("-inf")
        node.large_packet_lengths, node.tlmo_budget = {}, 0
        node.large_mids = lambda: {0x08D6: 2068}
        node.settle = lambda: None
        observation = []
        packet = SimpleNamespace(data=bytes(length))
        def wait(_mark, _mid, timeout, count):
            observation.append(("wait", node.tlmo_budget, timeout, count))
            return [packet]
        node.ground = SimpleNamespace(mark=lambda: 0, wait_mid=wait, since=lambda *_args: [packet])
        def cmd(_app, _name, payload, **_kwargs):
            node.tlmo_budget = struct.unpack_from("<H", payload)[0]
            observation.append(("budget", node.tlmo_budget))
        node.cmd = cmd
        return node, observation

    def test_held_snapshot_restores_budget_before_awaiting(self) -> None:
        node, evidence = self.request_fixture()
        with patch("node.time.sleep"):
            node.request(lambda: evidence.append(("send", node.tlmo_budget)), 0x08D6, timeout=9, held_snapshot=True)
        self.assertEqual(evidence[:3], [("send", 0), ("budget", 256), ("wait", 256, 2.0, 1)])
        self.assertEqual(node.large_packet_lengths, {0x08D6: 2068})

    def test_large_request_restores_prior_budget_and_rejects_wrong_size(self) -> None:
        node, evidence = self.request_fixture(2067)
        node.tlmo_budget = 8
        with patch("node.time.sleep"):
            with self.assertRaisesRegex(Fail, "expected 2068 bytes"):
                node.request(lambda: evidence.append(("send", node.tlmo_budget)), 0x08D6)
        self.assertEqual(evidence[0], ("budget", 256))
        self.assertEqual(evidence[-1], ("budget", 8))

    def test_batch_resumes_only_owned_node_on_send_failure(self) -> None:
        import signal
        node = Node.__new__(Node)
        node.pids = lambda: [8123]
        node.send_raw = lambda _frame: (_ for _ in ()).throw(Fail("injected send failure"))
        with patch("node.os.kill") as signals, patch("node.Path.read_text", return_value="State:\tT (stopped)\n"):
            with self.assertRaisesRegex(Fail, "injected send"):
                node.send_batch((b"framed",))
        self.assertEqual(tuple(call.args for call in signals.call_args_list),
                         ((8123, signal.SIGSTOP), (8123, signal.SIGCONT)))


if __name__ == "__main__":
    unittest.main()

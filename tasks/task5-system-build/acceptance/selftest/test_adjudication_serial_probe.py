"""Exercise shared serial probes with native lifecycle delays and MODE isolation."""
from __future__ import annotations

import binascii
import hashlib
import hmac
import io
import logging
import struct
import sys
import types
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK))
import ctf_adversarial as adversarial


class SerialProbeFixture(unittest.TestCase):
    def run_probe(self, *, mode_ack: bool = True, mutation: str = '') -> tuple[bool, list[dict], list[bytes], str]:
        sent: list[bytes] = []
        calls: list[dict] = []
        serials: dict[int, int] = {}
        saved: dict[str, bytes] = {}
        mode_enabled = True
        mode_observed = False
        hk_requested = False
        hk_visible = False
        cleared = False
        clock = {'now': 0.0}
        acknowledgement_at = 0.4

        class FakeSocket:
            def __init__(self, *_args: object) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def sendto(self, data: bytes, *_args: object) -> int:
                sent.append(bytes(data))
                return len(data)

            def send(self, data: bytes, *_args: object) -> int:
                sent.append(bytes(data))
                return len(data)

        socket_module = types.ModuleType('socket')
        socket_module.AF_INET = 2
        socket_module.SOCK_DGRAM = 2
        socket_module.socket = FakeSocket
        time_module = types.ModuleType('time')
        time_module.monotonic = lambda: clock['now']
        time_module.sleep = lambda seconds: clock.update(now=clock['now'] + seconds)

        def build(data: dict, name: str) -> bytes:
            source = data.get('source_id', 1)
            previous = serials.get(source, 0)
            serial = data.get('sequence', previous + 1)
            if mutation == 'per-plugin' and name == 'SendLinkCmdiCommand':
                serial = 1
            serials[source] = max(previous, serial)
            command = bytes.fromhex('1801c00000010027')
            body = struct.pack('>HBBHHII', 0x4356, 1, 0, source, len(command), 1000, serial) + command
            packet = body + hmac.new(b'cFS-cmdval-test-key-0123456789ab', body, hashlib.sha256).digest()
            frame = packet + struct.pack('>H', binascii.crc_hqx(packet, 0xFFFF))
            if mutation == 'bad-crc' and name == 'SendLinkCmdiCommand':
                frame = frame[:-1] + bytes((frame[-1] ^ 1,))
            return frame

        def dispatch(item: dict) -> bool:
            nonlocal mode_enabled, mode_observed, hk_requested, hk_visible, cleared
            calls.append(dict(item, at=clock['now']))
            name, data = item['instruction'], item['data']
            if name == 'ClearTlmPacket':
                mode_observed = False
                hk_visible = False
                cleared = True
                return True
            if name == 'CheckTlmValue':
                mode_observed = hk_visible
                return mode_observed
            if name in ('OpenUdpLink', 'CloseLink'):
                return True
            if name == 'ReplayCmdiFrame':
                encoded = saved[data['variable']]
                if mutation == 'replay-changes':
                    encoded = encoded[:-1] + bytes((encoded[-1] ^ 1,))
            else:
                encoded = build(data, name)
                if data.get('save_frame'):
                    saved[data['save_frame']] = encoded
            with FakeSocket() as sender:
                sender.sendto(encoded, ('127.0.0.1', 1234))
            if name == 'SendCmdiCommand' and data.get('mid') == 'MODE_CMD_MID':
                if data.get('source_id') != 2 or data['args']['Payload']['Enabled'] != 0:
                    return False
                mode_enabled = False
            if name == 'SendCmdiCommand' and data.get('mid') == 'MODE_SEND_HK_MID':
                if data.get('source_id') != 2 or not cleared:
                    return False
                hk_requested = True
            if name == 'StopApp':
                return not mode_enabled and mode_observed and data.get('timeout', 10) >= 6.2
            if name == 'StartApp':
                return data.get('timeout', 10) >= 10
            return True

        def pump_telemetry() -> None:
            nonlocal hk_visible
            calls.append({'instruction': 'PumpTelemetry', 'at': clock['now']})
            if not mode_enabled and mode_ack and cleared and hk_requested and clock['now'] >= acknowledgement_at:
                hk_visible = True

        class FakePlugin:
            def __init__(self) -> None:
                pass

        plugin_manager = types.ModuleType('lib.plugin_manager')
        plugin_manager.Plugin = FakePlugin
        plugin_manager.ArgTypes = types.SimpleNamespace(other='other')
        global_module = types.ModuleType('lib.ctf_global')
        global_module.Global = types.SimpleNamespace(
            plugin_manager=types.SimpleNamespace(find_plugin_for_command_and_execute=dispatch),
            time_manager=types.SimpleNamespace(pre_command=pump_telemetry))
        lib = types.ModuleType('lib')
        lib.__path__ = []
        logger_module = types.ModuleType('lib.logger')
        logger_module.logger = logging.getLogger('serial-probe-selftest')
        modules = {'socket': socket_module, 'time': time_module, 'lib': lib, 'lib.plugin_manager': plugin_manager,
                   'lib.ctf_global': global_module, 'lib.logger': logger_module}
        vector = next(item for item in adversarial.adversarial_vectors() if item['id'] == 'cross-plugin-shared-cmdi-serials')
        namespace: dict = {}
        source = adversarial.ADAPTER_SOURCE.replace('__ACCEPTANCE_PACK__', repr(str(PACK)))
        output = io.StringIO()
        with patch.dict(sys.modules, modules), redirect_stdout(output):
            exec(compile(source, 'acceptance_serial_probe.py', 'exec'), namespace)
            plugin = namespace['AcceptanceDispatchPlugin']()
            for item in vector['setup']:
                if not plugin.dispatch('setup', item['instruction'], item['data'], True):
                    return False, calls, sent, output.getvalue()
            result = plugin.serial_probe()
        return result, calls, sent, output.getvalue()

    def test_native_stop_delay_and_source_two_setup_preserve_exact_probe(self) -> None:
        result, calls, sent, _output = self.run_probe()
        self.assertTrue(result)
        self.assertEqual([struct.unpack_from('>H', frame, 4)[0] for frame in sent], [2, 2, 1, 1, 1, 1, 1, 1])
        self.assertEqual([struct.unpack_from('>I', frame, 12)[0] for frame in sent[2:]], [100, 101, 102, 103, 100, 104])
        self.assertEqual(sent[2], sent[6])
        checks = [item for item in calls if item['instruction'] == 'CheckTlmValue']
        pumps = [item for item in calls if item['instruction'] == 'PumpTelemetry']
        self.assertGreaterEqual(len(checks), 2)
        self.assertEqual([item['at'] for item in pumps], [item['at'] for item in checks])
        self.assertLess(checks[0]['at'], 0.4)
        self.assertGreaterEqual(next(item['at'] for item in calls if item['instruction'] == 'StopApp'), 0.4)
        self.assertGreaterEqual(next(item['data']['timeout'] for item in calls if item['instruction'] == 'StopApp'), 12)

    def test_missing_mode_acknowledgement_stops_before_lifecycle_probe(self) -> None:
        result, calls, _sent, output = self.run_probe(mode_ack=False)
        self.assertFalse(result)
        self.assertFalse(any(item['instruction'] == 'StopApp' for item in calls))
        checks = [item for item in calls if item['instruction'] == 'CheckTlmValue']
        self.assertGreaterEqual(len(checks), 2)
        self.assertLessEqual(checks[-1]['at'], 5.0)
        self.assertIn('fresh MODE HK', output)

    def test_serial_and_frame_defects_still_fail(self) -> None:
        for mutation in ('per-plugin', 'bad-crc', 'replay-changes'):
            with self.subTest(mutation=mutation):
                result, _calls, _sent, _output = self.run_probe(mutation=mutation)
                self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()

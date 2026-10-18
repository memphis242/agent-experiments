"""Self-test of the independent frame codec (S3.1) and the stream deframer (S5.2)."""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frames as fr  # noqa: E402

# RFC 4231 test cases 1-7: (key, data, HMAC-SHA-256, truncate to bytes or None)
RFC4231 = [
    (b"\x0b" * 20, b"Hi There", "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7", None),
    (b"Jefe", b"what do ya want for nothing?", "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", None),
    (b"\xaa" * 20, b"\xdd" * 50, "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe", None),
    (bytes(range(1, 26)), b"\xcd" * 50, "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b", None),
    (b"\x0c" * 20, b"Test With Truncation", "a3b6167473100ee06e0c796c2955552b", 16),
    (b"\xaa" * 131, b"Test Using Larger Than Block-Size Key - Hash Key First",
     "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54", None),
    (b"\xaa" * 131, b"This is a test using a larger than block-size key and a larger than block-size data. The key needs "
                    b"to be hashed before being used by the HMAC algorithm.",
     "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2", None),
]


class Checksums(unittest.TestCase):
    def test_crc_check_value(self) -> None:
        self.assertEqual(fr.crc16_ccitt_false(b"123456789"), 0x29B1)
        self.assertEqual(fr.crc16_ccitt_false(b""), 0xFFFF)

    def test_crc_matches_binascii(self) -> None:
        import binascii
        rnd = random.Random(5)
        for n in (1, 2, 17, 58, 1074):
            data = bytes(rnd.randrange(256) for _ in range(n))
            self.assertEqual(fr.crc16_ccitt_false(data), binascii.crc_hqx(data, 0xFFFF))

    def test_hmac_rfc4231(self) -> None:
        for i, (key, data, want, trunc) in enumerate(RFC4231, 1):
            got = fr.hmac_sha256(key, data)
            self.assertEqual(got[:trunc].hex() if trunc else got.hex(), want, f"RFC 4231 case {i}")

    def test_hmac_matches_stdlib(self) -> None:
        rnd = random.Random(7)
        for klen in (0, 1, 32, 63, 64, 65, 200):
            key = os.urandom(klen)
            data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(300)))
            self.assertEqual(fr.hmac_sha256(key, data), hmac.new(key, data, hashlib.sha256).digest())


class Frames(unittest.TestCase):
    def test_layout(self) -> None:
        cmd = fr.build_command(0x1882, 0)
        self.assertEqual(len(cmd), 8)
        x = 0
        for b in cmd:
            x ^= b
        self.assertEqual(x, 0xFF, "command checksum makes the XOR of all bytes 0xFF")
        f = fr.build_frame(cmd, time_seconds=0x01020304, source_id=2, sequence=0x0A0B0C0D)
        self.assertEqual(len(f), 50 + 8)
        self.assertEqual(f[:16].hex(), "4356" "01" "00" "0002" "0008" "01020304" "0a0b0c0d")
        self.assertEqual(f[16:24], cmd)
        self.assertEqual(f[24:56], fr.hmac_sha256(fr.DEFAULT_KEY, f[:24]))
        self.assertEqual(struct.unpack(">H", f[56:58])[0], fr.crc16_ccitt_false(f[:56]))
        parsed = fr.parse_frame(f)
        self.assertEqual((parsed.source_id, parsed.time_seconds, parsed.sequence, parsed.command), (2, 0x01020304, 0x0A0B0C0D, cmd))

    def test_rejections_in_order(self) -> None:
        cmd = fr.build_command(0x1882, 0)
        good = fr.build_frame(cmd, time_seconds=5)
        cases = {
            "FORMAT": [fr.build_frame(cmd, time_seconds=5, magic=0x4357), fr.build_frame(cmd, time_seconds=5, version=2),
                       fr.build_frame(cmd, time_seconds=5, flags=1), fr.build_frame(cmd, time_seconds=5, length_field=9),
                       good + b"\0", good[:57], fr.build_frame(fr.build_command(0x0882, 0), time_seconds=5),
                       fr.build_frame(cmd[:4] + b"\x00\x05" + cmd[6:], time_seconds=5)],
            "CRC": [fr.corrupt(good, "crc"), fr.corrupt(good, "crc-and-mac")],
            "HMAC": [fr.corrupt(good, "mac"), fr.build_frame(cmd, time_seconds=5, key=b"x" * 32)],
        }
        for reason, frames in cases.items():
            for i, f in enumerate(frames):
                with self.assertRaises(fr.FrameError, msg=f"{reason} {i}") as cm:
                    fr.parse_frame(f)
                self.assertEqual(cm.exception.reason, reason, f"{reason} case {i}")

    def test_max_command(self) -> None:
        big = fr.build_verification(0x18E8, 1024, counter=3)
        f = fr.build_frame(big, time_seconds=1)
        self.assertEqual(len(f), fr.MAX_FRAME)
        self.assertEqual(fr.parse_frame(f).command, big)
        self.assertEqual(struct.unpack_from("<I", big, 16)[0], 3)
        self.assertTrue(fr.is_command(big))


class Deframer(unittest.TestCase):
    def frames(self, n: int) -> list[bytes]:
        return [fr.build_frame(fr.build_verification(0x18E6, 20 + 7 * i, counter=i), time_seconds=i) for i in range(n)]

    def test_whole_and_concatenated(self) -> None:
        fs = self.frames(3)
        d = fr.Deframer()
        units = d.feed(b"".join(fr.stream_wrap(f) for f in fs))
        self.assertEqual([u.data for u in units], fs)

    def test_byte_by_byte_and_garbage(self) -> None:
        fs = self.frames(2)
        stream = b"\x00\x1a\xcf" + fr.stream_wrap(fs[0]) + b"junk\x1a\xcf\xfc" + fr.stream_wrap(fs[1])
        d = fr.Deframer()
        got = []
        for b in stream:
            got += d.feed(bytes([b]))
        self.assertEqual([u.data for u in got], fs)

    def test_out_of_range_length_resumes_after_marker(self) -> None:
        good = self.frames(1)[0]
        bad_hdr = fr.MARKER + struct.pack(">HBBHHII", fr.MAGIC, 1, 0, 1, 2000, 0, 0)
        tiny_hdr = fr.MARKER + struct.pack(">HBBHHII", fr.MAGIC, 1, 0, 1, 7, 0, 0)
        d = fr.Deframer()
        units = d.feed(bad_hdr + tiny_hdr + fr.stream_wrap(good))
        self.assertEqual([u.bad_length for u in units], [2000, 7, None])
        self.assertEqual(units[-1].data, good)

    def test_partial_kept(self) -> None:
        good = self.frames(1)[0]
        d = fr.Deframer()
        w = fr.stream_wrap(good)
        self.assertEqual(d.feed(w[:30]), [])
        self.assertEqual([u.data for u in d.feed(w[30:])], [good])


if __name__ == "__main__":
    unittest.main()

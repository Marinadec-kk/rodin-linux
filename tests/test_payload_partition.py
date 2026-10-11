import bz2
import hashlib
import importlib.util
import io
import lzma
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stream = load("extract_payload_partition")


def v(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    return bytes(out) + bytes([n])


def field(n, value):
    if isinstance(value, int):
        return v(n << 3) + v(value)
    return v((n << 3) | 2) + v(len(value)) + value


COMPRESS = {0: lambda x: x, 1: bz2.compress, 8: lzma.compress}


def build(chunks, name=b"vendor", block_size=4, size=None):
    """chunks: list of (kind, start_block, data). Returns (message, blob_area, image)."""
    blobs, ops = b"", []
    image = bytearray(size if size is not None else
                      max((s * block_size + len(d)) for _, s, d in chunks))
    for kind, start, data in chunks:
        extent = field(1, start) + field(2, len(data) // block_size)
        if kind == 6:
            ops.append(field(1, 6) + field(6, extent))
            continue
        compressed = COMPRESS[kind](data)
        ops.append(field(1, kind) + field(2, len(blobs)) + field(3, len(compressed)) +
                   field(6, extent) + field(8, hashlib.sha256(compressed).digest()))
        blobs += compressed
        image[start * block_size:start * block_size + len(data)] = data
    info = field(1, len(image)) + field(2, hashlib.sha256(image).digest())
    message = field(1, name) + field(7, info) + b"".join(field(8, op) for op in ops)
    return message, blobs, bytes(image)


def run(message, blobs, block_size=4):
    out = io.BytesIO()
    info = stream.extract_partition_to(stream.base.fields(message), block_size,
                                       lambda offset, size: blobs[offset:offset + size], out)
    return out.getvalue(), info


class StreamingPayloadTests(unittest.TestCase):
    def test_out_of_order_mixed_operations(self):
        message, blobs, image = build([(8, 3, b"wxyz" * 2), (6, 2, b"\0" * 4),
                                       (0, 0, b"abcd"), (1, 1, b"efgh")])
        out, info = run(message, blobs)
        self.assertEqual(out, image)
        self.assertEqual(info["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual(info["operation_counts"], {"8": 1, "6": 1, "0": 1, "1": 1})

    def test_rejects_unsupported_kinds_including_zstd(self):
        for kind in (2, 3, 4, 5, 7, 9, 10, 14):
            message = (field(1, b"vendor") +
                       field(7, field(1, 4) + field(2, b"h" * 32)) +
                       field(8, field(1, kind) + field(6, field(1, 0) + field(2, 1))))
            with self.assertRaisesRegex(ValueError, "Unsupported|source"):
                run(message, b"")

    def test_rejects_source_references(self):
        message, blobs, _ = build([(0, 0, b"abcd")])
        parsed = stream.base.fields(message)
        parsed[8] = [parsed[8][0] + field(4, field(1, 0) + field(2, 1))]
        with self.assertRaises(ValueError):
            stream.extract_partition_to(parsed, 4, lambda o, s: blobs, io.BytesIO())

    def test_rejects_bad_operation_and_partition_hash(self):
        message, blobs, _ = build([(0, 0, b"abcd"), (0, 1, b"efgh")])
        with self.assertRaisesRegex(ValueError, "Operation blob"):
            run(message, b"XXXX" + blobs[4:])
        parsed = stream.base.fields(message)
        parsed[7] = [field(1, 8) + field(2, b"x" * 32)]
        with self.assertRaisesRegex(ValueError, "partition SHA-256"):
            stream.extract_partition_to(parsed, 4, lambda o, s: blobs[o:o + s], io.BytesIO())

    def test_rejects_overlap_gap_and_bounds(self):
        message, blobs, _ = build([(0, 0, b"abcd")])
        parsed = stream.base.fields(message)
        parsed[8] *= 2
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            stream.extract_partition_to(parsed, 4, lambda o, s: blobs, io.BytesIO())
        message, blobs, _ = build([(0, 0, b"abcd")], size=8)
        with self.assertRaisesRegex(ValueError, "unwritten"):
            run(message, blobs)
        message, blobs, _ = build([(0, 1, b"abcd")])
        parsed = stream.base.fields(message)
        parsed[7] = [field(1, 4) + field(2, b"x" * 32)]
        with self.assertRaisesRegex(ValueError, "outside"):
            stream.extract_partition_to(parsed, 4, lambda o, s: blobs, io.BytesIO())

    def test_rejects_truncated_blob_and_zero_with_data(self):
        message, blobs, _ = build([(0, 0, b"abcd")])
        with self.assertRaisesRegex(ValueError, "Truncated"):
            run(message, blobs[:2])
        message = (field(1, b"vendor") + field(7, field(1, 4) + field(2, b"h" * 32)) +
                   field(8, field(1, 6) + field(3, 4) + field(8, b"d" * 32) +
                         field(6, field(1, 0) + field(2, 1))))
        with self.assertRaisesRegex(ValueError, "ZERO"):
            run(message, b"abcd")

    def test_rejects_disallowed_name_and_old_image(self):
        for name in (b"boot", b"system", b"../vendor"):
            message, blobs, _ = build([(0, 0, b"abcd")], name=name)
            with self.assertRaises(ValueError):
                run(message, blobs)
        message, blobs, _ = build([(0, 0, b"abcd")])
        parsed = stream.base.fields(message)
        parsed[6] = [b"old"]
        with self.assertRaises(ValueError):
            stream.extract_partition_to(parsed, 4, lambda o, s: blobs, io.BytesIO())

    def test_operation_size_limit(self):
        message, blobs, _ = build([(0, 0, b"abcdefgh")])
        with patch.object(stream, "MAX_OPERATION", 4):
            with self.assertRaisesRegex(ValueError, "too large"):
                run(message, blobs)

    def test_zip_roundtrip_rom_pin_and_protected_output(self):
        data = bytes(range(256)) * 32
        message, blobs, image = build([(8, 0, data[:4096]), (1, 1, data[4096:])],
                                      block_size=4096)
        manifest = field(3, 4096) + field(12, 0) + field(13, message)
        raw = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0) + manifest + blobs
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
                z.writestr("payload.bin", raw)
            out_dir = Path(directory) / "out"
            with self.assertRaisesRegex(ValueError, "ROM SHA-256"):
                stream.extract(path, "vendor", out_dir)
            self.assertFalse((out_dir / "vendor.img.part").exists())
            pin = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(stream.base, "ROM_SHA256", pin):
                info = stream.extract(path, "vendor", out_dir)
                self.assertEqual((out_dir / "vendor.img").read_bytes(), image)
                self.assertEqual(info["sha256"], hashlib.sha256(image).hexdigest())
                self.assertIn('"vendor"', (out_dir / "extraction.json").read_text())
                with self.assertRaisesRegex(ValueError, "exists"):
                    stream.extract(path, "vendor", out_dir)
                with self.assertRaisesRegex(ValueError, "absent"):
                    stream.extract(path, "odm", out_dir)
                with self.assertRaisesRegex(ValueError, "build/ota"):
                    stream.extract(path, "vendor", stream.PROTECTED_OUTPUT)


if __name__ == "__main__":
    unittest.main()

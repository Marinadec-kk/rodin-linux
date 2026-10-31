import bz2
import hashlib
import importlib.util
import lzma
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


payload = load("extract_boot_payload")
boot = load("inspect_boot")


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


def partition(data, kind=0, block_size=4):
    compressed = {0: lambda x: x, 1: bz2.compress, 8: lzma.compress}[kind](data)
    extent = field(1, 0) + field(2, len(data) // block_size)
    op = (field(1, kind) + field(2, 0) + field(3, len(compressed)) +
          field(6, extent) + field(8, hashlib.sha256(compressed).digest()))
    info = field(1, len(data)) + field(2, hashlib.sha256(data).digest())
    message = field(1, b"boot") + field(7, info) + field(8, op)
    return message, compressed


class PayloadTests(unittest.TestCase):
    def test_full_replace_types(self):
        data = b"12345678"
        for kind in (0, 1, 8):
            message, blob = partition(data, kind)
            out, info = payload.extract_partition(payload.fields(message), 4,
                                                  lambda start, size: blob[start:start + size])
            self.assertEqual(out, data)
            self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())

    def test_zero_operation(self):
        data = b"\0" * 8
        op = field(1, 6) + field(6, field(1, 0) + field(2, 2))
        info = field(1, 8) + field(2, hashlib.sha256(data).digest())
        message = field(1, b"boot") + field(7, info) + field(8, op)
        out, _ = payload.extract_partition(payload.fields(message), 4, lambda offset, size: b"")
        self.assertEqual(out, data)

    def test_rejects_unknown_delta_and_discard(self):
        for kind in (3, 4, 5, 7, 9, 10):
            with self.assertRaises(ValueError):
                payload.decode_operation(kind, b"", 8)

    def test_rejects_wrong_decompressed_size_and_trailing_data(self):
        for kind, blob in ((0, b"abc"), (1, bz2.compress(b"abc")),
                           (8, lzma.compress(b"abc") + b"trailing")):
            with self.assertRaises(ValueError):
                payload.decode_operation(kind, blob, 8)

    def test_rejects_bad_operation_hash(self):
        message, _ = partition(b"12345678")
        with self.assertRaises(ValueError):
            payload.extract_partition(payload.fields(message), 4, lambda offset, size: b"87654321")

    def test_rejects_wrong_partition_hash(self):
        message, blob = partition(b"12345678")
        parsed = payload.fields(message)
        parsed[7] = [field(1, 8) + field(2, b"x" * 32)]
        with self.assertRaises(ValueError):
            payload.extract_partition(parsed, 4, lambda offset, size: blob)

    def test_rejects_overlapping_or_missing_extents(self):
        message, blob = partition(b"12345678")
        parsed = payload.fields(message)
        parsed[8] *= 2
        with self.assertRaises(ValueError):
            payload.extract_partition(parsed, 4, lambda offset, size: blob)
        parsed = payload.fields(message)
        parsed[7] = [field(1, 12) + field(2, b"x" * 32)]
        with self.assertRaises(ValueError):
            payload.extract_partition(parsed, 4, lambda offset, size: blob)

    def test_rejects_old_image_or_unsafe_name(self):
        message, blob = partition(b"12345678")
        for change in ({6: [b"old"]}, {1: [b"../boot"]}):
            parsed = payload.fields(message)
            parsed.update(change)
            with self.assertRaises(ValueError):
                payload.extract_partition(parsed, 4, lambda offset, size: blob)

    def test_protobuf_validation(self):
        for invalid in (b"\x80", b"\0", b"\x0a\x02x", b"\xff" * 11, b"\x0b"):
            with self.assertRaises(ValueError):
                payload.fields(invalid)
        with self.assertRaises(ValueError):
            payload.one({1: [1, 2]}, 1)
        with self.assertRaises(ValueError):
            payload.one({1: [b"a"]}, 1, kind=int)
        # Unknown fixed-width fields can be safely skipped.
        self.assertEqual(payload.fields(b"\x09" + b"x" * 8)[1], [b"x" * 8])

    def test_zip_payload_roundtrip_and_rom_pin(self):
        data = b"a" * 4096
        message, compressed = partition(data, 8, 4096)
        manifest = field(3, 4096) + field(12, 0) + field(13, message)
        raw = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0) + manifest + compressed
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
                z.writestr("payload.bin", raw)
            with self.assertRaises(ValueError):
                list(payload.read_payload(path, ["boot"]))
            with patch.object(payload, "ROM_SHA256", hashlib.sha256(path.read_bytes()).hexdigest()):
                result = list(payload.read_payload(path, ["boot"]))
                self.assertEqual(result[0][0], data)
                with self.assertRaises(ValueError):
                    list(payload.read_payload(path, ["init_boot"]))


class BootTests(unittest.TestCase):
    def image(self, version=4):
        data = bytearray(12288)
        data[:8] = b"ANDROID!"
        struct.pack_into("<4I", data, 8, 3, 2, 0, 1584 if version == 4 else 1580)
        struct.pack_into("<I", data, 40, version)
        data[4096:4099] = b"ker"
        data[8192:8194] = b"rd"
        return data

    def test_v3_v4_offsets(self):
        for version in (3, 4):
            result = boot.inspect(self.image(version))
            self.assertEqual(result["kernel"]["offset"], 4096)
            self.assertEqual(result["ramdisk"]["offset"], 8192)
            self.assertEqual(result["header_version"], version)
            self.assertNotIn("cmdline", result)

    def test_init_boot_without_kernel(self):
        data = self.image()
        struct.pack_into("<I", data, 8, 0)
        result = boot.inspect(data)
        self.assertEqual(result["kernel"]["size"], 0)
        self.assertEqual(result["ramdisk"]["offset"], 4096)

    def test_rejects_bad_header_and_truncation(self):
        for invalid in (b"bad", self.image()[:8200]):
            if len(invalid) > 8000:
                struct.pack_into("<I", invalid, 12, 100)
            with self.assertRaises(ValueError):
                boot.inspect(invalid)
        data = self.image()
        struct.pack_into("<I", data, 40, 2)
        with self.assertRaises(ValueError):
            boot.inspect(data)

    def test_avb_footer_bounds(self):
        data = self.image()
        data.extend(b"\0" * 4096)
        offset = 12288
        data[offset:offset + 4] = b"AVB0"
        data[-64:-60] = b"AVBf"
        struct.pack_into(">IIQQQ", data, len(data) - 60, 1, 0, 12288, offset, 256)
        result = boot.inspect(data)
        self.assertEqual(result["avb_footer"]["vbmeta_offset"], offset)
        self.assertEqual(result["avb_footer"]["signature_verification"], "not performed")
        struct.pack_into(">Q", data, len(data) - 36, 100000)
        with self.assertRaises(ValueError):
            boot.inspect(data)

    def test_signature_bounds(self):
        data = self.image()
        struct.pack_into("<I", data, 1580, 1)
        with self.assertRaises(ValueError):
            boot.inspect(data)


if __name__ == "__main__":
    unittest.main()

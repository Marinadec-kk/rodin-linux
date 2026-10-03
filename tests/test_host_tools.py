import gzip
import importlib.util
from pathlib import Path
import stat
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("build_initramfs")
inspector = load("inspect_vendor_boot")


def parse_newc(data):
    result = {}
    offset = 0
    while True:
        if data[offset:offset + 6] != b"070701":
            raise AssertionError("Invalid newc magic")
        fields = [int(data[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        mode, length, name_length = fields[1], fields[6], fields[11]
        start = offset + 110
        name = data[start:start + name_length - 1].decode()
        if data[start + name_length - 1] != 0:
            raise AssertionError("Unterminated cpio name")
        start = (start + name_length + 3) // 4 * 4
        payload = data[start:start + length]
        offset = (start + length + 3) // 4 * 4
        if name == "TRAILER!!!":
            return result
        if name in result:
            raise AssertionError("Duplicate path")
        result[name] = (mode, payload, fields[9], fields[10])


def elf(kind=1, dynamic_tag=0):
    data = bytearray(136)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", data, 18, 183)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<IIQQQQQQ", data, 64, kind, 0, 120, 0, 0, 16, 16, 8)
    struct.pack_into("<qQ", data, 120, dynamic_tag, 0)
    return data


def vendor_image(version=4):
    data = bytearray(16384)
    data[:8] = b"VNDRBOOT"
    struct.pack_into("<5I", data, 8, version, 4096, 0, 0, 3)
    struct.pack_into("<2I", data, 2096, 2128 if version == 4 else 2112, 3)
    data[4096:4099] = b"abc"
    data[8192:8195] = b"dtb"
    if version == 4:
        struct.pack_into("<4I", data, 2112, 108, 1, 108, 0)
        struct.pack_into("<3I", data, 12288, 3, 0, 2)
        data[12300:12308] = b"recovery"
    return data


class InitramfsTests(unittest.TestCase):
    def test_deterministic_archive_and_compression(self):
        first = builder.build(b"fixture", b"#!/bin/sh\n")
        self.assertEqual(first, builder.build(b"fixture", b"#!/bin/sh\n"))
        self.assertEqual(gzip.decompress(first[1]), first[0])
        self.assertEqual(len(first[0]) % 512, 0)
        entries = parse_newc(first[0])
        self.assertEqual(entries["init"][0], stat.S_IFREG | 0o755)
        self.assertEqual(entries["bin/sh"][1], b"busybox")
        self.assertEqual(entries["dev/console"], (stat.S_IFCHR | 0o600, b"", 5, 1))
        self.assertEqual(entries["dev/null"][2:], (1, 3))
        self.assertEqual(entries["dev/kmsg"][2:], (1, 11))
        self.assertEqual(set(entries), {"bin", "sbin", "dev", "dev/pts", "proc", "sys", "run", "root", "bin/busybox", "bin/sh", "init", "dev/console", "dev/null", "dev/tty", "dev/kmsg"})

    def test_rejects_unsafe_paths(self):
        for name in ("/init", "../init", "a/../b", "bad\x00name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                builder.newc([(name, stat.S_IFREG, b"", 0, 0)])

    def test_static_aarch64_and_static_pie(self):
        builder.validate_busybox(elf())
        builder.validate_busybox(elf(kind=2))

    def test_rejects_dynamic_dependencies(self):
        for data in (elf(kind=3), elf(kind=2, dynamic_tag=1)):
            with self.assertRaises(ValueError):
                builder.validate_busybox(data)

    def test_rejects_bad_elf(self):
        data = elf()
        struct.pack_into("<H", data, 18, 62)
        for invalid in (b"not ELF", data, elf()[:80]):
            with self.assertRaises(ValueError):
                builder.validate_busybox(invalid)

    def test_rejects_unpinned_apk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.apk"
            path.write_bytes(b"not trusted")
            with self.assertRaises(ValueError):
                builder.read_busybox(path)


class VendorBootTests(unittest.TestCase):
    def test_v4_layout(self):
        result = inspector.inspect(vendor_image())
        self.assertEqual(result["ramdisk_offset"], 4096)
        self.assertEqual(result["dtb_offset"], 8192)
        self.assertEqual(result["table_offset"], 12288)
        self.assertEqual(result["bootconfig_offset"], 16384)
        self.assertEqual(result["entries"][0]["name"], "recovery")
        self.assertEqual(result["entries"][0]["type"], 2)
        self.assertNotIn("cmdline", result)

    def test_v3(self):
        result = inspector.inspect(vendor_image(3))
        self.assertEqual(result["header_version"], 3)
        self.assertEqual(result["entries"], [])

    def test_rejects_bad_magic_and_truncation(self):
        for data in (b"bad", vendor_image()[:8000], vendor_image()[:12350]):
            with self.assertRaises(ValueError):
                inspector.inspect(data)

    def test_rejects_bad_page_size(self):
        data = vendor_image()
        struct.pack_into("<I", data, 12, 3000)
        with self.assertRaises(ValueError):
            inspector.inspect(data)

    def test_rejects_entry_outside_ramdisk(self):
        data = vendor_image()
        struct.pack_into("<3I", data, 12288, 4, 0, 2)
        with self.assertRaises(ValueError):
            inspector.inspect(data)

    def test_rejects_bad_table_dimensions(self):
        data = vendor_image()
        struct.pack_into("<I", data, 2112, 107)
        with self.assertRaises(ValueError):
            inspector.inspect(data)


if __name__ == "__main__":
    unittest.main()

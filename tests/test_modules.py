from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import inspect_modules as modules
import build_initramfs as builder


def elf(info=b"name=test\0vermagic=6.6 test\0depends=\0"):
    names = b"\0.shstrtab\0.modinfo\0"
    data = bytearray(64 + 3 * 64)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", data, 16, 1, 183)
    struct.pack_into("<Q", data, 40, 64)
    struct.pack_into("<HHH", data, 58, 64, 3, 1)
    for index, name, kind, offset, length in (
            (1, 1, 3, len(data), len(names)),
            (2, 11, 1, len(data) + len(names), len(info))):
        struct.pack_into("<IIQQQQIIQQ", data, 64 + index * 64,
                         name, kind, 0, 0, offset, length, 0, 0, 1, 0)
    return bytes(data) + names + info


class ModuleTests(unittest.TestCase):
    def test_metadata_and_repeated_values(self):
        info = modules.modinfo(elf(b"name=wlan\0vermagic=6.6 SMP\0depends=core,phy\0"
                                   b"alias=one\0alias=two\0firmware=wifi/fw.bin\0"))
        self.assertEqual(info["alias"], ["one", "two"])
        self.assertEqual(info["firmware"], ["wifi/fw.bin"])
        self.assertEqual(info["depends"], ["core,phy"])

    def test_invalid_elf_and_bounds(self):
        original = elf()
        invalid = [b"", original[:63], original[:200], original[:-1]]
        for offset, value in ((4, 1), (5, 2), (16, 2), (18, 62), (58, 32), (62, 9)):
            data = bytearray(original)
            data[offset] = value
            invalid.append(bytes(data))
        data = bytearray(original)
        struct.pack_into("<Q", data, 40, 2**63)
        invalid.append(bytes(data))
        data = bytearray(original)
        struct.pack_into("<I", data, 192, 9999)
        invalid.append(bytes(data))
        for data in invalid:
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                modules.modinfo(data)

    def test_bad_modinfo(self):
        for info in (b"name=x\0", b"name=x\0vermagic=v\0name=y\0",
                     b"name=x\0vermagic=v\0broken\0", b"name=x\0vermagic=v",
                     b"name=\0vermagic=v\0"):
            with self.assertRaises(ValueError):
                modules.modinfo(elf(info))

    def test_report_dependencies_and_firmware(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            tree = root / "modules"
            tree.mkdir()
            (tree / "wifi.ko").write_bytes(elf(
                b"name=wlan\0vermagic=v\0depends=core-driver,missing\0"
                b"firmware=wifi/fw.bin\0firmware=missing.bin\0"))
            (tree / "core.ko").write_bytes(elf(b"name=core_driver\0vermagic=v\0"))
            firmware = root / "firmware"
            (firmware / "wifi").mkdir(parents=True)
            (firmware / "wifi/fw.bin").write_bytes(b"firmware")
            report = modules.report([tree], [firmware])
            wifi = report["modules"][1]
            self.assertEqual(report["module_count"], 2)
            self.assertEqual(wifi["subsystem_hints"], ["wifi"])
            self.assertEqual(len(wifi["dependency_candidates"]["core-driver"]), 1)
            self.assertEqual(wifi["dependency_candidates"]["missing"], [])
            self.assertEqual(wifi["firmware_checks"]["wifi/fw.bin"]["status"], "present")
            self.assertEqual(wifi["firmware_checks"]["missing.bin"]["status"],
                             "absent_in_supplied_roots")
            self.assertEqual(modules.report([tree], [])["modules"][1]
                             ["firmware_checks"]["wifi/fw.bin"]["status"], "not_checked")

    def test_non_utf8_modinfo(self):
        with self.assertRaises(ValueError):
            modules.modinfo(elf(b"name=x\0vermagic=v\0firmware=\xff\xfe\0"))

    def test_cpio_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "modules.cpio"
            path.write_bytes(builder.newc([
                ("lib/modules/a.ko", stat.S_IFREG | 0o644, elf(), 0, 0),
                ("lib/modules/b.ko", stat.S_IFREG | 0o644, elf(), 0, 0)]))
            report = modules.report([path], [])
            self.assertEqual(len(report["ambiguous_module_names"]["test"]), 2)

    def test_reject_duplicate_or_symlink_cpio(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "modules.cpio"
            entry = ("a.ko", stat.S_IFREG | 0o644, elf(), 0, 0)
            for entries in ([entry, entry], [("a.ko", stat.S_IFLNK | 0o777, b"x", 0, 0)]):
                path.write_bytes(builder.newc(entries))
                with self.assertRaises(ValueError):
                    modules.report([path], [])

    def test_reject_duplicate_input(self):
        with tempfile.TemporaryDirectory() as work:
            tree = Path(work) / "modules"
            tree.mkdir()
            (tree / "a.ko").write_bytes(elf())
            with self.assertRaises(ValueError):
                modules.report([tree, tree], [])

    def test_reject_symlink_module(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "a.ko").symlink_to("other")
            with self.assertRaises(ValueError):
                modules.report([root], [])

    def test_firmware_path_boundary(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            tree = root / "modules"
            tree.mkdir()
            for name in (b"../escape", b"/absolute", b""):
                (tree / "a.ko").write_bytes(elf(b"name=a\0vermagic=v\0firmware=" + name + b"\0"))
                with self.assertRaises(ValueError):
                    modules.report([tree], [])
            (tree / "a.ko").write_bytes(elf(b"name=a\0vermagic=v\0firmware=escape\0"))
            (root / "escape").symlink_to(tree / "a.ko")
            firmware = root / "firmware"
            firmware.mkdir()
            (firmware / "escape").symlink_to(tree / "a.ko")
            report = modules.report([tree], [firmware])
            self.assertEqual(report["modules"][0]["firmware_checks"]["escape"]["status"],
                             "absent_in_supplied_roots")


if __name__ == "__main__":
    unittest.main()

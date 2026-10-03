import importlib.util
from pathlib import Path
import stat
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("build_initramfs")
ramdisk = load("inspect_ramdisk")


def archive(name="init", mode=stat.S_IFREG | 0o755, value=b"test"):
    return builder.newc([(name, mode, value, 0, 0)])


class RamdiskTests(unittest.TestCase):
    def test_regular_member(self):
        self.assertEqual(ramdisk.select_regular(archive(), "init"), b"test")

    def test_concatenated_archives(self):
        data = archive("first") + archive("second")
        self.assertEqual([x[0] for x in ramdisk.members(data)], ["first", "second"])

    def test_duplicate_regular_member_rejected(self):
        with self.assertRaises(ValueError):
            ramdisk.select_regular(archive() + archive(), "init")

    def test_no_symlink_or_device_copy(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o600, stat.S_IFDIR | 0o755):
            with self.assertRaises(ValueError):
                ramdisk.select_regular(archive(mode=mode), "init")

    def test_absent_member(self):
        with self.assertRaises(ValueError):
            ramdisk.select_regular(archive(), "missing")

    def test_unsafe_path_rejected(self):
        for name in (b"../x", b"/xxx"):
            data = bytearray(archive())
            data[110:114] = name
            with self.assertRaises(ValueError):
                list(ramdisk.members(data))

    def test_truncation_and_missing_trailer(self):
        data = archive()
        for invalid in (data[:100], data[:120], data[:124], data + b"garbage", b""):
            with self.assertRaises(ValueError):
                list(ramdisk.members(invalid))

    def test_bad_name_terminator(self):
        data = bytearray(archive())
        data[114] = 65
        with self.assertRaises(ValueError):
            list(ramdisk.members(data))


if __name__ == "__main__":
    unittest.main()

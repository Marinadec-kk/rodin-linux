import gzip
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# These CLI tools also import sibling modules when executed directly.
sys.path.insert(0, str(ROOT / "tools"))
try:
    import build_candidate as candidate
    import build_initramfs as ramfs
    import inspect_boot
    import inspect_ramdisk
finally:
    sys.path.pop(0)


class CandidateTests(unittest.TestCase):
    def original(self):
        image = bytearray(256 * 1024)
        image[:8] = b"ANDROID!"
        struct.pack_into("<4I", image, 8, 0, 12, 12345, 1584)
        struct.pack_into("<I", image, 40, 4)
        image[44:48] = b"test"
        return bytes(image)

    def test_header_preservation_and_ramdisk_roundtrip(self):
        original = self.original()
        image = candidate.unsigned_image(original, b"payload")
        header = bytearray(original[:4096])
        struct.pack_into("<I", header, 12, 7)
        self.assertEqual(image[:4096], bytes(header))
        self.assertEqual(len(image) % 4096, 0)
        layout = inspect_boot.inspect(image)
        self.assertEqual(layout["kernel"]["size"], 0)
        self.assertEqual(layout["ramdisk"]["size"], 7)
        self.assertEqual(image[4096:4103], b"payload")
        self.assertIsNone(layout["avb_footer"])

    def test_reject_wrong_image_type(self):
        for offset, value in ((8, 1), (40, 3), (1580, 100)):
            image = bytearray(self.original())
            struct.pack_into("<I", image, offset, value)
            with self.assertRaises(ValueError):
                candidate.unsigned_image(bytes(image), b"payload")

    def test_reject_empty_or_oversized_ramdisk(self):
        for value in (b"", b"x" * (256 * 1024)):
            with self.assertRaises(ValueError):
                candidate.unsigned_image(self.original(), value)

    def plan(self, loads, deps, files):
        with patch.object(candidate, "SEEDS", {"mtu3.ko"}):
            return candidate.module_plan(loads, deps, files)

    def test_dependency_order_and_deduplication(self):
        files = {"lib/modules/mtu3.ko": b"", "lib/modules/phy.ko": b""}
        plan = self.plan("mtu3.ko\nmtu3.ko\n",
                         "/lib/modules/mtu3.ko: /lib/modules/phy.ko\n/lib/modules/phy.ko:\n", files)
        self.assertEqual(plan, ["/lib/modules/phy.ko", "/lib/modules/mtu3.ko"])

    def test_missing_seed_module_or_metadata(self):
        for loads, deps, files in (
            ("other.ko\n", "/lib/modules/other.ko:\n", {"lib/modules/other.ko": b""}),
            ("mtu3.ko\n", "/lib/modules/mtu3.ko:\n", {}),
            ("mtu3.ko\n", "", {"lib/modules/mtu3.ko": b""}),
            ("mtu3.ko\n", "/lib/modules/mtu3.ko: /lib/modules/absent.ko\n", {"lib/modules/mtu3.ko": b""}),
        ):
            with self.assertRaises(ValueError):
                self.plan(loads, deps, files)

    def test_excluded_watchdog_dependency(self):
        files = {"lib/modules/mtu3.ko": b"", "lib/modules/mtk_wdt.ko": b""}
        with self.assertRaises(ValueError):
            self.plan("mtu3.ko\n", "/lib/modules/mtu3.ko: /lib/modules/mtk_wdt.ko\n"
                      "/lib/modules/mtk_wdt.ko:\n", files)

    def test_dependency_cycle(self):
        files = {"lib/modules/mtu3.ko": b"", "lib/modules/phy.ko": b""}
        with self.assertRaises(ValueError):
            self.plan("mtu3.ko\n", "/lib/modules/mtu3.ko: /lib/modules/phy.ko\n"
                      "/lib/modules/phy.ko: /lib/modules/mtu3.ko\n", files)

    def test_unsafe_module_paths_or_duplicate_metadata(self):
        for loads, deps in (
            ("../mtu3.ko\n", ""),
            ("mtu3.ko\n", "/lib/modules/mtu3.ko: /lib/modules/../bad.ko\n"),
            ("mtu3.ko\n", "/lib/modules/mtu3.ko:;command\n"),
            ("mtu3.ko\n", "/lib/modules/mtu3.ko:\n/lib/modules/mtu3.ko:\n"),
        ):
            with self.assertRaises(ValueError):
                self.plan(loads, deps, {})

    def test_extra_initramfs_files(self):
        extra = [("etc", stat.S_IFDIR | 0o755, b"", 0, 0),
                 ("etc/plan", stat.S_IFREG | 0o600, b"checked", 0, 0)]
        cpio, compressed = ramfs.build(b"binary", b"init", extra)
        self.assertEqual(gzip.decompress(compressed), cpio)
        self.assertEqual(inspect_ramdisk.select_regular(cpio, "etc/plan"), b"checked")
        with self.assertRaises(ValueError):
            ramfs.build(b"binary", b"init", [("init", stat.S_IFREG | 0o600, b"evil", 0, 0)])

    def test_publish_does_not_follow_existing_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "restore.img"
            original.write_bytes(b"original")
            output = root / "output"
            output.mkdir()
            (output / "candidate.img").symlink_to(original)
            candidate.publish(output, "candidate.img", b"candidate")
            self.assertEqual(original.read_bytes(), b"original")
            self.assertEqual((output / "candidate.img").read_bytes(), b"candidate")
            self.assertFalse((output / "candidate.img").is_symlink())

    def test_reject_absent_or_incomplete_kernel_config(self):
        for kernel in (b"no config", b"IKCFG_ST" + gzip.compress(b"CONFIG_RD_GZIP=y\n") + b"IKCFG_ED"):
            with self.assertRaises(ValueError):
                candidate.kernel_config(kernel)


if __name__ == "__main__":
    unittest.main()

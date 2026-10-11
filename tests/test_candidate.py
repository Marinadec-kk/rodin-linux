from contextlib import ExitStack, redirect_stdout
import gzip
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from test_host_tools import vendor_image

ROOT = Path(__file__).resolve().parents[1]
# These CLI tools also import sibling modules when executed directly.
sys.path.insert(0, str(ROOT / "tools"))
try:
    import build_candidate as candidate
    import build_initramfs as ramfs
    import inspect_boot
    import inspect_ramdisk
    import smoke_arm64 as smoke
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

    def test_generated_module_artifact_contract(self):
        files = {"lib/modules/seed-one.ko": b"\0name=seed_one\0",
                 "lib/modules/provider.ko": b"\0name=provider\0",
                 "lib/modules/modules.load": b"seed-one.ko\n",
                 "lib/modules/modules.dep": b"/lib/modules/seed-one.ko: /lib/modules/provider.ko\n/lib/modules/provider.ko:\n"}
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            ota, output = root / "build/ota", root / "build/candidate"
            ota.mkdir(parents=True)
            (root / "initramfs").mkdir()
            (root / "initramfs/usb-init").write_bytes(b"fixture init")
            original = self.original()
            for name in candidate.ORIGINAL_HASHES:
                (ota / name).write_bytes(original)
            tool = root / "host-tool"
            tool.write_bytes(b"fixture tool")
            unsigned = candidate.unsigned_image
            mocks = [(candidate, "ROOT", root),
                     (candidate, "ORIGINAL_HASHES", {n: candidate.digest(original) for n in candidate.ORIGINAL_HASHES}),
                     (candidate, "SEEDS", {"seed-one.ko"})]
            for obj, name, value in mocks:
                stack.enter_context(patch.object(obj, name, value))
            stack.enter_context(patch.object(candidate.shutil, "which", return_value=str(tool)))
            stack.enter_context(patch.object(candidate.subprocess, "check_output", side_effect=["avbtool 1.4.0\n", b"fixture AVB info"]))
            stack.enter_context(patch.object(candidate.subprocess, "run"))
            stack.enter_context(patch.object(candidate, "decompress_lz4", return_value=b"kernel"))
            stack.enter_context(patch.object(candidate, "kernel_config", return_value=b"config"))
            stack.enter_context(patch.object(candidate, "platform_members", return_value=files))
            stack.enter_context(patch.object(ramfs, "read_busybox", return_value=b"fixture binary"))
            stack.enter_context(patch.object(candidate, "unsigned_image", side_effect=lambda orig, data: unsigned(orig, data).ljust(len(orig), b"\0")))
            stack.enter_context(patch.object(sys, "argv", ["build_candidate", "--apk", "fixture.apk"]))
            with redirect_stdout(io.StringIO()):
                candidate.main()
            manifest = json.loads((output / "manifest.json").read_text())
            loads = (output / "modules.load").read_text().splitlines()
            self.assertTrue(loads)
            for line in loads:
                self.assertRegex(line, r"^/lib/modules/[A-Za-z0-9_-]+\.ko [A-Za-z0-9_]+$")
            self.assertEqual(len(loads), manifest["module_count"])
            self.assertEqual(manifest["zlib_version"], zlib.ZLIB_VERSION)
            sums = dict(line.split()[::-1] for line in (output / "modules.sha256").read_text().splitlines())
            self.assertEqual(set(sums), {line.split()[0] for line in loads})
            staged = root / "staged"
            for name, data in files.items():
                target = staged / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            for path, digest in sums.items():
                self.assertEqual(digest, candidate.digest((staged / path.lstrip("/")).read_bytes()))
            archive = (output / "initramfs.cpio").read_bytes()
            for name in ("modules.load", "modules.sha256"):
                self.assertEqual(inspect_ramdisk.select_regular(archive, "etc/rodin/" + name), (output / name).read_bytes())

    def platform_image(self, types):
        image = vendor_image()
        struct.pack_into("<I", image, 24, max(3, 3 * len(types)))
        struct.pack_into("<4I", image, 2112, 108 * len(types), len(types), 108, 0)
        for index, kind in enumerate(types):
            image[4096 + 3 * index:4099 + 3 * index] = b"abc"
            struct.pack_into("<3I", image, 12288 + 108 * index, 3, 3 * index, kind)
        return bytes(image)

    def test_platform_members_filters_module_regular_files(self):
        entries = [("lib/modules/example.ko", stat.S_IFREG, b"module", 0, 0),
                   ("elsewhere/example.ko", stat.S_IFREG, b"other", 0, 0),
                   ("lib/modules/link.ko", stat.S_IFLNK, b"example.ko", 0, 0),
                   ("lib/modules/directory", stat.S_IFDIR, b"", 0, 0)]
        with patch.object(candidate, "decompress_lz4", return_value=ramfs.newc(entries)) as decompress:
            self.assertEqual(candidate.platform_members(self.platform_image([2, 1]), "fixture-lz4"),
                             {"lib/modules/example.ko": b"module"})
            decompress.assert_called_once_with(b"abc", "fixture-lz4")

    def test_platform_members_rejects_duplicate_member(self):
        entry = ("lib/modules/example.ko", stat.S_IFREG, b"module", 0, 0)
        with patch.object(candidate, "decompress_lz4", return_value=ramfs.newc([entry, entry])):
            with self.assertRaisesRegex(ValueError, "Ambiguous platform"):
                candidate.platform_members(self.platform_image([1]), "fixture-lz4")

    def test_platform_members_requires_exactly_one_platform(self):
        for types in ([], [2], [1, 1]):
            with self.subTest(types=types), patch.object(candidate, "decompress_lz4") as decompress:
                with self.assertRaisesRegex(ValueError, "exactly one PLATFORM"):
                    candidate.platform_members(self.platform_image(types), "fixture-lz4")
                decompress.assert_not_called()

    def module_loader(self, root, content):
        plan = root / "modules.load"
        if content is not None:
            plan.write_text(content)
        script = (ROOT / "initramfs/usb-init").read_text()
        function = script[script.index("load_modules() {"):script.index("\nusb_console()")]
        function = function.replace("/etc/rodin/modules.load", str(plan)).replace("/run/rodin-bringup.log", str(root / "log"))
        function = function.replace("/sys/module/", str(root / "sys/module") + "/")
        (root / "sys/module").mkdir(parents=True)
        return subprocess.run(["sh", "-c", 'log() { echo "$*"; }; bb() { [ "$1" = sha256sum ]; }; '
                               + function + '\nload_modules'], capture_output=True, text=True)

    def test_module_loader_rejects_missing_or_empty_plan(self):
        for content in (None, ""):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                result = self.module_loader(Path(temp), content)
                self.assertEqual(result.returncode, 1)
                self.assertIn("module load plan", result.stdout)

    def test_module_loader_rejects_blank_or_nameless_entry(self):
        for content in ("\n", "/lib/modules/example.ko\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                result = self.module_loader(Path(temp), content)
                self.assertEqual(result.returncode, 1)
                self.assertIn("entry has no name", result.stdout)

    def test_module_checksum_probe_synthetic_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ota").mkdir()
            (root / "candidate").mkdir()
            work = root / "work"
            work.mkdir()
            vendor = self.platform_image([1])
            (root / "ota/vendor_boot.img").write_bytes(vendor)
            data = b"fixture module"
            listing = root / "candidate/modules.sha256"
            listing.write_text(candidate.digest(data) + "  /lib/modules/example.ko\n")
            archive = ramfs.newc([("lib/modules/example.ko", stat.S_IFREG, data, 0, 0)])
            with patch.dict(candidate.ORIGINAL_HASHES, {"vendor_boot.img": candidate.digest(vendor)}), \
                    patch.object(candidate, "decompress_lz4", return_value=archive):
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(smoke.module_checksum_probe(["env"], work, root))
                self.assertIn(str(work / "platform/lib/modules/example.ko"), (work / "platform.sha256").read_text())
                listing.write_text("0" * 64 + "  /lib/modules/example.ko\n")
                with tempfile.TemporaryDirectory() as other, self.assertRaises(ValueError):
                    smoke.module_checksum_probe(["env"], Path(other), root)

    def test_module_checksum_probe_skips_absent_inputs(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output:
            self.assertFalse(smoke.module_checksum_probe(["unused"], Path(temp), Path(temp)))
            self.assertIn("SKIP", output.getvalue())

    def test_reject_absent_or_incomplete_kernel_config(self):
        for kernel in (b"no config", b"IKCFG_ST" + gzip.compress(b"CONFIG_RD_GZIP=y\n") + b"IKCFG_ED"):
            with self.assertRaises(ValueError):
                candidate.kernel_config(kernel)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verify_restore_inputs as restore


class RestoreInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.copy = base / "original", base / "offline"
        self.root.mkdir()
        self.copy.mkdir()
        self.entries = []
        for name in sorted(restore.REQUIRED):
            data = (name + " original").encode()
            self.entries.append({"name": name, "size": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest(),
                                 "operation_counts": {"8": 1}})
            for location in (self.root, self.copy):
                (location / (name + ".img")).write_bytes(data)
        self.manifest = base / "extraction.json"
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"source_rom_sha256": "0" * 64,
                                             "status": "host extraction only",
                                             "partitions": self.entries}))

    def report(self, copies=None):
        return restore.report(self.manifest, self.root, [self.copy] if copies is None else copies)

    def test_all_match(self):
        result = self.report()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["artifacts"]), 10)
        self.assertTrue(all(a["status"] == "PASS" for a in result["artifacts"]))

    def test_wrong_hash(self):
        path = self.root / "boot.img"
        path.write_bytes(b"x" * path.stat().st_size)
        result = self.report()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["artifacts"][0]["errors"], ["SHA-256 mismatch"])
        self.assertEqual(len(result["artifacts"]), 10)

    def test_wrong_size(self):
        self.entries[0]["size"] += 1
        self.write_manifest()
        result = self.report()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["artifacts"][0]["errors"], ["Size mismatch"])

    def test_missing_primary(self):
        (self.root / "init_boot.img").unlink()
        self.assertEqual(self.report()["status"], "FAIL")

    def test_missing_copy(self):
        (self.copy / "init_boot.img").unlink()
        result = self.report()
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any(a["status"] == "FAIL" and a["location"] == "copy-1"
                            for a in result["artifacts"]))

    def test_extra_unpinned_file_is_ignored(self):
        (self.root / "untrusted.img").write_bytes(b"not pinned")
        result = self.report()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["artifacts"]), 10)

    def test_copy_required_and_all_copies_checked(self):
        self.assertEqual(self.report([])["status"], "FAIL")
        third = self.copy.parent / "missing"
        result = self.report([self.copy, third])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(len(result["artifacts"]), 15)

    def test_same_or_nested_directory_is_not_independent(self):
        for copy in (self.root, self.root / "child"):
            with self.subTest(copy=copy):
                self.assertEqual(self.report([copy])["status"], "FAIL")
        alias = self.copy.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.report([alias])["status"], "FAIL")

    def test_symlink_and_hardlink_are_not_copies(self):
        path = self.copy / "boot.img"
        path.unlink()
        path.symlink_to(self.root / "boot.img")
        self.assertEqual(self.report()["status"], "FAIL")
        path.unlink()
        os.link(self.root / "boot.img", path)
        self.assertEqual(self.report()["status"], "FAIL")

    def test_invalid_manifest(self):
        original = [dict(entry) for entry in self.entries]
        for key, value in (("name", "../boot"), ("size", True), ("size", -1),
                           ("sha256", "invalid")):
            self.entries = [dict(entry) for entry in original]
            self.entries[0][key] = value
            self.write_manifest()
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.report()
        for entries in (original[:-1], original + [original[0]]):
            self.entries = entries
            self.write_manifest()
            with self.assertRaises(ValueError):
                self.report()
        self.manifest.write_text('{"partitions": {}}')
        with self.assertRaises(ValueError):
            self.report()

    def test_additional_pinned_bootstrap_is_checked(self):
        self.entries.append({"name": "bootstrap", "size": 1,
                             "sha256": hashlib.sha256(b"b").hexdigest()})
        self.write_manifest()
        self.assertEqual(self.report()["status"], "FAIL")
        for location in (self.root, self.copy):
            (location / "bootstrap.img").write_bytes(b"b")
        self.assertEqual(self.report()["status"], "PASS")

    def test_cli_exit_codes_and_json(self):
        command = [sys.executable, str(ROOT / "tools/verify_restore_inputs.py"),
                   "--manifest", str(self.manifest), "--root", str(self.root)]
        for args, expected in (([], 1), (["--copy", str(self.copy)], 0)):
            run = subprocess.run(command + args, capture_output=True, text=True)
            self.assertEqual(run.returncode, expected, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "FAIL" if expected else "PASS")
        self.manifest.write_text("not JSON")
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()

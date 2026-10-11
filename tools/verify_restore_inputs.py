#!/usr/bin/env python3
"""Verify pinned OTA restore images and offline copies on the host; never access the phone."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat

REQUIRED = {"boot", "init_boot", "vendor_boot", "system_dlkm", "vendor_dlkm"}


def load_manifest(path):
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("partitions"), list):
        raise ValueError("Manifest must contain a partitions list")
    entries = manifest["partitions"]
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Partition entry must be an object")
        name, size, digest = (entry.get(key) for key in ("name", "size", "sha256"))
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("Unsafe/invalid partition name")
        if name in names:
            raise ValueError("Duplicate partition: " + name)
        names.add(name)
        if type(size) is not int or size <= 0:
            raise ValueError("Invalid size for " + name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid SHA-256 for " + name)
    if REQUIRED - names:
        raise ValueError("Missing pinned partitions: " + ", ".join(sorted(REQUIRED - names)))
    return entries


def report(manifest, root, copies):
    entries = load_manifest(manifest)
    locations = [root, *copies]
    errors = []
    if not copies:
        errors.append("At least one independent offline --copy is required")
    resolved = [path.resolve() for path in locations]
    for index, path in enumerate(resolved):
        if not path.is_dir():
            errors.append("Not an existing directory: " + str(locations[index]))
        for previous in resolved[:index]:
            if path.is_relative_to(previous) or previous.is_relative_to(path):
                errors.append("Locations must be distinct, non-nested directories: " + str(path))
    artifacts = []
    identities = {}
    for index, location in enumerate(locations):
        for entry in entries:
            path = location / (entry["name"] + ".img")
            result = {"location": "root" if index == 0 else "copy-" + str(index),
                      "path": str(path), "expected_size": entry["size"],
                      "expected_sha256": entry["sha256"], "errors": []}
            try:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Not a regular file (symlinks are not accepted)")
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in identities:
                    result["errors"].append("Same file as " + identities[identity])
                identities[identity] = str(path)
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                result.update(size=size, sha256=digest.hexdigest())
                if size != entry["size"]:
                    result["errors"].append("Size mismatch")
                if result["sha256"] != entry["sha256"]:
                    result["errors"].append("SHA-256 mismatch")
            except (OSError, ValueError) as error:
                result["errors"].append(str(error))
            result["status"] = "FAIL" if result["errors"] else "PASS"
            artifacts.append(result)
    return {"status": "FAIL" if errors or any(a["status"] == "FAIL" for a in artifacts) else "PASS",
            "errors": errors, "artifacts": artifacts,
            "limits": ["Only manifest-pinned <name>.img files are checked; extra files are ignored.",
                       "Storage independence/offline state and manifest provenance require operator confirmation.",
                       "Integrity is not live-partition equivalence or demonstrated recovery."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="Pinned extraction.json with partitions/name/size/sha256")
    parser.add_argument("--root", type=Path, required=True,
                        help="Primary directory containing original <name>.img files")
    parser.add_argument("--copy", type=Path, action="append", default=[],
                        help="Independent offline copy directory; repeatable, at least one required for PASS")
    args = parser.parse_args()
    try:
        result = report(args.manifest, args.root, args.copy)
    except (OSError, ValueError) as error:
        result = {"status": "FAIL", "errors": [str(error)], "artifacts": []}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

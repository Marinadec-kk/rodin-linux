#!/usr/bin/env python3
"""Inspect uncompressed newc ramdisks; optionally copy one regular host file.

Never materializes an archive tree, symlinks, or device nodes. Compressed input
must be decompressed separately on the host. Concatenated archives are supported.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat


def members(data):
    offset, seen_trailer = 0, False
    while offset < len(data):
        if data[offset] == 0 and seen_trailer:
            offset += 1
            continue
        if offset + 110 > len(data) or data[offset:offset + 6] != b"070701":
            raise ValueError("Truncated or unsupported newc header")
        try:
            fields = [int(data[offset + 6 + 8 * i:offset + 14 + 8 * i], 16) for i in range(13)]
        except ValueError as error:
            raise ValueError("Invalid newc numeric field") from error
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_start = offset + 110
        if not 1 <= name_size <= 4096 or name_start + name_size > len(data):
            raise ValueError("Invalid or truncated newc name")
        encoded = data[name_start:name_start + name_size]
        if encoded[-1] != 0 or b"\0" in encoded[:-1]:
            raise ValueError("Invalid newc name terminator")
        name = encoded[:-1].decode("utf-8")
        start = (name_start + name_size + 3) // 4 * 4
        if start + size > len(data):
            raise ValueError("Truncated newc file")
        offset = (start + size + 3) // 4 * 4
        if name == "TRAILER!!!":
            if size:
                raise ValueError("Invalid newc trailer")
            seen_trailer = True
            continue
        if seen_trailer:
            seen_trailer = False
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe ramdisk member path")
        yield str(path), mode, data[start:start + size]
    if not seen_trailer:
        raise ValueError("Missing final newc trailer")


def select_regular(data, name):
    matches = [(mode, value) for path, mode, value in members(data) if path == name]
    if len(matches) != 1 or not stat.S_ISREG(matches[0][0]):
        raise ValueError("Requested member must be a unique regular file")
    return matches[0][1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cpio", type=Path)
    parser.add_argument("--member")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if bool(args.member) != bool(args.output):
        parser.error("--member and --output must be provided together")
    if args.output and args.output.resolve() == args.cpio.resolve():
        parser.error("Output must not overwrite input")
    data = args.cpio.read_bytes()
    if args.member:
        value = select_regular(data, args.member)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(value)
        print(json.dumps({"member": args.member, "size": len(value),
                          "sha256": hashlib.sha256(value).hexdigest()}))
    else:
        print(json.dumps([{"name": name, "mode": oct(mode), "size": len(value),
                           "sha256": hashlib.sha256(value).hexdigest()}
                          for name, mode, value in members(data)], indent=2))


if __name__ == "__main__":
    main()

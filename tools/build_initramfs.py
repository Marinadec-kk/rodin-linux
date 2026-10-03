#!/usr/bin/env python3
"""Build a deterministic research initramfs without root or flashing a device."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import tarfile

APK_NAME = "busybox-static-1.37.0-r30.apk"
APK_URL = f"https://dl-cdn.alpinelinux.org/alpine/v3.23/main/aarch64/{APK_NAME}"
APK_SHA256 = "44c9abdfb970f398fa72c8382fe2d8808eea16beaf82daf6ac708b92f1b8659e"
ROOT = Path(__file__).resolve().parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_busybox(data):
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected little-endian ELF64 BusyBox")
    if struct.unpack_from("<H", data, 18)[0] != 183:
        raise ValueError("Expected AArch64, not a host-architecture executable")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    if not count or size < 56 or offset < 64 or offset + size * count > len(data):
        raise ValueError("Invalid ELF program header table")
    for i in range(count):
        kind, _, file_offset, _, _, file_size, _, _ = struct.unpack_from(
            "<IIQQQQQQ", data, offset + i * size)
        if file_offset + file_size > len(data):
            raise ValueError("ELF segment outside file")
        if kind == 3:
            raise ValueError("BusyBox must not require a PT_INTERP loader")
        if kind == 2:
            # Static PIE can have PT_DYNAMIC for self-relocation, but no DT_NEEDED.
            if file_size % 16:
                raise ValueError("Invalid ELF dynamic table")
            for entry in range(file_offset, file_offset + file_size, 16):
                tag, _ = struct.unpack_from("<qQ", data, entry)
                if tag == 0:
                    break
                if tag == 1:
                    raise ValueError("BusyBox must not require shared libraries")


def read_busybox(apk):
    data = Path(apk).read_bytes()
    if sha256(data) != APK_SHA256:
        raise ValueError("APK SHA-256 mismatch; refusing unpinned input")
    # Read one known member, never extract arbitrary archive paths to disk.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("bin/busybox.static")
        if not member.isfile() or member.size > 4 * 1024 * 1024:
            raise ValueError("Invalid BusyBox archive member")
        binary = archive.extractfile(member).read()
    validate_busybox(binary)
    return binary


def newc(entries):
    """entries: (relative path, mode including file type, data, device major/minor)."""
    out = bytearray()
    all_entries = list(entries) + [("TRAILER!!!", 0, b"", 0, 0)]
    for inode, (name, mode, data, major, minor) in enumerate(all_entries, 1):
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or "\x00" in name:
            raise ValueError("Unsafe cpio path")
        encoded = name.encode("utf-8") + b"\x00"
        fields = (inode, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1,
                  0, len(data), 0, 0, major, minor, len(encoded), 0)
        out += b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
        out += encoded
        out += b"\x00" * (-len(out) % 4)
        out += data
        out += b"\x00" * (-len(out) % 4)
    out += b"\x00" * (-len(out) % 512)
    return bytes(out)


def build(binary, init):
    entries = []
    for name in ("bin", "sbin", "dev", "dev/pts", "proc", "sys", "run", "root"):
        entries.append((name, stat.S_IFDIR | (0o700 if name == "root" else 0o755), b"", 0, 0))
    entries += [("bin/busybox", stat.S_IFREG | 0o755, binary, 0, 0),
                ("bin/sh", stat.S_IFLNK | 0o777, b"busybox", 0, 0),
                ("init", stat.S_IFREG | 0o755, init, 0, 0)]
    for name, major, minor in (("console", 5, 1), ("null", 1, 3), ("tty", 5, 0), ("kmsg", 1, 11)):
        entries.append((f"dev/{name}", stat.S_IFCHR | 0o600, b"", major, minor))
    cpio = newc(entries)
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0, compresslevel=9) as f:
        f.write(cpio)
    return cpio, compressed.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "initramfs")
    args = parser.parse_args()
    binary = read_busybox(args.apk)
    init = (ROOT / "initramfs" / "init").read_bytes()
    cpio, compressed = build(binary, init)
    manifest = {
        "status": "host-built research artifact; never boot-tested; NOT a flashable boot image",
        "architecture": "aarch64", "busybox_apk_url": APK_URL,
        "busybox_apk_sha256": APK_SHA256, "busybox_license": "GPL-2.0-only",
        "busybox_binary_sha256": sha256(binary), "init_sha256": sha256(init),
        "cpio_sha256": sha256(cpio), "gzip_sha256": sha256(compressed),
        "cpio_size": len(cpio), "gzip_size": len(compressed),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "initramfs.cpio").write_bytes(cpio)
    (args.output / "initramfs.cpio.gz").write_bytes(compressed)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only Android boot/init_boot v3/v4 structural inspector."""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def avb_footer(data):
    if len(data) < 64 or data[-64:-60] != b"AVBf":
        return None
    major, minor, original_size, offset, size = struct.unpack_from(">IIQQQ", data, len(data) - 60)
    if major != 1 or original_size > offset or size < 256 or offset + size > len(data) - 64:
        raise ValueError("Invalid AVB footer bounds/version")
    if data[offset:offset + 4] != b"AVB0":
        raise ValueError("AVB footer does not point to a vbmeta header")
    return {"version_major": major, "version_minor": minor,
            "original_image_size": original_size, "vbmeta_offset": offset,
            "vbmeta_size": size, "signature_verification": "not performed"}


def inspect(data):
    if len(data) < 1580 or data[:8] != b"ANDROID!":
        raise ValueError("Not an Android boot image")
    kernel_size, ramdisk_size, _, header_size = struct.unpack_from("<4I", data, 8)
    version = struct.unpack_from("<I", data, 40)[0]
    if version not in (3, 4) or header_size != (1584 if version == 4 else 1580):
        raise ValueError("Only Android boot header v3/v4 supported")
    if len(data) < header_size:
        raise ValueError("Truncated boot header")
    signature_size = struct.unpack_from("<I", data, 1580)[0] if version == 4 else 0
    ramdisk_offset = 4096 + (kernel_size + 4095) // 4096 * 4096
    signature_offset = ramdisk_offset + (ramdisk_size + 4095) // 4096 * 4096
    result = {"image_sha256": hashlib.sha256(data).hexdigest(), "image_size": len(data),
              "header_version": version, "header_size": header_size, "page_size": 4096,
              "signature_offset": signature_offset, "signature_size": signature_size,
              "avb_footer": avb_footer(data)}
    for name, offset, size in (("kernel", 4096, kernel_size),
                               ("ramdisk", ramdisk_offset, ramdisk_size),
                               ("signature", signature_offset, signature_size)):
        if offset + size > len(data):
            raise ValueError(f"Truncated {name} section")
        section = data[offset:offset + size]
        result[name] = {"offset": offset, "size": size,
                        "sha256": hashlib.sha256(section).hexdigest(),
                        "magic_hex": section[:8].hex()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.image.read_bytes()), indent=2))


if __name__ == "__main__":
    main()

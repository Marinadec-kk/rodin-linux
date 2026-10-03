#!/usr/bin/env python3
"""Read-only Android vendor_boot v3/v4 inspector. No device commands or writes."""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def align(value, page):
    return (value + page - 1) // page * page


def inspect(data):
    if len(data) < 2112 or data[:8] != b"VNDRBOOT":
        raise ValueError("Not a vendor_boot image")
    version, page, _, _, ramdisk_size = struct.unpack_from("<5I", data, 8)
    if version not in (3, 4) or page < 512 or page > 65536 or page & (page - 1):
        raise ValueError("Unsupported version or invalid page size")
    header_size, dtb_size = struct.unpack_from("<2I", data, 2096)
    if header_size != (2128 if version == 4 else 2112) or len(data) < header_size:
        raise ValueError("Invalid or truncated header")
    ramdisk_offset = align(header_size, page)
    dtb_offset = ramdisk_offset + align(ramdisk_size, page)
    table_offset = dtb_offset + align(dtb_size, page)
    if ramdisk_offset + ramdisk_size > len(data) or dtb_offset + dtb_size > len(data):
        raise ValueError("Truncated ramdisk or DTB")
    result = {
        "image_sha256": hashlib.sha256(data).hexdigest(), "image_size": len(data),
        "header_version": version, "page_size": page, "header_size": header_size,
        "ramdisk_offset": ramdisk_offset, "ramdisk_size": ramdisk_size,
        "dtb_offset": dtb_offset, "dtb_size": dtb_size, "entries": [],
    }
    if version == 4:
        table_size, count, entry_size, bootconfig_size = struct.unpack_from("<4I", data, 2112)
        if entry_size < 108 or table_size != count * entry_size:
            raise ValueError("Invalid ramdisk table dimensions")
        bootconfig_offset = table_offset + align(table_size, page)
        if table_offset + table_size > len(data) or bootconfig_offset + bootconfig_size > len(data):
            raise ValueError("Truncated ramdisk table or bootconfig")
        result.update(table_offset=table_offset, table_size=table_size,
                      bootconfig_offset=bootconfig_offset, bootconfig_size=bootconfig_size)
        ranges = []
        for index in range(count):
            position = table_offset + index * entry_size
            size, offset, kind = struct.unpack_from("<3I", data, position)
            if offset + size > ramdisk_size:
                raise ValueError("Ramdisk entry outside ramdisk section")
            if any(offset < end and offset + size > start for start, end in ranges):
                raise ValueError("Overlapping ramdisk fragments")
            ranges.append((offset, offset + size))
            name = data[position + 12:position + 44].split(b"\0", 1)[0].decode("ascii", errors="replace")
            fragment = data[ramdisk_offset + offset:ramdisk_offset + offset + size]
            result["entries"].append({"index": index, "name": name, "type": kind,
                                      "size": size, "offset_in_ramdisk": offset,
                                      "sha256": hashlib.sha256(fragment).hexdigest(),
                                      "compression_magic_hex": fragment[:4].hex()})
    # Do not emit command line or bootconfig contents: future device captures
    # may contain identifiers. This inspector only reports structural metadata.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.image.read_bytes()), indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Host-only extraction of selected partitions from the pinned full rodin OTA.

This deliberately supports only payload v2/minor 0 and full REPLACE operations,
not an arbitrary OTA installer. It never talks to a device or applies a delta.
Wire field numbers follow Android update_engine's update_metadata.proto.
"""
import argparse
import bz2
import hashlib
import json
import lzma
from pathlib import Path
import struct
import zipfile

ROM_SHA256 = "51a24fb889c286c27df892fcbee1f9977f9d32a76bbd1316900f3e90aa8a52e8"
ALLOWED = {"boot", "init_boot", "vendor_boot", "vendor_dlkm", "system_dlkm"}
MAX_PARTITION = 256 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def varint(data, offset):
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ValueError("Protobuf varint exceeds 64 bits")
        value |= (byte & 127) << (7 * index)
        if byte < 128:
            return value, offset
    raise ValueError("Unterminated protobuf varint")


def fields(data):
    offset, result = 0, {}
    while offset < len(data):
        key, offset = varint(data, offset)
        number, wire = key >> 3, key & 7
        if not number:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - offset:
                raise ValueError("Truncated protobuf field")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(number, []).append(value)
    return result


def one(message, number, default=None, kind=None):
    values = message.get(number, [])
    if not values:
        if default is None:
            raise ValueError(f"Missing protobuf field {number}")
        value = default
    elif len(values) != 1:
        raise ValueError(f"Duplicate singular protobuf field {number}")
    else:
        value = values[0]
    if kind is not None and not isinstance(value, kind):
        raise ValueError(f"Wrong type for protobuf field {number}")
    return value


def decode_operation(kind, blob, expected):
    if kind == 0:
        result = blob
    elif kind in (1, 8):
        decoder = (bz2.BZ2Decompressor() if kind == 1 else
                   lzma.LZMADecompressor(memlimit=MAX_PARTITION))
        result = decoder.decompress(blob, max_length=expected + 1)
        if not decoder.eof or decoder.unused_data:
            raise ValueError("Incomplete, oversized or trailing compressed operation")
    elif kind == 6:
        if blob:
            raise ValueError("ZERO operation contains data")
        result = b"\0" * expected
    else:
        raise ValueError(f"Unsupported operation type {kind}; deltas/discards are rejected")
    if len(result) != expected:
        raise ValueError("Operation size does not match destination extents")
    return result


def extract_partition(message, block_size, read_blob):
    name = one(message, 1, kind=bytes).decode("ascii")
    if name not in ALLOWED or 6 in message:
        raise ValueError("Partition not allowed or requires an old image")
    info = fields(one(message, 7, kind=bytes))
    size = one(info, 1, kind=int)
    expected_hash = one(info, 2, kind=bytes)
    if not 0 < size <= MAX_PARTITION or size % block_size or len(expected_hash) != 32:
        raise ValueError("Invalid partition size/hash")
    image = bytearray(size)
    written = bytearray(size // block_size)
    operations = message.get(8, [])
    if not operations:
        raise ValueError("Partition has no operations")
    counts = {}
    for raw in operations:
        op = fields(raw)
        kind = one(op, 1, kind=int)
        if kind not in (0, 1, 6, 8) or 4 in op or 5 in op or 9 in op:
            raise ValueError("Only full-image replacement/zero operations supported")
        extents = []
        for extent in op.get(6, []):
            e = fields(extent)
            start, count = one(e, 1, kind=int), one(e, 2, kind=int)
            if not count or start + count > len(written):
                raise ValueError("Destination extent outside partition")
            # Mark while checking to catch overlapping extents within one op too.
            if any(written[start:start + count]):
                raise ValueError("Overlapping destination extents")
            written[start:start + count] = b"\1" * count
            extents.append((start * block_size, count * block_size))
        expected = sum(length for _, length in extents)
        if not expected:
            raise ValueError("Operation has no destination extents")
        if 7 in op and one(op, 7, kind=int) != expected:
            raise ValueError("Destination length mismatch")
        offset = one(op, 2, default=0, kind=int)
        length = one(op, 3, default=0, kind=int)
        if length > MAX_PARTITION:
            raise ValueError("Operation blob too large")
        blob = read_blob(offset, length)
        if len(blob) != length:
            raise ValueError("Truncated operation blob")
        if length:
            digest = one(op, 8, kind=bytes)
            if len(digest) != 32 or hashlib.sha256(blob).digest() != digest:
                raise ValueError("Operation blob SHA-256 mismatch")
        decoded = decode_operation(kind, blob, expected)
        cursor = 0
        for start, length in extents:
            image[start:start + length] = decoded[cursor:cursor + length]
            cursor += length
        counts[str(kind)] = counts.get(str(kind), 0) + 1
    if not all(written):
        raise ValueError("Partition contains unwritten blocks")
    actual_hash = hashlib.sha256(image).digest()
    if actual_hash != expected_hash:
        raise ValueError("Extracted partition SHA-256 mismatch")
    return bytes(image), {"name": name, "size": size, "sha256": actual_hash.hex(),
                          "operation_counts": counts}


def read_payload(rom, requested):
    digest = hashlib.sha256()
    with rom.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != ROM_SHA256:
        raise ValueError("ROM SHA-256 mismatch; only the pinned rodin full OTA is accepted")
    with zipfile.ZipFile(rom) as archive, rom.open("rb") as stream:
        entry = archive.getinfo("payload.bin")
        if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1:
            raise ValueError("Expected unencrypted ZIP_STORED payload.bin")
        stream.seek(entry.header_offset)
        header = stream.read(30)
        if header[:4] != b"PK\x03\x04":
            raise ValueError("Invalid ZIP local header")
        name_size, extra_size = struct.unpack_from("<HH", header, 26)
        payload_start = entry.header_offset + 30 + name_size + extra_size
        stream.seek(payload_start)
        header = stream.read(24)
        if len(header) != 24 or header[:4] != b"CrAU":
            raise ValueError("Invalid payload header")
        version, manifest_size, signature_size = struct.unpack_from(">QQI", header, 4)
        if version != 2 or not 0 < manifest_size <= 16 * 1024 * 1024:
            raise ValueError("Only bounded payload v2 manifests are supported")
        blob_start = 24 + manifest_size + signature_size
        if blob_start > entry.file_size:
            raise ValueError("Metadata extends beyond payload")
        manifest_data = stream.read(manifest_size)
        if len(manifest_data) != manifest_size:
            raise ValueError("Truncated manifest")
        manifest = fields(manifest_data)
        if one(manifest, 12, default=0, kind=int) != 0:
            raise ValueError("Delta payloads are not supported")
        block_size = one(manifest, 3, default=4096, kind=int)
        if block_size != 4096:
            raise ValueError("Only 4096-byte payload blocks supported")
        partitions = {}
        for raw in manifest.get(13, []):
            p = fields(raw)
            name = one(p, 1, kind=bytes).decode("ascii")
            if name in partitions:
                raise ValueError("Duplicate partition name")
            partitions[name] = p
        if set(requested) - partitions.keys():
            raise ValueError("Requested partition is absent from manifest")

        def read_blob(offset, size):
            if offset + size > entry.file_size - blob_start:
                raise ValueError("Operation extends beyond payload")
            stream.seek(payload_start + blob_start + offset)
            return stream.read(size)

        for name in requested:
            yield extract_partition(partitions[name], block_size, read_blob)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("--partitions", nargs="+", choices=sorted(ALLOWED),
                        default=["boot", "init_boot", "vendor_boot"])
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "ota")
    args = parser.parse_args()
    if len(set(args.partitions)) != len(args.partitions):
        parser.error("Duplicate requested partition")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"source_rom_sha256": ROM_SHA256, "status": "host extraction only", "partitions": []}
    for image, info in read_payload(args.rom, args.partitions):
        target = args.output / (info["name"] + ".img")
        temporary = target.with_suffix(".img.part")
        temporary.write_bytes(image)
        temporary.replace(target)
        report["partitions"].append(info)
        print(json.dumps(info), flush=True)
    (args.output / "extraction.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()

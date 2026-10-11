#!/usr/bin/env python3
"""Host-only streaming extraction of one large partition from the pinned rodin OTA.

Companion to extract_boot_payload.py for partitions that exceed its in-memory
256 MiB limit (vendor, odm, ...). Only payload v2/minor 0 full REPLACE,
REPLACE_BZ, REPLACE_XZ and ZERO operations are accepted; deltas, discards and
other compression types (for example REPLACE_ZSTD) are rejected. Each
operation blob is checked against its manifest SHA-256 before decoding and the
finished image is re-read and checked against the manifest partition hash.
Only one decoded operation is held in memory at a time. It never talks to a
device.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_boot_payload as base  # noqa: E402

ALLOWED = {"vendor", "odm", "vendor_dlkm", "system_dlkm", "odm_dlkm",
           "connsys_wifi", "connsys_bt", "connsys_gnss"}
SUPPORTED_KINDS = (0, 1, 6, 8)  # REPLACE, REPLACE_BZ, ZERO, REPLACE_XZ
MAX_PARTITION = 4 * 1024 * 1024 * 1024
MAX_OPERATION = 64 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
PROTECTED_OUTPUT = ROOT / "build" / "ota"


def operation_plan(message, block_size):
    """Validate a partition message and return (name, size, hash, operations)."""
    name = base.one(message, 1, kind=bytes).decode("ascii")
    if name not in ALLOWED or 6 in message:
        raise ValueError("Partition not allowed or requires an old image")
    info = base.fields(base.one(message, 7, kind=bytes))
    size = base.one(info, 1, kind=int)
    expected_hash = base.one(info, 2, kind=bytes)
    if not 0 < size <= MAX_PARTITION or size % block_size or len(expected_hash) != 32:
        raise ValueError("Invalid partition size/hash")
    raw_operations = message.get(8, [])
    if not raw_operations:
        raise ValueError("Partition has no operations")
    written = bytearray(size // block_size)
    plan = []
    for raw in raw_operations:
        op = base.fields(raw)
        kind = base.one(op, 1, kind=int)
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"Unsupported operation type {kind}; only full "
                             "REPLACE/REPLACE_BZ/REPLACE_XZ/ZERO are accepted")
        if 4 in op or 5 in op or 9 in op:
            raise ValueError("Operation references source data")
        extents = []
        for extent in op.get(6, []):
            e = base.fields(extent)
            start, count = base.one(e, 1, kind=int), base.one(e, 2, kind=int)
            if not count or start + count > len(written):
                raise ValueError("Destination extent outside partition")
            if any(written[start:start + count]):
                raise ValueError("Overlapping destination extents")
            written[start:start + count] = b"\1" * count
            extents.append((start * block_size, count * block_size))
        expected = sum(length for _, length in extents)
        if not expected:
            raise ValueError("Operation has no destination extents")
        if expected > MAX_OPERATION:
            raise ValueError("Operation destination too large")
        if 7 in op and base.one(op, 7, kind=int) != expected:
            raise ValueError("Destination length mismatch")
        offset = base.one(op, 2, default=0, kind=int)
        length = base.one(op, 3, default=0, kind=int)
        if length > MAX_OPERATION:
            raise ValueError("Operation blob too large")
        if kind == 6 and length:
            raise ValueError("ZERO operation contains data")
        if kind != 6 and not length:
            raise ValueError("Replace operation has no data")
        digest = base.one(op, 8, kind=bytes) if length else None
        if digest is not None and len(digest) != 32:
            raise ValueError("Invalid operation blob hash")
        plan.append((kind, offset, length, digest, extents, expected))
    if not all(written):
        raise ValueError("Partition contains unwritten blocks")
    return name, size, expected_hash, plan


def extract_partition_to(message, block_size, read_blob, out):
    """Stream one partition into the seekable, readable binary file `out`."""
    name, size, expected_hash, plan = operation_plan(message, block_size)
    out.seek(0)
    out.truncate(size)
    counts = {}
    for kind, offset, length, digest, extents, expected in plan:
        blob = read_blob(offset, length)
        if len(blob) != length:
            raise ValueError("Truncated operation blob")
        if length and hashlib.sha256(blob).digest() != digest:
            raise ValueError("Operation blob SHA-256 mismatch")
        decoded = base.decode_operation(kind, blob, expected)
        cursor = 0
        for start, chunk in extents:
            out.seek(start)
            out.write(decoded[cursor:cursor + chunk])
            cursor += chunk
        counts[str(kind)] = counts.get(str(kind), 0) + 1
    out.flush()
    out.seek(0, os.SEEK_END)
    if out.tell() != size:
        raise ValueError("Output size mismatch")
    out.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: out.read(4 * 1024 * 1024), b""):
        digest.update(chunk)
    if digest.digest() != expected_hash:
        raise ValueError("Extracted partition SHA-256 mismatch")
    return {"name": name, "size": size, "sha256": digest.hexdigest(),
            "operation_counts": counts}


def open_payload(rom, stream, check_rom=True):
    """Return (manifest_partitions, block_size, read_blob) for an open ROM stream."""
    if check_rom:
        digest = hashlib.sha256()
        stream.seek(0)
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != base.ROM_SHA256:
            raise ValueError("ROM SHA-256 mismatch; only the pinned rodin full OTA is accepted")
    with zipfile.ZipFile(rom) as archive:
        entry = archive.getinfo("payload.bin")
    if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1:
        raise ValueError("Expected unencrypted ZIP_STORED payload.bin")
    stream.seek(entry.header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
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
    manifest = base.fields(manifest_data)
    if base.one(manifest, 12, default=0, kind=int) != 0:
        raise ValueError("Delta payloads are not supported")
    block_size = base.one(manifest, 3, default=4096, kind=int)
    if block_size != 4096:
        raise ValueError("Only 4096-byte payload blocks supported")
    partitions = {}
    for raw in manifest.get(13, []):
        p = base.fields(raw)
        name = base.one(p, 1, kind=bytes).decode("ascii")
        if name in partitions:
            raise ValueError("Duplicate partition name")
        partitions[name] = p

    def read_blob(offset, size):
        if offset + size > entry.file_size - blob_start:
            raise ValueError("Operation extends beyond payload")
        stream.seek(payload_start + blob_start + offset)
        return stream.read(size)

    return partitions, block_size, read_blob


def extract(rom, name, output, check_rom=True, force=False):
    output = output.resolve()
    if output == PROTECTED_OUTPUT.resolve() or PROTECTED_OUTPUT.resolve() in output.parents:
        raise ValueError("Refusing to write under build/ota (original artifacts)")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{name}.img"
    if target.exists() and not force:
        raise ValueError(f"{target} exists; pass --force to replace it")
    temporary = output / f"{name}.img.part"
    with rom.open("rb") as stream:
        partitions, block_size, read_blob = open_payload(rom, stream, check_rom)
        if name not in partitions:
            raise ValueError("Requested partition is absent from manifest")
        try:
            with temporary.open("w+b") as out:
                info = extract_partition_to(partitions[name], block_size, read_blob, out)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(target)
    report_path = output / "extraction.json"
    report = {"source_rom_sha256": base.ROM_SHA256, "status": "host extraction only",
              "partitions": []}
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if report.get("source_rom_sha256") != base.ROM_SHA256:
            raise ValueError("Existing extraction.json refers to another ROM")
    report["partitions"] = [p for p in report["partitions"] if p.get("name") != name]
    report["partitions"].append(info)
    report["partitions"].sort(key=lambda p: p["name"])
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    parser.add_argument("--partition", required=True, choices=sorted(ALLOWED))
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "ota-extra")
    parser.add_argument("--force", action="store_true", help="replace an existing image")
    args = parser.parse_args()
    try:
        info = extract(args.rom, args.partition, args.output, force=args.force)
    except ValueError as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(info), flush=True)


if __name__ == "__main__":
    main()

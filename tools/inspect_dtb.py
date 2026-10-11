#!/usr/bin/env python3
"""Inspect one FDT v17, optionally preceded by a 64-byte MediaTek wrapper."""
import argparse
import json
from pathlib import Path
import struct

FDT_MAGIC = b"\xd0\x0d\xfe\xed"
MTK_MAGIC = b"\xd7\xb7\xab\x1e"


def property_value(raw):
    # FDT has no type tags; keep raw bytes to make the display heuristic lossless.
    if raw and raw.endswith(b"\0"):
        parts = raw[:-1].split(b"\0")
        if all(part and all(32 <= c < 127 for c in part) for part in parts):
            strings = [part.decode("ascii") for part in parts]
            return {"type": "string" if len(strings) == 1 else "strings",
                    "value": strings[0] if len(strings) == 1 else strings,
                    "hex": raw.hex()}
    if raw and len(raw) % 4 == 0:
        return {"type": "u32", "value": list(struct.unpack(">" + "I" * (len(raw) // 4), raw)), "hex": raw.hex()}
    return {"type": "bytes", "value": list(raw), "hex": raw.hex()}


def inspect(data):
    wrapper = data[:64] if data[:4] == MTK_MAGIC else b""
    data = data[len(wrapper):]
    if len(data) < 40:
        raise ValueError("Truncated FDT header")
    fields = struct.unpack_from(">10I", data)
    keys = ("magic", "totalsize", "off_dt_struct", "off_dt_strings", "off_mem_rsvmap",
            "version", "last_comp_version", "boot_cpuid_phys", "size_dt_strings", "size_dt_struct")
    header = dict(zip(keys, fields))
    magic, total, so, st, ro, version, last, _, sl, ss = fields
    if magic != 0xd00dfeed or version != 17 or last > 17:
        raise ValueError("Expected FDT version 17")
    if total != len(data):
        raise ValueError("Truncated FDT or trailing data (expected exactly one FDT)")
    if so % 4 or ro % 8 or min(so, st, ro) < 40:
        raise ValueError("Invalid block alignment/offset")
    if so + ss > total or st + sl > total or ro + 16 > total:
        raise ValueError("Block outside FDT")
    re = ro
    while True:
        if re + 16 > total:
            raise ValueError("Unterminated reservation map")
        address, size = struct.unpack_from(">QQ", data, re)
        re += 16
        if address == size == 0:
            break
    ranges = sorted([(0, 40), (so, so + ss), (st, st + sl), (ro, re)])
    if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
        raise ValueError("Overlapping FDT blocks")
    strings = data[st:st + sl]
    end = so + ss
    pos = so
    stack, nodes, compatibles, phandles = [], {}, {}, {}
    root = None

    def take(length):
        nonlocal pos
        if pos + length > end:
            raise ValueError("Truncated structure block")
        raw = data[pos:pos + length]
        pos += length
        return raw

    def text(raw):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Invalid FDT text") from error

    while pos < end:
        token = struct.unpack(">I", take(4))[0]
        if token == 1:
            stop = data.find(b"\0", pos, end)
            if stop < 0:
                raise ValueError("Unterminated node name")
            name = text(take(stop - pos + 1)[:-1])
            take((-pos) % 4)
            if "/" in name or (not stack and (name or root is not None)) or (stack and not name):
                raise ValueError("Invalid root/node name")
            path = (stack[-1]["path"].rstrip("/") + "/" + name) if stack else "/"
            if path in nodes:
                raise ValueError("Duplicate node")
            node = {"name": name, "path": path, "properties": {}, "children": []}
            nodes[path] = node
            if stack:
                stack[-1]["children"].append(node)
            else:
                root = node
            stack.append(node)
        elif token == 2:
            if not stack:
                raise ValueError("Unbalanced END_NODE")
            stack.pop()
        elif token == 3:
            if not stack or stack[-1]["children"]:
                raise ValueError("Property outside node or after child nodes")
            length, offset = struct.unpack(">II", take(8))
            stop = strings.find(b"\0", offset)
            if offset >= len(strings) or stop < 0:
                raise ValueError("Invalid property name offset")
            name = text(strings[offset:stop])
            props = stack[-1]["properties"]
            if not name or name in props:
                raise ValueError("Empty/duplicate property")
            raw = take(length)
            take((-pos) % 4)
            props[name] = property_value(raw)
        elif token == 4:
            continue
        elif token == 9:
            if stack or root is None or pos != end:
                raise ValueError("Invalid FDT_END")
            break
        else:
            raise ValueError("Unknown FDT token")
    else:
        raise ValueError("Missing FDT_END")
    for path, node in nodes.items():
        props = node["properties"]
        if "compatible" in props:
            prop = props["compatible"]
            if prop["type"] not in ("string", "strings"):
                raise ValueError("Invalid compatible strings")
            for compat in ([prop["value"]] if prop["type"] == "string" else prop["value"]):
                compatibles.setdefault(compat, []).append(path)
        handles = []
        for name in ("phandle", "linux,phandle"):
            if name in props:
                raw = bytes.fromhex(props[name]["hex"])
                if len(raw) != 4:
                    raise ValueError("Invalid phandle size")
                handle = struct.unpack(">I", raw)[0]
                if handle in (0, 0xffffffff):
                    raise ValueError("Invalid phandle")
                handles.append(handle)
        if len(set(handles)) > 1:
            raise ValueError("Conflicting phandle properties")
        for handle in set(handles):
            if str(handle) in phandles:
                raise ValueError("Duplicate phandle")
            phandles[str(handle)] = path
    return {"header": header, "wrapper_hex": wrapper.hex(), "tree": root,
            "compatible_index": compatibles, "phandles": phandles}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtb", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.dtb.read_bytes()), indent=2))


if __name__ == "__main__":
    main()

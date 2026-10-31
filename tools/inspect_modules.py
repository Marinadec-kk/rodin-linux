#!/usr/bin/env python3
"""Report ARM64 module metadata from uncompressed CPIO or extracted trees; never load modules."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import struct

import inspect_ramdisk


def modinfo(data):
    if (len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<HH", data, 16) != (1, 183)):
        raise ValueError("Expected little-endian ELF64 AArch64 relocatable module")
    offset = struct.unpack_from("<Q", data, 40)[0]
    size, count, names_index = struct.unpack_from("<HHH", data, 58)
    if size != 64 or not count or not 0 < names_index < count or offset < 64:
        raise ValueError("Unsupported ELF section table")
    if offset + size * count > len(data):
        raise ValueError("Truncated ELF section table")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, offset + size * i)
                for i in range(count)]

    def content(section):
        start, length = section[4:6]
        if start + length > len(data):
            raise ValueError("Truncated ELF section")
        return data[start:start + length]

    names = content(sections[names_index])
    matches = []
    for section in sections:
        start = section[0]
        end = names.find(b"\0", start)
        if start >= len(names) or end < 0:
            raise ValueError("Invalid ELF section name")
        if names[start:end] == b".modinfo":
            if section[1] != 1:
                raise ValueError("Expected PROGBITS modinfo")
            matches.append(content(section))
    if len(matches) != 1 or not matches[0].endswith(b"\0"):
        raise ValueError("Missing/ambiguous or unterminated modinfo")
    result = {}
    for entry in matches[0].split(b"\0"):
        if not entry:
            continue
        try:
            key, separator, value = entry.decode("utf-8").partition("=")
        except UnicodeDecodeError as error:
            raise ValueError("Non-UTF-8 modinfo entry") from error
        if not separator or not key:
            raise ValueError("Malformed modinfo entry")
        result.setdefault(key, []).append(value)
    for key in ("name", "vermagic"):
        if len(result.get(key, [])) != 1 or not result[key][0]:
            raise ValueError("Missing/ambiguous module " + key)
    return result


def input_modules(path):
    if path.is_dir():
        for file in sorted(path.rglob("*.ko")):
            if not stat.S_ISREG(file.lstat().st_mode):
                raise ValueError("Module input must be a regular file: " + str(file))
            yield file.relative_to(path).as_posix(), file.read_bytes()
    else:
        seen = set()
        for name, mode, data in inspect_ramdisk.members(path.read_bytes()):
            if name.endswith(".ko"):
                if name in seen or not stat.S_ISREG(mode):
                    raise ValueError("Duplicate/non-regular module in CPIO: " + name)
                seen.add(name)
                yield name, data


def report(inputs, firmware_roots):
    if len({path.resolve() for path in inputs}) != len(inputs):
        raise ValueError("Duplicate module input")
    modules = []
    for source in inputs:
        for path, data in input_modules(source):
            info = modinfo(data)
            modules.append({
                "source": str(source), "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "name": info["name"][0], "vermagic": info["vermagic"][0],
                "subsystem_hints": [label for label, terms in (
                    ("usb", ("usb", "mtu3", "xhci", "xsphy", "tcpc", "tcpci")),
                    ("wifi", ("wlan", "wifi", "80211")),
                    ("bluetooth", ("bluetooth", "btmtk", "bt_drv", "btif")),
                    ("connectivity", ("conninfra", "connfem", "connv3", "connectivity")),
                ) if any(term in (path + " " + info["name"][0]).lower() for term in terms)],
                "depends": [name for value in info.get("depends", [])
                            for name in value.split(",") if name],
                "aliases": info.get("alias", []),
                "firmware": info.get("firmware", []),
            })
    providers = {}
    for module in modules:
        providers.setdefault(module["name"].replace("-", "_"), []).append(
            module["source"] + ":" + module["path"])
    for module in modules:
        module["dependency_candidates"] = {
            name: providers.get(name.replace("-", "_"), [])
            for name in module["depends"]}
        checks = {}
        for name in module["firmware"]:
            path = PurePosixPath(name)
            if not name or path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe declared firmware path")
            found = []
            for root in firmware_roots:
                candidate = root / name
                if candidate.is_file() and candidate.resolve().is_relative_to(root.resolve()):
                    found.append(str(candidate))
            checks[name] = {"status": ("present" if found else "absent_in_supplied_roots")
                            if firmware_roots else "not_checked", "paths": found}
        module["firmware_checks"] = checks
    return {
        "status": "HOST METADATA ONLY; no module loading or hardware validation",
        "limits": [
            "Missing dependencies may be built into the kernel or in unexamined inputs.",
            "Firmware declarations may omit dynamically constructed/vendor userspace requests.",
            "Metadata does not establish DT/provider, Android HAL or kernel ABI compatibility.",
            "Subsystem hints use filenames/module names, not hardware identification.",
            "Only uncompressed .ko files are examined; EROFS must be extracted separately.",
        ],
        "inputs": [str(path) for path in inputs],
        "firmware_roots": [str(path) for path in firmware_roots],
        "module_count": len(modules),
        "vermagic_values": sorted({m["vermagic"] for m in modules}),
        "ambiguous_module_names": {k: v for k, v in sorted(providers.items()) if len(v) > 1},
        "modules": modules,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True,
                        help="Uncompressed newc CPIO or extracted module tree; repeatable")
    parser.add_argument("--firmware-root", type=Path, action="append", default=[],
                        help="Extracted firmware search root; repeatable")
    args = parser.parse_args()
    for root in args.firmware_root:
        if not root.is_dir():
            parser.error("Firmware root must be an existing directory")
    print(json.dumps(report(args.input, args.firmware_root), indent=2))


if __name__ == "__main__":
    main()

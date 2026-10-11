#!/usr/bin/env python3
"""Audit USB DT owners and provider references; metadata is not proof of probing."""
import argparse
import fnmatch
import json
from pathlib import Path
import struct

import inspect_dtb

USB_NAMES = {"usb0", "usb-phy0", "usb2-phy0", "usb3-phy0", "xhci0", "extcon-usb",
             "rt-pd-manager", "charger", "usb-boost-manager", "usb-meta",
             "typec-mux-switch", "usb-dp-selector"}
BUILTIN_HINTS = {"syscon": "CONFIG_MFD_SYSCON", "fixed-clock": "CONFIG_COMMON_CLK",
                 "regulator-fixed": "CONFIG_REGULATOR_FIXED_VOLTAGE",
                 "mediatek,mtk-xhci": "CONFIG_USB_XHCI_PLATFORM"}


def flatten(node):
    yield node
    for child in node["children"]:
        yield from flatten(child)


def strings(node, key):
    prop = node["properties"].get(key, {})
    if prop.get("type") == "string":
        return [prop["value"]]
    return prop.get("value", []) if prop.get("type") == "strings" else []


def cells(node, key):
    raw = bytes.fromhex(node["properties"][key]["hex"])
    if len(raw) % 4:
        raise ValueError("Non-cell reference: " + key)
    return list(struct.unpack(">" + "I" * (len(raw) // 4), raw))


def alias_matches(alias, node):
    name = node["name"].split("@")[0]
    dtype = next(iter(strings(node, "device_type")), "")
    compatibles = strings(node, "compatible")
    # OF tables match any compatible; modpost emits both Ccompatible and CcompatibleC* aliases.
    prefix = "of:N" + name + "T" + dtype
    return alias.startswith("of:") and any(
        fnmatch.fnmatchcase(prefix + "C" + c.replace(" ", "_"), alias) or
        fnmatch.fnmatchcase(prefix + "C" + c.replace(" ", "_") + "C", alias)
        for c in compatibles)


def owner(node, modules, config):
    matches = [{"module": m["name"], "aliases": [a for a in m["aliases"] if alias_matches(a, node)]}
               for m in modules if any(alias_matches(a, node) for a in m["aliases"])]
    if matches:
        return {"status": "module_candidate" if len(matches) == 1 else "ambiguous",
                "matches": matches}
    hints = sorted({BUILTIN_HINTS[c] for c in strings(node, "compatible")
                    if c in BUILTIN_HINTS and BUILTIN_HINTS[c] + "=y" in config.splitlines()})
    if hints:
        return {"status": "builtin_heuristic", "configs": hints}
    return {"status": "unresolved", "reason": "No matching OF alias or specific CONFIG=y hint"}


def references(node, nodes, handles, modules, config):
    result = {}
    specs = {"vusb33-supply": None, "clocks": "#clock-cells", "power-domains": "#power-domain-cells",
             "phys": "#phy-cells", "mediatek,syscon-wakeup": "remaining", "tcpc": None}
    for key, count_key in specs.items():
        if key not in node["properties"]:
            continue
        entries = []
        if key == "tcpc" and strings(node, key):
            name = strings(node, key)[0]
            paths = [p for p, n in nodes.items() if name in strings(n, "tcpc,name")]
            entries.append({"name": name, "status": "resolved" if len(paths) == 1 else "unresolved",
                            "paths": paths})
            if len(paths) == 1:
                entries[-1]["owner"] = owner(nodes[paths[0]], modules, config)
        else:
            values = cells(node, key)
            i = 0
            while i < len(values):
                handle = values[i]
                i += 1
                path = handles.get(str(handle))
                if path is None or path not in nodes:
                    entries.append({"phandle": handle, "status": "unresolved", "remaining_cells": values[i:]})
                    break
                provider = nodes[path]
                entry = {"phandle": handle, "path": path, "status": "resolved",
                         "owner": owner(provider, modules, config)}
                if count_key == "remaining":
                    count = len(values) - i
                    entry["argument_interpretation"] = "vendor binding unknown; first phandle only"
                elif count_key is None:
                    count = 0
                elif count_key not in provider["properties"]:
                    entry.update(status="unresolved", reason="Missing " + count_key)
                    entries.append(entry)
                    break
                else:
                    counts = cells(provider, count_key)
                    if len(counts) != 1:
                        raise ValueError("Invalid provider cell count")
                    count = counts[0]
                if i + count > len(values):
                    raise ValueError("Truncated provider arguments: " + key)
                entry["args"] = values[i:i + count]
                i += count
                entries.append(entry)
                if count_key is None and i != len(values):
                    raise ValueError("Expected single phandle: " + key)
        result[key] = entries
    return result


def audit(dump, inventory, config="", paths=None):
    nodes = {n["path"]: n for n in flatten(dump["tree"])}
    selected = paths if paths is not None else [p for p, n in nodes.items()
                                               if n["name"].split("@")[0] in USB_NAMES]
    rows = []
    for path in selected:
        if path not in nodes:
            rows.append({"path": path, "owner": {"status": "unresolved", "reason": "Node absent"}, "providers": {}})
            continue
        node = nodes[path]
        own = owner(node, inventory["modules"], config)
        # PHY child nodes are resources instantiated by their parent's driver, not OF devices.
        if own["status"] == "unresolved" and not strings(node, "compatible") and "#phy-cells" in node["properties"]:
            parent = nodes.get(path.rsplit("/", 1)[0])
            if parent:
                parent_owner = owner(parent, inventory["modules"], config)
                if parent_owner["status"] == "module_candidate":
                    own = {"status": "parent_provider_heuristic", "parent": parent["path"], "owner": parent_owner}
        rows.append({"path": path, "compatible": strings(node, "compatible"),
                     "dt_status": strings(node, "status"), "owner": own,
                     "providers": references(node, nodes, dump["phandles"], inventory["modules"], config)})
    unresolved = [r["path"] for r in rows if r["owner"]["status"] in ("unresolved", "ambiguous")]
    return {"status": "OFFLINE CANDIDATES ONLY; heuristics do not establish driver binding",
            "node_count": len(rows), "covered_count": len(rows) - len(unresolved),
            "unresolved": unresolved, "nodes": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dtb", type=Path)
    source.add_argument("--dump", type=Path)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--node", action="append", help="Explicit audit path; repeatable (default: USB path nodes)")
    args = parser.parse_args()
    dump = inspect_dtb.inspect(args.dtb.read_bytes()) if args.dtb else json.loads(args.dump.read_text())
    print(json.dumps(audit(dump, json.loads(args.inventory.read_text()),
                           args.config.read_text() if args.config else "", args.node), indent=2))


if __name__ == "__main__":
    main()

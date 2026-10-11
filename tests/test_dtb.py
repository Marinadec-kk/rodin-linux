import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import inspect_dtb
import check_dt_modules


def u32(*values):
    return struct.pack(">" + "I" * len(values), *values)


def fdt(node, nop=False):
    names = bytearray()
    block = bytearray()

    def padded(raw):
        return raw + b"\0" * (-len(raw) % 4)

    def emit(n):
        name, props, children = n
        block.extend(u32(1) + padded(name.encode() + b"\0"))
        if nop:
            block.extend(u32(4))
        for key, raw in props.items():
            offset = len(names)
            names.extend(key.encode() + b"\0")
            block.extend(u32(3, len(raw), offset) + padded(raw))
        for child in children:
            emit(child)
        block.extend(u32(2))

    emit(node)
    block.extend(u32(9))
    return u32(0xd00dfeed, 56 + len(block) + len(names), 56, 56 + len(block),
               40, 17, 16, 0, len(names), len(block)) + bytes(16) + block + names


class FdtTests(unittest.TestCase):
    def fixture(self):
        return fdt(("", {"model": b"fixture\0", "empty": b"", "raw": b"\xff\x01\x02",
                         "numbers": u32(0, 123)}, [
            ("bus", {}, [("device@4", {"compatible": b"test,one\0test,two\0", "phandle": u32(3)}, [])])]), nop=True)

    def test_nested_types_and_indexes(self):
        result = inspect_dtb.inspect(self.fixture())
        props = result["tree"]["properties"]
        self.assertEqual(props["model"]["type"], "string")
        self.assertEqual(props["numbers"]["value"], [0, 123])
        self.assertEqual(props["raw"]["type"], "bytes")
        self.assertEqual(props["empty"]["value"], [])
        self.assertEqual(result["compatible_index"]["test,two"], ["/bus/device@4"])
        self.assertEqual(result["phandles"], {"3": "/bus/device@4"})

    def test_wrapper(self):
        wrapper = inspect_dtb.MTK_MAGIC + bytes(60)
        result = inspect_dtb.inspect(wrapper + self.fixture())
        self.assertEqual(result["wrapper_hex"], wrapper.hex())
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(wrapper[:32])

    def test_every_truncation(self):
        data = self.fixture()
        for length in range(len(data)):
            with self.subTest(length=length), self.assertRaises(ValueError):
                inspect_dtb.inspect(data[:length])

    def test_header_bounds_version_overlap_and_trailing(self):
        data = self.fixture()
        for offset, value in [(0, 0), (4, len(data) + 1), (8, len(data)),
                              (12, len(data)), (16, len(data)), (20, 16), (24, 18),
                              (32, len(data)), (36, len(data)), (12, 56), (8, 57)]:
            bad = bytearray(data)
            struct.pack_into(">I", bad, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                inspect_dtb.inspect(bad)
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(data + data)

    def test_bad_tokens_properties_and_balance(self):
        data = fdt(("", {"item": b"x\0"}, []))
        for offset, value in [(56, 7), (64, 2), (68, 0xffffffff), (72, 999), (84, 4)]:
            bad = bytearray(data)
            struct.pack_into(">I", bad, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                inspect_dtb.inspect(bad)
        for props in [{"phandle": u32(0)}, {"phandle": b"x"},
                      {"phandle": u32(1), "linux,phandle": u32(2)}]:
            with self.assertRaises(ValueError):
                inspect_dtb.inspect(fdt(("", props, [])))
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(fdt(("", {}, [("a", {"phandle": u32(1)}, []),
                                             ("b", {"phandle": u32(1)}, [])])))

    def test_unterminated_blocks_and_late_property(self):
        data = bytearray(fdt(("", {"item": b"x\\0"}, [])))
        data[40:56] = b"\\xff" * 16
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(data)
        data = bytearray(fdt(("", {}, [])))
        data[60:64] = b"abcd"
        struct.pack_into(">I", data, 64, 1)
        struct.pack_into(">I", data, 68, 1)
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(data)
        data = bytearray(fdt(("", {"item": b"x\\0"}, [])))
        data[-1] = 65
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(data)
        data = bytearray(fdt(("", {}, [("child", {}, [])])))
        # Turn the parent's END_NODE into a property after an already closed child.
        struct.pack_into(">I", data, len(data) - 8, 3)
        with self.assertRaises(ValueError):
            inspect_dtb.inspect(data)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.dtb"
            path.write_bytes(self.fixture())
            output = subprocess.check_output([sys.executable, "tools/inspect_dtb.py", "--dtb", str(path)])
            self.assertEqual(json.loads(output)["phandles"], {"3": "/bus/device@4"})


class AuditTests(unittest.TestCase):
    def fixture(self):
        dump = inspect_dtb.inspect(fdt(("", {}, [
            ("device@4", {"compatible": b"test,specific\0test,generic\0", "device_type": b"thing\0",
                          "clocks": u32(7, 99, 7, 100), "phys": u32(8, 1),
                          "vusb33-supply": u32(7), "power-domains": u32(7, 4),
                          "mediatek,syscon-wakeup": u32(7, 24, 2), "tcpc": b"port-test\0"}, []),
            ("provider", {"compatible": b"test,provider\0", "phandle": u32(7),
                          "#clock-cells": u32(1), "#power-domain-cells": u32(1)}, [
                ("resource", {"phandle": u32(8), "#phy-cells": u32(1)}, [])]),
            ("port", {"compatible": b"test,port\0", "tcpc,name": b"port-test\0"}, []),
            ("builtin", {"compatible": b"syscon\0"}, []), ("orphan", {}, [])])))
        inventory = {"modules": [
            {"name": "driver", "aliases": ["of:NdeviceTthingCtest,specificC*", "of:N*T*Ctest,generic"]},
            {"name": "provider_driver", "aliases": ["of:N*T*Ctest,provider"]}]}
        return dump, inventory

    def test_matching_and_providers(self):
        dump, inventory = self.fixture()
        result = check_dt_modules.audit(dump, inventory, "CONFIG_MFD_SYSCON=y\n",
                                        ["/device@4", "/provider/resource", "/builtin", "/orphan"])
        self.assertEqual(result["covered_count"], 3)
        self.assertEqual(result["unresolved"], ["/orphan"])
        row = result["nodes"][0]
        self.assertEqual(row["owner"]["matches"][0]["module"], "driver")
        self.assertEqual(row["providers"]["clocks"][1]["args"], [100])
        self.assertEqual(row["providers"]["tcpc"][0]["paths"], ["/port"])
        self.assertEqual(row["providers"]["phys"][0]["path"], "/provider/resource")
        self.assertEqual(result["nodes"][1]["owner"]["status"], "parent_provider_heuristic")
        self.assertEqual(result["nodes"][2]["owner"]["status"], "builtin_heuristic")

    def test_names_types_and_compatible_order(self):
        dump, _ = self.fixture()
        node = dump["tree"]["children"][0]
        for alias in ["of:NwrongT*Ctest,specificC*", "of:N*TwrongCtest,specificC*",
                      "of:N*T*Ctest,absentC*"]:
            self.assertFalse(check_dt_modules.alias_matches(alias, node), alias)
        self.assertTrue(check_dt_modules.alias_matches("of:N*T*Ctest,genericC*", node))
        self.assertTrue(check_dt_modules.alias_matches("of:N*T*Ctest,specific", node))

    def test_missing_config_ambiguous_and_absent_node(self):
        dump, inventory = self.fixture()
        inventory["modules"].append({"name": "second", "aliases": ["of:N*T*Ctest,genericC*"]})
        result = check_dt_modules.audit(dump, inventory, "CONFIG_MFD_SYSCON=m", ["/device@4", "/builtin", "/absent"])
        self.assertEqual(len(result["unresolved"]), 3)
        self.assertEqual(result["nodes"][0]["owner"]["status"], "ambiguous")

    def test_missing_and_truncated_provider(self):
        dump, inventory = self.fixture()
        node = dump["tree"]["children"][0]
        node["properties"]["clocks"] = inspect_dtb.property_value(u32(999, 12))
        result = check_dt_modules.audit(dump, inventory, paths=["/device@4"])
        self.assertEqual(result["nodes"][0]["providers"]["clocks"][0]["status"], "unresolved")
        node["properties"]["clocks"] = inspect_dtb.property_value(u32(7))
        with self.assertRaises(ValueError):
            check_dt_modules.audit(dump, inventory, paths=["/device@4"])

    def test_tcpc_phandle_and_missing_cell_count(self):
        dump, inventory = self.fixture()
        node = dump["tree"]["children"][0]
        node["properties"]["tcpc"] = inspect_dtb.property_value(u32(7))
        del dump["tree"]["children"][1]["properties"]["#clock-cells"]
        row = check_dt_modules.audit(dump, inventory, paths=["/device@4"])["nodes"][0]
        self.assertEqual(row["providers"]["tcpc"][0]["path"], "/provider")
        self.assertEqual(row["providers"]["clocks"][0]["status"], "unresolved")

    def test_cli_raw_and_dump(self):
        dump, inventory = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dump.json").write_text(json.dumps(dump))
            (root / "inventory.json").write_text(json.dumps(inventory))
            (root / "raw.dtb").write_bytes(fdt(("", {}, [("usb0", {"compatible": b"test,generic\0"}, [])])))
            for flag, name in [("--dump", "dump.json"), ("--dtb", "raw.dtb")]:
                output = subprocess.check_output([sys.executable, "tools/check_dt_modules.py", flag,
                                                  str(root / name), "--inventory", str(root / "inventory.json")])
                self.assertIn("unresolved", json.loads(output))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Build an UNBOOTED init_boot USB-ACM candidate on the host; never invoke adb/fastboot."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

import build_initramfs as ramfs
import inspect_boot
import inspect_ramdisk
import inspect_vendor_boot

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_HASHES = {
    "boot.img": "a9be0c0c90983989bc6e64de0c0007249edfac668c271e626821e5613244cbaa",
    "init_boot.img": "808c76f0ba3694394ce1623a495fe628bd0c4dc143fe88aeaa49e60c644e394a",
    "vendor_boot.img": "261dc46ceb453a9384208613aee34623dad3f96b95385f8366db71f38c1ba877",
}
AVB_SALT = "e20fd79092c8453cd3bce9234dd6d36a60f0a0a0cd0b9a224a3101505b1cc2fa"
# Compatibility metadata inherited from the pinned original, not a claim of Android userspace.
AVB_PROPERTIES = {
    "com.android.build.init_boot.os_version": "16",
    "com.android.build.init_boot.fingerprint": "Xiaomi/lineage_rodin/rodin:16/BP4A.251205.006/eng.androi:userdebug/release-keys",
    "com.android.build.init_boot.security_patch": "2026-08-01",
    "org.rodin.experiment": "host-only-usb-acm-003",
}
SEEDS = {
    "nvmem_mtk-devinfo.ko", "tinysys-scmi.ko", "mtk-scpsys.ko", "mtk-scpsys-mt6899.ko",
    "mtk-pmic-wrap.ko", "mtk-spmi-pmic.ko", "spmi-mtk-pmif.ko", "i2c-mt65xx.ko",
    "mt6681-core.ko", "mt6685-core.ko", "phy-mtk-xsphy.ko", "xhci-mtk-hcd-v2.ko",
    "mtu3.ko", "mt6375.ko", "tcpc_class.ko", "tcpc_mt6375.ko", "rt_pd_manager.ko",
    "extcon-mtk-usb.ko", "tcpci_late_sync.ko", "mux_switch.ko", "ps5170.ko",
    "usb_dp_selector.ko",
}
MODULE_PATH = re.compile(r"/lib/modules/[A-Za-z0-9_-]+\.ko\Z")
EXCLUDED_MODULES = {"mtk_wdt.ko", "bootmonitor.ko", "monitor_hang.ko", "aee_hangdet.ko",
                    "mtk-mmc.ko", "ufs-mediatek-mod.ko", "crash_module.ko",
                    "block2mtd.ko", "mtdoops.ko"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def module_plan(load_text, dep_text, available):
    """Declared symbol dependency closure, not a proof of DT/probe completeness."""
    loads = load_text.splitlines()
    if len(set(loads)) != len(loads):
        # The pinned load list repeats mtk_dramc; stable deduplication is intentional.
        loads = list(dict.fromkeys(loads))
    for name in loads:
        if not MODULE_PATH.fullmatch("/lib/modules/" + name):
            raise ValueError("Unsafe module load entry")
    selected = {n for n in loads if n in SEEDS or n.startswith("clk-mt6899")
                or n == "clk-common.ko" or n.startswith("pinctrl-mtk-v2")
                or n == "pinctrl-mt6899.ko" or n.endswith("-regulator.ko")}
    if not SEEDS <= selected:
        raise ValueError("Missing mandatory module seed")
    deps = {}
    for line in dep_text.splitlines():
        key, separator, values = line.partition(":")
        children = values.split()
        if not separator or key in deps or not all(MODULE_PATH.fullmatch(x) for x in [key, *children]):
            raise ValueError("Malformed/unsafe module dependencies")
        deps[key] = children
    order, active, done = [], set(), set()

    def visit(path):
        if path in done:
            return
        if path in active:
            raise ValueError("Module dependency cycle")
        if Path(path).name in EXCLUDED_MODULES:
            raise ValueError("Provisional USB plan must not pull storage/watchdog/crash drivers")
        if path not in deps or path.lstrip("/") not in available:
            raise ValueError("Missing module or dependency metadata")
        active.add(path)
        for child in deps[path]:
            visit(child)
        active.remove(path)
        done.add(path)
        order.append(path)

    for name in loads:
        if name in selected:
            visit("/lib/modules/" + name)
    return order


def unsigned_image(original, compressed):
    layout = inspect_boot.inspect(original)
    if (layout["header_version"] != 4 or layout["kernel"]["size"] != 0
            or layout["signature_size"] != 0 or layout["page_size"] != 4096):
        raise ValueError("Expected original init_boot v4 without a kernel/GKI signature")
    if not compressed or len(compressed) > len(original) - 128 * 1024:
        raise ValueError("Ramdisk does not fit partition")
    # Preserve the complete header page, including OS version, reserved fields and cmdline.
    header = bytearray(original[:4096])
    struct.pack_into("<I", header, 12, len(compressed))
    image = bytes(header) + compressed
    return image + b"\0" * (-len(image) % 4096)


def decompress_lz4(data, lz4):
    # Inputs are hash-pinned before reaching this function; bound accepted output size.
    with tempfile.TemporaryFile() as out:
        subprocess.run([lz4, "-dc"], input=data, stdout=out, check=True)
        if out.tell() > 128 * 1024 * 1024:
            raise ValueError("LZ4 expansion too large")
        out.seek(0)
        return out.read()


def kernel_config(kernel):
    start = kernel.find(b"IKCFG_ST")
    end = kernel.find(b"IKCFG_ED", start + 8)
    if start < 0 or end <= start:
        raise ValueError("Expected embedded OTA kernel config")
    config = gzip.decompress(kernel[start + 8:end])
    options = set(config.decode().splitlines())
    required = {"CONFIG_BLK_DEV_INITRD=y", "CONFIG_RD_GZIP=y", "CONFIG_RD_LZ4=y", "CONFIG_BINFMT_SCRIPT=y",
                "CONFIG_BINFMT_ELF=y", "CONFIG_MODULES=y", "CONFIG_MODVERSIONS=y",
                "CONFIG_USB_GADGET=y", "CONFIG_USB_CONFIGFS=y", "CONFIG_USB_CONFIGFS_ACM=y",
                "CONFIG_USB_F_ACM=y", "CONFIG_USB_LIBCOMPOSITE=y", "CONFIG_CONFIGFS_FS=y",
                "CONFIG_TTY=y", "CONFIG_UNIX98_PTYS=y", "CONFIG_PROC_FS=y", "CONFIG_SYSFS=y",
                "CONFIG_TMPFS=y"}
    if not required <= options:
        raise ValueError("OTA kernel lacks candidate prerequisites: " + str(required - options))
    return config


def platform_members(vendor, lz4):
    layout = inspect_vendor_boot.inspect(vendor)
    fragments = [x for x in layout["entries"] if x.get("type") == 1]
    if len(fragments) != 1:
        raise ValueError("Expected exactly one PLATFORM fragment")
    fragment = fragments[0]
    start = layout["ramdisk_offset"] + fragment["offset_in_ramdisk"]
    data = vendor[start:start + fragment["size"]]
    archive = decompress_lz4(data, lz4)
    files = {}
    for name, mode, value in inspect_ramdisk.members(archive):
        if name.startswith("lib/modules/") and stat.S_ISREG(mode):
            if name in files:
                raise ValueError("Ambiguous platform module member")
            files[name] = value
    return files


def publish(output, name, data):
    # Atomic replacement does not follow an existing output-file symlink/hardlink.
    with tempfile.NamedTemporaryFile(dir=output, delete=False) as file:
        path = Path(file.name)
        try:
            file.write(data)
            file.close()
            os.replace(path, output / name)
        finally:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ota-dir", type=Path, default=ROOT / "build" / "ota")
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "candidate")
    parser.add_argument("--verify-only", action="store_true",
                        help="Rebuild in temporary storage and compare every existing artifact; do not replace outputs")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "build").resolve()):
        raise ValueError("Candidate outputs must stay in the ignored project build directory")
    originals = {}
    for name, expected in ORIGINAL_HASHES.items():
        path = (args.ota_dir / name).resolve()
        if output == path.parent or output in path.parents:
            raise ValueError("Output must not contain original restore artifacts")
        originals[name] = path.read_bytes()
        if digest(originals[name]) != expected:
            raise ValueError(f"Original {name} hash mismatch")
    avbtool, lz4 = shutil.which("avbtool"), shutil.which("lz4")
    if not avbtool or not lz4:
        raise ValueError("Host avbtool and lz4 are required")
    version = subprocess.check_output([avbtool, "version"], text=True).strip()
    if version != "avbtool 1.4.0":
        raise ValueError("Expected tested host avbtool 1.4.0 for deterministic output")
    boot_kernel = inspect_boot.inspect(originals["boot.img"])["kernel"]
    start, size = boot_kernel["offset"], boot_kernel["size"]
    config = kernel_config(decompress_lz4(originals["boot.img"][start:start + size], lz4))
    files = platform_members(originals["vendor_boot.img"], lz4)
    order = module_plan(files["lib/modules/modules.load"].decode(),
                        files["lib/modules/modules.dep"].decode(), files)
    plan, checksums = [], []
    for path in order:
        data = files[path.lstrip("/")]
        names = re.findall(rb"\x00name=([A-Za-z0-9_]+)\x00", data)
        if len(names) != 1:
            raise ValueError(f"Missing/ambiguous module name: {path}")
        plan.append(f"{path} {names[0].decode()}\n")
        checksums.append(f"{digest(data)}  {path}\n")
    extra = [(n, stat.S_IFDIR | 0o755, b"", 0, 0) for n in ("etc", "etc/rodin")]
    for name, text in (("modules.load", "".join(plan)), ("modules.sha256", "".join(checksums))):
        extra.append(("etc/rodin/" + name, stat.S_IFREG | 0o600, text.encode(), 0, 0))
    init = (ROOT / "initramfs" / "usb-init").read_bytes()
    binary = ramfs.read_busybox(args.apk)
    cpio, compressed = ramfs.build(binary, init, extra)
    raw = unsigned_image(originals["init_boot.img"], compressed)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as work:
        # avbtool resolves hash-descriptor targets by partition name, not arbitrary basename.
        image = Path(work) / "init_boot.img"
        image.write_bytes(raw)
        command = [avbtool, "add_hash_footer", "--image", str(image), "--partition_size",
                   str(len(originals["init_boot.img"])), "--partition_name", "init_boot",
                   "--algorithm", "NONE", "--hash_algorithm", "sha256", "--salt", AVB_SALT]
        for name, value in AVB_PROPERTIES.items():
            command += ["--prop", name + ":" + value]
        subprocess.run(command, check=True)
        subprocess.run([avbtool, "verify_image", "--image", str(image)], check=True)
        info = subprocess.check_output([avbtool, "info_image", "--image", str(image)])
        candidate = image.read_bytes()
    layout = inspect_boot.inspect(candidate)
    if len(candidate) != len(originals["init_boot.img"]) or layout["ramdisk"]["sha256"] != digest(compressed):
        raise ValueError("Candidate size/ramdisk round-trip check failed")
    manifest = {
        "status": "HOST-ONLY UNBOOTED CANDIDATE; not approved for flashing",
        "experiment": "003-usb-acm", "target_partition": "init_boot (slot not selected)",
        "candidate_sha256": digest(candidate), "candidate_size": len(candidate),
        "original_restore_sha256": ORIGINAL_HASHES, "kernel_and_vendor_boot_modified": False,
        "busybox_apk_sha256": ramfs.APK_SHA256, "usb_init_sha256": digest(init),
        "ramdisk_gzip_sha256": digest(compressed), "ramdisk_gzip_size": len(compressed),
        "avbtool_version": version, "avbtool_file_sha256": digest(Path(avbtool).read_bytes()),
        "avb_algorithm": "NONE (same as original init_boot)", "avb_chain_acceptance": "unproven",
        "ota_kernel_config_sha256": digest(config),
        "module_count": len(order), "module_plan_sha256": digest("".join(plan).encode()),
        "module_strategy": "provisional provider/USB seeds plus declared dependency closure",
        "runtime_validation": "not boot-tested; no USB controller or module initialization demonstrated",
        "layout": layout,
    }
    artifacts = {"init_boot-candidate.img": candidate, "initramfs.cpio": cpio,
                 "initramfs.cpio.gz": compressed, "avb-info.txt": info,
                 "modules.load": "".join(plan).encode(), "modules.sha256": "".join(checksums).encode(),
                 "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode()}
    for name, data in artifacts.items():
        if args.verify_only:
            if (output / name).read_bytes() != data:
                raise ValueError(f"Existing candidate artifact differs from reproducible build: {name}")
        else:
            publish(output, name, data)
    if args.verify_only:
        print("PASS: all candidate artifacts match a fresh build; AVB self-hash verified (NOT boot-chain acceptance)")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

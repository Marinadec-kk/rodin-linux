# Host-side bring-up experiment 001

**Status: built and inspected on the PC only. No Linux boot test, no USB console test, and no experimental device writes.**

## Source map

Repository names can be misleading: the crDroid `android_kernel_xiaomi_rodin` repository contains prebuilt artifacts rather than the complete kernel source.

| Repository / branch | Observed revision | Finding |
| --- | --- | --- |
| [MiCode/Xiaomi_Kernel_OpenSource, bsp-rodin-v-oss](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/tree/bsp-rodin-v-oss) | `2cd69ee4193920840ee3c837384716a66b1faecd` | Kernel source tree; top-level Makefile reports **6.6.30**, SPDX GPL-2.0. Not the exact running 6.6.89 build. |
| [crdroidandroid/android_kernel_xiaomi_rodin, 16.0](https://github.com/crdroidandroid/android_kernel_xiaomi_rodin/tree/16.0) | `7f68c236fed56faeccc666ee2c93707f409952ed` | `Image.lz4`, DTB/DTBO, headers, load lists and prebuilt modules. |
| [mt6899-devs/android_kernel_device_modules_6.6, bsp-rodin-v-oss](https://github.com/mt6899-devs/android_kernel_device_modules_6.6/tree/bsp-rodin-v-oss) | `ea65723c452f28681ee011499f0ebf452b1f0620` | Device-module source tree with MT6899 directories and build configuration. Completeness, licensing and exact ABI match remain to be audited. |
| [mt6899-devs/android_vendor_mediatek_kernel_modules, bsp-rodin-v-oss](https://github.com/mt6899-devs/android_vendor_mediatek_kernel_modules/tree/bsp-rodin-v-oss) | `26e64cd4c795b2ff2c07d530b956beabfdada2f1` | Additional vendor-module source candidate. No build/reproducibility claim yet. |

These revisions record the research snapshot, not a verified working build combination. Do not substitute this source kernel for the phone's running GKI without resolving module ABI compatibility.

## Recovery image inspection

Input is the official recovery image previously obtained through the crDroid rodin download page:

- Source: <https://sourceforge.net/projects/crdroid/files/rodin/12.x/recovery/vendor_boot.img/download>
- SHA-256: `0a6c50cd85a425f5b174a66dceb294fc935000f9f64642337a090233f0373d4e`
- Size: 67,108,864 bytes
- Android vendor_boot header v4, page size 4096, header size 2128
- Vendor ramdisk: 34,887,764 bytes starting at offset 4096
- DTB section: 444,841 bytes starting at offset 34,893,824
- Ramdisk table: two entries, each 108 bytes
- Bootconfig section: 149 bytes (contents intentionally not published)

| Fragment | Type | Size | SHA-256 |
| --- | --- | --- | --- |
| unnamed platform fragment | 1 / PLATFORM | 15,341,278 | `44713e36fb3dc9ec6d50c71570a43be1fafe1260cd5cc090f565e670983bd0b3` |
| recovery fragment | 2 / RECOVERY | 19,546,486 | `e50e0f7507276fd091dcb6f63a05ad6f3971021505b51db361abb7a218a22ab6` |

Both fragments use legacy LZ4 magic `02214c18`; both decompressed and their cpio directory listings were read on the host.

The PLATFORM fragment contains `lib/modules/modules.dep`, `modules.load`, `modules.load.recovery`, `first_stage_ramdisk/system/etc/fstab.mt6899` and drivers including `pinctrl-mt6899.ko`, `clk-mt6899.ko`, `ufs-mediatek-mod.ko`, `phy-mtk-ufs.ko`, regulator drivers and USB-related modules. The RECOVERY fragment contains Android init, recovery and ADB binaries.

**Implication:** the standalone research initramfs below does not replace or reproduce platform-driver initialization. A usable USB/storage path may require vendor ramdisk modules, their dependency order, DT/bootconfig preservation and the exact matching kernel. Boot, init_boot and vendor_boot composition still needs analysis. This project does not yet repack any of them.

Run the read-only inspector:

```bash
python3 tools/inspect_vendor_boot.py /path/to/vendor_boot.img
```

It reports structural metadata and hashes, not command-line/bootconfig contents, and rejects truncated/out-of-bounds inputs.

## Minimal ARM64 initramfs

The archive contains:

- Static ARM64 BusyBox (static PIE, no PT_INTERP and no DT_NEEDED libraries).
- A research `/init` which mounts proc/sysfs/devpts/tmpfs, reports reaching PID 1 and tries a console shell.
- Explicit `/dev/console`, `/dev/null`, `/dev/tty` and `/dev/kmsg` cpio nodes: the current GKI has DEVTMPFS disabled.
- No automatic storage mounts, firmware loads, vendor module loads or USB gadget setup.

**No observable console on rodin is demonstrated.** An image which reaches PID 1 but has no usable console is not a successful port. No screenshot or feature-matrix claim should imply otherwise.

### Reproduce

Requirements: Python 3.10+ and curl. No root, cross compiler, phone, or cpio executable is needed to build. Do not execute the ARM64 binary on an x86 host.

```bash
mkdir -p build/input
curl --fail --location --output build/input/busybox-static-1.37.0-r30.apk \
  https://dl-cdn.alpinelinux.org/alpine/v3.23/main/aarch64/busybox-static-1.37.0-r30.apk
python3 tools/build_initramfs.py --apk build/input/busybox-static-1.37.0-r30.apk
python3 -m unittest discover -s tests -v
sh -n initramfs/init
```

The builder refuses APK bytes other than the pinned SHA-256 `44c9abdfb970f398fa72c8382fe2d8808eea16beaf82daf6ac708b92f1b8659e`. This digest pins the APK obtained from Alpine's HTTPS mirror during research; it is **not** a claim that Alpine's APK signature was independently verified. The Alpine package declares BusyBox as GPL-2.0-only. Its binary is not committed or published here; any later redistribution needs corresponding-source/license compliance.

Outputs under ignored `build/initramfs/`:

- `initramfs.cpio` — 1,119,232 bytes
- `initramfs.cpio.gz` — 653,654 bytes
- `manifest.json` — provenance and input/output hashes

Reference gzip SHA-256 for this revision's init: `a10b4bbab0cd41f5d810c47e848acf89e2401606373349570cffef8cfb3f0ead`. Exact compressed bytes can vary across Python/zlib versions; record the environment when comparing across machines. Two independent output builds on this host were byte-identical.

### Verification performed

- 12 unit tests pass: newc structure/modes/device nodes, deterministic build, unsafe path rejection, AArch64 ELF and static-PIE handling, dependency/interpreter rejection, pinned-input rejection, vendor_boot v3/v4 parsing and malformed-image rejection.
- Shell syntax checked with `sh -n`.
- GNU cpio independently listed the built archive, including character devices and executable init.
- `readelf` confirmed AArch64 ELF64 with no interpreter; static PIE's dynamic table has no shared-library dependencies.
- No ARM runtime test: qemu-aarch64 and qemu-system-aarch64 are not installed on this host.

## Next gates before device experiments

1. Establish matching restore artifacts and an actual recovery path, not just a theoretical A/B rollback.
2. Inspect boot/init_boot and kernel-module ABI/load order on the host.
3. Choose an observable first-boot path (USB gadget, usable framebuffer or another verified console).
4. Establish whether temporary boot is supported; do not assume `fastboot boot` works on rodin.
5. Only then design an explicit experimental boot procedure. Do not flash the cpio archive: **it is not an Android boot image.**

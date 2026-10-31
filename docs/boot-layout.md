# Host-side experiment 002: boot composition and USB prerequisites

**Status: read-only host extraction and Android observation. No image was modified or flashed; no non-Android boot is claimed.**

## Extracting the full OTA

Input: `crDroidAndroid-16.0-20260925-rodin-v12.12.zip`, pinned SHA-256 `51a24fb889c286c27df892fcbee1f9977f9d32a76bbd1316900f3e90aa8a52e8`.

`tools/extract_boot_payload.py` reads the stored payload directly from the ZIP, without unpacking the entire ROM. It implements a deliberately limited reader for Android payload major v2 / minor 0, using the wire field numbers from [update_engine's update_metadata.proto](https://github.com/LineageOS/android_system_update_engine/blob/lineage-23.0/update_metadata.proto). The actual manifest uses 4096-byte blocks. This tool is not an OTA updater and never talks to a device.

Supported operations: full REPLACE (0), REPLACE_BZ (1), REPLACE_XZ (8), and ZERO (6). Delta/source operations and DISCARD are rejected. The input ROM is hash-pinned; operation blobs and complete extracted partitions are checked against manifest SHA-256 values. Missing/overlapping destination blocks are rejected. Payload/AVB signatures are not cryptographically verified by these tools.

```bash
python3 tools/extract_boot_payload.py \
  ../downloads/crDroidAndroid-16.0-20260925-rodin-v12.12.zip \
  --partitions boot init_boot vendor_boot system_dlkm vendor_dlkm
```

Images and `extraction.json` are written to ignored `build/ota/`. No extracted images are committed.

| Partition | Bytes | SHA-256 matching OTA manifest |
| --- | ---: | --- |
| boot | 67,108,864 | `a9be0c0c90983989bc6e64de0c0007249edfac668c271e626821e5613244cbaa` |
| init_boot | 8,388,608 | `808c76f0ba3694394ce1623a495fe628bd0c4dc143fe88aeaa49e60c644e394a` |
| vendor_boot | 67,108,864 | `261dc46ceb453a9384208613aee34623dad3f96b95385f8366db71f38c1ba877` |
| system_dlkm | 6,959,104 | `5bc4770aed61c4e9baf69c96b3fc0546debb1863856b0f57c5727388e996563a` |
| vendor_dlkm | 16,424,960 | `14f4b90ff83adb283397e04fe740e30ce951a53368166c306a00f8a91d520179` |

`file` identifies the dlkm images as EROFS. Their complete contents have not been extracted yet; no host mount was performed.

## Where the kernel and init live

Both boot and init_boot have Android boot header v4, header size 1584, fixed page size 4096.

| Image | Kernel bytes | Generic ramdisk bytes | Finding |
| --- | ---: | ---: | --- |
| boot | 17,184,688 | 0 | Contains the legacy-LZ4 kernel, no generic ramdisk. |
| init_boot | 0 | 2,908,123 | Contains the legacy-LZ4 generic ramdisk and Android `/init`. |

The kernel's release string is `6.6.89-android15-8-g8e4be6b47e40-ab14134548-4k`, matching the running Android observation. This compares a release string, not the full live partition bytes or module ABI.

The generic ramdisk's concatenated newc archives contain `/init`, explicit device nodes, Android first-stage directories, `system/bin/modprobe` and snapuserd-related files. The `/init` entry is Android's init, not this project's shell script.

The OTA vendor_boot has a 15,345,994-byte PLATFORM ramdisk and a 19,568,656-byte RECOVERY ramdisk. **It differs from the separately downloaded recovery image inspected in experiment 001.** Keep artifacts from the same build together; do not assume the standalone recovery and OTA platform fragment are interchangeable.

The PLATFORM fragment contains MediaTek drivers and module metadata. Recovery contains another Android init/recovery userspace. Generic and vendor ramdisk selection/composition must be preserved and understood before replacing `/init`; merely adding a shell script to an arbitrary image is not a verified boot strategy.

Inspect without modifications:

```bash
python3 tools/inspect_boot.py build/ota/boot.img
python3 tools/inspect_boot.py build/ota/init_boot.img
python3 tools/inspect_vendor_boot.py build/ota/vendor_boot.img
```

### AVB is still present

Both boot header v4 `signature_size` fields are zero, but **all three OTA images have AVB footers and embedded vbmeta**. A zero GKI boot-signature size does not mean no verified-boot metadata exists.

| Image | AVB original image bytes | Embedded vbmeta offset | vbmeta bytes |
| --- | ---: | ---: | ---: |
| boot | 17,190,912 | 17,190,912 | 2,432 |
| init_boot | 2,912,256 | 2,912,256 | 832 |
| vendor_boot | 35,377,152 | 35,377,152 | 640 |

Footer version observed: 1.0. These are structural observations, not signature verification or proof a modified image will be accepted. No AVB disable/lock/re-sign operation was attempted.

## Ramdisk analysis tool

`tools/inspect_ramdisk.py` lists uncompressed newc archives, including concatenated archives. It can copy one uniquely named regular member to a user-selected host output; it does not extract an archive tree, create device nodes, follow symlinks, or write to a device. Ambiguous duplicate files, unsafe paths, malformed archives and symlink/device-node selection are rejected.

Decompress sections on the host with `lz4 -dc`, then:

```bash
python3 tools/inspect_ramdisk.py build/inspection/ota-platform.cpio
python3 tools/inspect_ramdisk.py build/inspection/ota-platform.cpio \
  --member lib/modules/modules.dep --output build/inspection/modules.dep
```

The uncompressed fragment is not produced automatically by the OTA extractor: derive its offset and size with the vendor_boot inspector and copy that section on the host before decompression. Generated binary artifacts stay under `build/`.

## USB: promising path, not a demonstrated console

Additional observations from the running kernel config:

```text
CONFIG_USB_CONFIGFS_ACM=y
CONFIG_USB_F_ACM=y
CONFIG_USB_U_SERIAL=y
# CONFIG_USB_G_SERIAL is not set
CONFIG_USB_CONFIGFS_ECM=y
CONFIG_USB_LIBCOMPOSITE=y
CONFIG_CONFIGFS_FS=y
CONFIG_TTY=y
CONFIG_UNIX98_PTYS=y
```

This makes **configfs ACM (USB serial)** worth investigating before a full network/SSH setup. It does not imply a preconfigured serial gadget. Legacy g_serial is disabled. ECM remains an alternative.

Running Android exposes controller property `sys.usb.controller=11201000.usb0`. The ADB shell cannot list `/sys/class/udc` or `/config/usb_gadget` due to permission denial. An empty/suppressed listing must not be reported as absence of a controller. No USB properties or sysfs/configfs nodes were changed during research.

Names visible in `/proc/modules` include `phy_mtk_xsphy`, `mtu3`, `tcpc_class`, `tcpc_mt6375`, `extcon_mtk_usb`, and `tcpci_late_sync`. Android's USB init rc uses configfs under `/config/usb_gadget/g1`, creates ACM functions, and binds the gadget via a controller property. Android's actual ADB connection uses FunctionFS/Android userspace; the new BusyBox initramfs does not reproduce that.

### Module load-order evidence

From the OTA PLATFORM fragment's `modules.load` and `modules.dep`:

| Module | Zero-based load-list index | Declared dependency closure |
| --- | ---: | ---: |
| phy-mtk-xsphy.ko | 117 | 0 |
| mtu3.ko | 119 | 0 |
| tcpc_class.ko | 158 | 1 |
| tcpc_mt6375.ko | 159 | 2 |
| extcon-mtk-usb.ko | 183 | 48 |
| tcpci_late_sync.ko | 184 | 2 |

A zero declared dependency count is not proof the driver works alone: DT, power, PHY, clocks, regulators, built-in symbols and role-switching still matter. The large extcon closure includes charging and display-related modules; a naïve two-module `insmod` is not established as sufficient.

`readelf` on the extracted `mtu3`, XSPHY and TCPC modules reports vermagic `6.6.89-android15-8-g03fb7c87b0b5-4k SMP preempt mod_unload modversions aarch64`. The release suffix differs from GKI; these modules are supplied together with the ROM, and Android loads corresponding modules. That difference alone is not proof of incompatibility, nor does the observed Android load validate a new kernel. KMI/modversion checks and driver initialization must be retained.

## Tests and next gates

35 host tests pass, including a synthetic full-OTA ZIP extraction with a deliberately substituted fixture hash; production extraction still accepts only the pinned official ZIP. Coverage includes compressed operations, integrity checks, bad protobuf fields, full-image coverage, boot v3/v4 offsets, AVB footer bounds, newc concatenation and unsafe-member rejection.

Next work is a **host-only candidate image design**, not immediate flashing:

1. Preserve the exact OTA kernel, device trees and platform modules.
2. Resolve how the generic ramdisk will be supplied and selected on this boot chain.
3. Design module initialization and a configfs ACM gadget; derive tty device numbers from sysfs instead of assuming a fixed major.
4. Establish a recovery/restore procedure and assess temporary boot support before any device experiment.
5. Account for AVB footers when designing a candidate image; do not silently strip or disable verified boot.

No temporary boot, custom-image write, root enablement or U-Boot/preloader/LK change was performed.

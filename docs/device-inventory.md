# Initial read-only inventory

Collected 2026-10-03 UTC from a running crDroid installation over authorized ADB. No root request, partition dump, reboot, or partition write was performed during this inventory. Identifiers, complete property dumps, and kernel command-line contents are intentionally excluded.

## Observations

| Field | Observed value |
| --- | --- |
| Android device codename | `rodin` |
| Board platform / reported SoC | `mt6899` / `MT6899` |
| Android ABI | `arm64-v8a` |
| Android release | `16` |
| crDroid version | `12.12` |
| Boot completed | `1` |
| Running kernel release | `6.6.89-android15-8-g8e4be6b47e40-ab14134548-4k` |
| Active slot | `_b` |
| Reported MemTotal | `11130136 kB` |
| SELinux | `Enforcing` |
| `/proc/config.gz` | Readable without root; 45,640 compressed bytes |
| DT compatible via `/sys/firmware/devicetree/base/compatible` | Inaccessible to the ADB shell |

The `android15` component of the kernel release is a kernel build label, not evidence that the Android userspace is version 15.

### Boot-state caveat

Android properties report `ro.boot.verifiedbootstate=green` and `ro.boot.flash.locked=1`. These disagree with the earlier bootloader-side observation `fastboot getvar unlocked: yes`, after which custom recovery and crDroid were successfully installed. Android properties alone are not an authoritative re-check of lock status; the cause of this discrepancy has not been investigated. Do not lock the bootloader or change AVB state in response to these properties.

## Relevant running-kernel configuration

Extracted from the running `/proc/config.gz`, not inferred from a public defconfig.

```text
CONFIG_ARM64=y
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
CONFIG_BLK_DEV_INITRD=y
CONFIG_RD_GZIP=y
CONFIG_RD_LZ4=y
# CONFIG_DEVTMPFS is not set
CONFIG_MODULES=y
CONFIG_MODULE_SIG=y
# CONFIG_MODULE_SIG_FORCE is not set
CONFIG_MODVERSIONS=y
CONFIG_EXT4_FS=y
CONFIG_F2FS_FS=y
CONFIG_SCSI_UFSHCD=y
CONFIG_USB_CONFIGFS=y
# CONFIG_USB_CONFIGFS_RNDIS is not set
CONFIG_USB_CONFIGFS_ECM=y
CONFIG_USB_CONFIGFS_F_FS=y
CONFIG_CGROUPS=y
CONFIG_MEMCG=y
CONFIG_NAMESPACES=y
# CONFIG_USER_NS is not set
CONFIG_OVERLAY_FS=y
CONFIG_SECCOMP=y
CONFIG_SECURITY_SELINUX=y
```

`CONFIG_SCSI_UFS_MEDIATEK`, `CONFIG_DRM_MEDIATEK` and `CONFIG_DRM_PANTHOR` do not appear in this config. Absence is not proof that equivalent vendor functionality is unavailable: this installation uses vendor modules.

## Partitions and modules

Partition names were listed without reading their contents. A/B pairs exist for `boot`, `init_boot`, `vendor_boot`, `dtbo`, `vbmeta`, `vbmeta_system` and `vbmeta_vendor`. `super`, `metadata` and `userdata` also exist. Firmware and boot-chain partitions include preloader, LK and several MediaTek coprocessor images; they are outside the scope of initial experimental writes.

`/vendor/lib/modules` and `/vendor_dlkm/lib/modules` exist. The vendor module directory contains, among others:

- `mali_kbase_mt6899_r49.ko`, `mali_mgm_mt6899_r49.ko`, `mali_prot_alloc_mt6899_r49.ko`
- `mtk_gpufreq_mt6899.ko`, `mtk_gpueb.ko`
- `wlan_drv_gen4m_6899.ko`, `bt_drv_6899.ko`
- `goodix_core_rodin.ko`, `focaltech_touch_rodin.ko`, `xiaomi_touch_rodin.ko`
- `snd-soc-mt6899-afe.ko`, `snd-soc-mt6368.ko`, `mt6899-mt6368.ko`
- `mtk_usb_f_rndis.ko`, `mtk_u_ether.ko`

These are filenames, not validation that each module is loaded or usable outside Android. Module binaries have not been copied or published. Reading `modules.load` did not yield contents; dependency/load-order analysis remains pending.

## Implications for a first non-Android boot

1. Built-in initramfs support makes a minimal alternate initramfs a plausible research direction; it does not establish that an alternate boot image will be accepted or boot successfully.
2. **DEVTMPFS is disabled.** A stock Alpine initramfs cannot simply be assumed to provide the necessary device nodes on this kernel. Investigate explicit device-node creation / an appropriate device manager, or a reproducible kernel configuration change.
3. **USB ECM is enabled in the kernel config.** This is a candidate for a headless console, not a demonstrated working gadget. Controller drivers, vendor modules, configfs setup, permissions and userspace tools still need investigation.
4. `CONFIG_MODVERSIONS=y` makes compatibility with the exact kernel/module ABI important. A generic replacement GKI is not automatically compatible with these vendor modules.
5. Wi-Fi, GPU, storage and power management need separate validation. Do not assume Android vendor drivers alone provide a functional Alpine desktop.
6. Recovery and matching restore artifacts must be established before experimental boots. Neither A/B slots nor `fastboot boot` support are assumed to provide a working rollback path.

## Next read-only work

- Locate complete rodin / MT6899 kernel and vendor-module sources with revision/license evidence.
- Inspect existing downloaded ROM/recovery artifacts on the host for boot format and ramdisk/module layout.
- Design a minimal initramfs and reproducible host-side build; do not flash it yet.

# Linux bring-up research for Xiaomi / POCO rodin

Research towards a native Linux / postmarketOS port for POCO X7 Pro (rodin, MediaTek MT6899 / Dimensity 8400-Ultra).

**Status: research only. No bootable Linux image, mainline support, or working postmarketOS device package is claimed.** The test phone currently boots crDroid 12.12 (Android 16) with an unlocked bootloader.

## Goals

1. Document the device and boot chain using read-only observations.
2. Establish a tested recovery path and preserve matching boot artifacts privately.
3. Investigate a minimal Linux userspace boot using the existing downstream kernel and matching modules/DTB. Feasibility is not yet established.
4. Reach an observable initramfs shell, ideally over USB; then investigate Alpine/postmarketOS packaging.
5. Assess mainline SoC support separately from downstream userspace bring-up.

A shell inside Android/chroot/proot is not the target and must not be reported as a native Linux port.

## Initial findings (2026-10-03)

- postmarketOS wiki searches for `rodin`, `MT6899`, and `Dimensity 8400` returned no entries. This is not proof that no private or unindexed work exists.
- crDroid's rodin BoardConfig identifies `mt6899`, a generic kernel image and prebuilt kernel, DTB/DTBO and vendor modules. It is not a complete build-from-source kernel tree.
- `xaga` (POCO X4 GT / Redmi Note 11T Pro, MT6895 / Dimensity 8100) is a useful Xiaomi/MediaTek mainline reference. Its wiki describes experimental external development, not a ready-made rodin package. Its images, bootloader changes and device tree must NOT be flashed onto rodin.
- MT6878 (Dimensity 7300) provides another recent MediaTek research reference. Different SoC support is not automatically reusable on MT6899.

## Safety and publication rules

- No changes to preloader, LK, GPT, vbmeta or bootloader locking as part of initial research.
- Do not assume `fastboot boot` is supported. Verify capabilities before relying on a temporary boot or rollback strategy.
- Do not flash an experimental kernel until recovery and matching restore artifacts are established.
- Android A/B slots are not a guaranteed rollback mechanism; document slot state and partition writes.
- Keep full images, partition dumps, serial numbers, IMEI, MAC addresses, accounts, tokens and private logs outside Git.
- Do not redistribute proprietary firmware/modules without checking the applicable permissions. Prefer reproducible extraction instructions.
- Maintain an evidence-backed feature matrix and record failures as well as successes.

## Milestones

- [x] Create isolated research repository and branch.
- [ ] Read-only inventory: kernel version/config where accessible, boot image format, DT compatibles, partition layout, modules and USB capabilities.
- [ ] Locate and audit rodin/MT6899 kernel sources and licensing; identify missing components.
- [ ] Confirm recovery path and privately preserve matching restore artifacts.
- [ ] Reproducible minimal initramfs build and boot image inspection, without flashing.
- [ ] First non-Android initramfs boot and observable console.
- [ ] Rootfs + USB networking.
- [ ] Experimental postmarketOS packaging.
- [ ] Storage, display, touch, Wi-Fi, thermal and power-management validation.

## Sources

- [crDroid rodin device tree](https://github.com/crdroidandroid/android_device_xiaomi_rodin/tree/16.0)
- [BoardConfig](https://github.com/crdroidandroid/android_device_xiaomi_rodin/blob/16.0/BoardConfig.mk)
- [crDroid installation guide](https://crdroid.net/rodin/12/install)
- [postmarketOS xaga wiki](https://wiki.postmarketos.org/wiki/Xiaomi_Redmi_Note_11T_Pro_(%2B)_/_POCO_X4_GT_/_Redmi_K50i_(xiaomi-xaga))
- [MT6895 mainline kernel work](https://github.com/MT6895-Mainline/linux)
- [MT6895 initramfs reference](https://github.com/MT6895-Mainline/initramfs)
- [postmarketOS MT6878 wiki](https://wiki.postmarketos.org/wiki/MediaTek_Dimensity_7300_(MT6878))
- [postmarketOS porting documentation](https://wiki.postmarketos.org/wiki/Porting_to_a_new_device)

## Repository and licensing

Public repository: https://github.com/Marinadec-kk/rodin-linux

Initial research branch: `research/bringup`. The repository preserves the existing AGPL-3.0 license selected by its owner. Any future imported kernel code, device trees or third-party patches retain their original licenses and notices; this repository license does not automatically relicense them.

Git author: `Marinadec-kk`, using the GitHub-provided numeric-ID noreply address. Hardware inventory is pending an authorized ADB connection.

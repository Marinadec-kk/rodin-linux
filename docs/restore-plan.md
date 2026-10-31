# Restore preparation for an init_boot-only experiment

**Prepared artifacts, not a tested rollback procedure. Device writes and experimental boot remain blocked.**

The proposed candidate changes **only the generic init_boot ramdisk**. The official OTA `boot`, `vendor_boot`, DT, firmware, vbmeta and both dlkm partitions remain unchanged. There is no reason to flash both slots, change the active slot or disable verification for this experiment.

## Local restore inputs

The exact pinned OTA and extracted images are recorded in [boot-layout.md](boot-layout.md). Before building, the candidate tool checks the original `boot.img`, `vendor_boot.img` and `init_boot.img` against their pinned SHA-256 values. They must remain under `build/ota/`; make a second offline copy of the ZIP and these three images before any phone experiment.

These are **official OTA restore artifacts, not read-back backups of the live partitions**. The OTA kernel release matches the running phone, but we cannot claim the entire installed partitions are byte-identical. ADB access is unprivileged; no block-device reads or root enablement were performed.

The observed active slot is `_b`; partition links for `boot_b`, `vendor_boot_b` and `init_boot_b` exist. Do not hardcode this slot at a later test: recheck it in bootloader fastboot, distinguish bootloader fastboot from userspace fastbootd, and do not change slots as an assumed rollback.

## Gates before the first write

- Have a copy of personal data independent of this phone. Keeping `/data` untouched is not a backup guarantee.
- Verify artifact hashes again on the host and keep the restore image outside the candidate output directory.
- Deliberately demonstrate physical entry into bootloader fastboot from working Android and return to Android. This has **not** been re-tested as part of the present experiment.
- Confirm the bootloader remains unlocked, the current slot and the relevant partition size using explicit read-only fastboot queries. Android verified-boot properties are not reliable evidence of relocking on this setup.
- Confirm the host sees the phone in bootloader fastboot and that the official recovery route remains available. USB ACM failure must not remove the only path back.
- Obtain separate approval for the exact write/test/restore commands. No script in this project performs these operations.

## Planned recovery action, not an executed command

If an approved init_boot-only experiment fails, the intended recovery is **restoring the original init_boot image to the very same explicitly confirmed slot** through bootloader fastboot, then booting Android. If fastboot cannot be reached, stop; do not touch preloader, LK, GPT, firmware or relock the device as a recovery attempt.

Restoring init_boot alone is appropriate only if that is the sole changed partition. It is not a universal recovery guide for a different experiment. A/B fallback and stock recovery accepting arbitrary images have not been assumed or tested.

## Temporary boot limitation

An init_boot image contains no kernel. `fastboot boot init_boot-candidate.img` is **not** a valid assumed test path. Whether this device supports temporary boot at all, and how that path composes a supplied generic ramdisk with the installed init_boot/vendor_boot, remain unknown. An unlocked bootloader does not establish either capability.

## AVB limitation

The original init_boot uses an unsigned `Algorithm: NONE` embedded vbmeta with a SHA-256 hash descriptor. The candidate regenerates that descriptor and footer without setting disable-verification flags. The OTA's signed top-level vbmeta contains the original init_boot hash but **already has flags=3** (hashtree/verification disabled); this is an observation of the official ROM, not a change made by this project. Its SHA-256 is `b82fe64d4308b1cbdd0b5572fe555abdf9d37817e440fdb2b307b805f9e0f702`. The live partition has not been read back, and the bootloader's handling of a custom init_boot is not demonstrated. No top-level vbmeta or signed boot image is modified; candidate acceptance remains unproven.

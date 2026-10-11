# Agent instructions

## Scope and starting point

This repository researches native Linux/postmarketOS bring-up on POCO X7 Pro (`rodin`, MT6899). A chroot/proot inside Android is not the target.

If present, read `HANDOFF.md` first for the last recorded state, artifacts and unresolved issues. Then read `README.md`, `docs/usb-acm-candidate.md` and `docs/restore-plan.md` before changing boot-image code. Historical observations must be rechecked before hardware actions.

The current candidate is host-built and unbooted. Do not describe it as a working port, bootable-on-device image or demonstrated USB console.

## Device safety and authorization

- Default scope: host-only builds/tests and read-only observations. Do not reboot the phone or change its runtime USB configuration without agreement.
- Experimental boot/flashing requires the user present, an explicitly agreed command sequence and a demonstrated physical bootloader recovery path. A request to continue research is not blanket permission to flash.
- Never change preloader, LK, GPT, firmware, slot selection, top-level vbmeta or bootloader locking without separate explicit authorization. Never relock as a recovery shortcut.
- Do not assume `fastboot boot`, A/B fallback or restoring the other slot works. An init_boot image has no kernel and is not an assumed standalone temporary-boot image.
- Preserve original restore inputs. OTA artifacts are not live partition read-back backups. The standalone recovery vendor_boot differs from the OTA vendor_boot; do not mix them blindly.
- No forced module loading or bypassing integrity checks to hide ABI mismatches.
- The candidate exposes an unauthenticated root USB shell and lacks validated thermal/power-management userspace. Any eventual hardware experiment must be short, supervised and local.

## Files, publication and Git

- Keep firmware, module binaries, images, device dumps, identifiers, credentials and private logs out of Git. Generated artifacts belong under ignored `build/`.
- Do not overwrite original artifacts under `build/ota/` with candidate outputs.
- Preserve the existing `main` branch and repository license. Third-party code retains its original license and notices.
- Check Git status before edits; do not discard unrelated work.
- Obtain explicit approval before pushing or other outward-facing actions. Earlier publication is not blanket authorization for future uploads.
- Communicate concisely in Russian unless the user requests another language.

## Checks

Run from the repository root after relevant changes:

```sh
python3 -m unittest discover -s tests -v
sh -n initramfs/init
sh -n initramfs/usb-init
git diff --check
```

With the pinned local inputs and tested host tools available:

```sh
python3 tools/build_candidate.py --apk build/input/busybox-static-1.37.0-r30.apk
python3 tools/build_candidate.py --apk build/input/busybox-static-1.37.0-r30.apk --verify-only
python3 tools/smoke_arm64.py --qemu build/host-tools/bin/qemu-aarch64 --apk build/input/busybox-static-1.37.0-r30.apk
```

The builder requires host `lz4` and `avbtool 1.4.0`. Do not silently replace pinned inputs or weaken checks to accommodate missing dependencies.

QEMU user-mode smoke tests validate ARM64 userspace on the host kernel, not phone boot, module probing, configfs/physical USB or AVB-chain acceptance. Candidate AVB self-hash verification likewise does not prove bootloader acceptance.

Report exactly what was tested, what failed and what remains unverified. Do not claim unattended progress will continue after the agent session ends.

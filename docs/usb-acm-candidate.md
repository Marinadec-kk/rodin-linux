# Host-side experiment 003: an init_boot USB ACM candidate

**UNBOOTED HOST ARTIFACT. Not approved for flashing. Not a working port.**

The next target is a BusyBox shell reached through USB serial without Android userspace. This experiment builds the candidate and checks its contents; it does not establish driver initialization, USB enumeration or bootloader acceptance.

## What changes

- The generic `init_boot` ramdisk is replaced with the pinned static AArch64 BusyBox, explicit device nodes, `initramfs/usb-init`, and a generated module load/checksum plan.
- Android init, snapuserd and the original generic userspace are intentionally not included. There is no Android/native boot selector: if this image were accepted on the normal boot path, Android would not start until the original init_boot is restored.
- The complete original header page is preserved except its ramdisk-size field. The ramdisk uses gzip; the config embedded in the actual extracted OTA kernel confirms gzip initrd, script/ELF execution, modules, modversions, configfs and ACM support.
- The init_boot footer and its unsigned `Algorithm: NONE` hash descriptor are regenerated with the original salt and compatibility properties. A property labels the experiment. The candidate has no disable-verification flags.
- **Kernel, boot image, vendor_boot, DTB/DTBO, firmware, top-level vbmeta, dlkm partitions, partition layout and slot state are not modified.**

The existing vendor_boot PLATFORM fragment is expected to provide the drivers when the bootloader composes the ramdisk. Those module binaries are **not copied into the candidate**. The expected composition has not been demonstrated with the custom init.

## Building and reproducibility

Requires Python 3, host `lz4` and tested `avbtool 1.4.0`. This host supplies avbtool through Fedora's `android-tools-37.0.0-4.fc44` package. All three original boot artifacts and the BusyBox APK are hash-checked before use. See [experiment 002](boot-layout.md) for extraction and [experiment 001](host-bringup.md) for the pinned APK input.

```bash
python3 tools/build_candidate.py \
  --apk build/input/busybox-static-1.37.0-r30.apk
python3 tools/build_candidate.py \
  --apk build/input/busybox-static-1.37.0-r30.apk --verify-only
```

Outputs stay under ignored `build/candidate/`. `--verify-only` rebuilds in temporary host storage, verifies the generated AVB self-hash and compares **every output artifact byte-for-byte**, without replacing outputs. It is not a device test.

Observed local result:

| Artifact | Bytes / SHA-256 |
| --- | --- |
| `init_boot-candidate.img` | 8,388,608 bytes; `c432dd4578f08717cf2d34d1fd3b010d889d08e145fcdfa70e6213a16db56f72` |
| `initramfs.cpio.gz` | 660,358 bytes; `6cea3554fa7377bafbe1f8252b9fa043216c2ee8f0fdf7d0082f28d24310254a` |
| embedded OTA kernel config | `9b544345144b5e7d050981905a655308700151693ddf9d10f94eee1eadeb19ec` |
| selected module load plan | 90 entries; `407f55f7b1812cc1b6c8d40702599c607cf07842530b0505c1b2592add84044c` |

Also emitted: uncompressed cpio, `modules.load`, `modules.sha256`, `avb-info.txt`, and a detailed `manifest.json`. Image size includes partition padding and AVB metadata; the userspace payload is about 645 KiB compressed. No generated binaries or private logs are committed.

An init_boot image has **no kernel**. Do not treat it as a standalone `fastboot boot` image.

### AVB checks and their limits

The builder generates and verifies a file named `init_boot.img` in an isolated temporary directory. This matters: `avbtool verify_image` resolves a hash descriptor's partition by name; an arbitrary filename could otherwise point it at a different sibling image. The final artifact is deliberately named `init_boot-candidate.img`.

Successful verification here proves that the regenerated unsigned descriptor matches the candidate's contents. It does not prove authenticity or acceptance by the phone's AVB chain.

Additional read-only OTA extraction of `vbmeta`, `vbmeta_system` and `vbmeta_vendor` is now supported:

```bash
python3 tools/extract_boot_payload.py \
  ../downloads/crDroidAndroid-16.0-20260925-rodin-v12.12.zip \
  --partitions vbmeta vbmeta_system vbmeta_vendor --output build/avb-reference
avbtool info_image --image build/avb-reference/vbmeta.img
```

The official OTA top-level vbmeta is RSA4096 signed, contains original init_boot/vendor_boot hashes and chained boot/system/vendor descriptors, and **already has flags=3** (hashtree disabled + verification disabled). These flags were not changed by the experiment. OTA vbmeta SHA-256: `b82fe64d4308b1cbdd0b5572fe555abdf9d37817e440fdb2b307b805f9e0f702`. This is not a read-back of the currently installed partition and does not establish custom-image acceptance.

## What PID 1 attempts

1. Refuse execution outside PID 1 before mounting or inserting anything. This protects against accidentally launching it in an ordinary host/Android shell.
2. Install BusyBox applet symlinks inside the writable initramfs; mount proc, sysfs, tmpfs at `/run`, and devpts. Keep PID 1 alive on initialization failures rather than intentionally triggering a kernel panic.
3. Check the expected SHA-256 of every selected module under `/lib/modules` against the OTA platform fragment. Wrong ramdisk/module combinations abort USB initialization.
4. Insert modules once in dependency order, without forcing vermagic/signatures. Any insertion failure prevents USB setup and is logged to `/run/rodin-bringup.log` and kernel messages.
5. Mount configfs inside `/run`; wait up to 30 seconds for a single UDC. No Android property is assumed and multiple controllers cause a safe selection failure.
6. Create one ACM function and configuration, then bind it to the discovered controller. Use fixed diagnostic strings, not the phone's serial number. VID/PID `1d6b:0104` are conventional Linux diagnostic gadget defaults, not IDs assigned for a shipping product.
7. Derive `/dev/ttyGS0`'s device numbers from sysfs, then restart `getty -L -n -l /bin/sh` whenever the shell exits. No guessed tty major or Android adbd/FunctionFS is used.

The shell has **no password** and full root privileges in this minimal system. It is for short, supervised, physically local research, not an unattended network service. The script does not mount or write phone block storage; a root shell is not a sandbox and must not be mistaken for a data-protection guarantee. A configfs bind log is not proof that a host enumerated or opened the device.

Logs are only in RAM/kernel messages; no persistent logging setup is created. On USB failure there is a `/dev/console` getty fallback, but that hardware console is not known to be observable.

## Why 90 modules, rather than the entire platform load list

`build_candidate.py` chooses provisional GPIO/pinctrl, clocks, regulators, PMIC/bus and USB/Type-C/mux seeds in the original load-list order, then computes their declared symbol dependency closure. The closure expands some seeds into charging/display helper modules because the downstream code couples those subsystems.

It does not blindly insert all 223 dependency-expanded platform modules. Explicit storage, watchdog/hang-detector and deliberate crash-driver entries are excluded, including if pulled as dependencies. Excluding watchdog drivers does **not** prove a bootloader-started watchdog is inactive or handled correctly; handoff remains an open hardware question.

Declared module dependencies do not capture all DT, power-domain, nvmem, firmware, role-switch or deferred-probe dependencies. No external firmware tree or Android HAL is provided. A 90-module plan is a candidate to validate, not a demonstrated minimal USB stack. Driver registration, peripheral-role selection, battery/thermal behavior and stable runtime remain unverified.

## Host validation

- **46 unit tests pass**. Added cases cover exact header-page preservation, raw ramdisk offsets/alignment, wrong image types, oversize payloads, dependency order/deduplication/cycles, missing metadata, unsafe paths, excluded watchdog dependencies and atomic output replacement without following a symlink into restore data.
- Both PID 1 scripts pass host `sh -n`; the USB init also passes the actual pinned ARM64 BusyBox ash parser.
- Two local candidate builds match byte-for-byte, including the manifest and regenerated footer; avbtool verifies the unsigned self-hash.
- Original boot/init_boot/vendor_boot hashes remain unchanged.
- The pinned ARM64 BusyBox executes under QEMU user mode. Required applets and the applet installer are present. A non-PID-1 launch refuses before any mounts.
- The actual ARM getty opens a host pseudoterminal; a small native host launcher forwards getty's arguments into ARM ash. That interactive ARM shell receives and executes a test command. Input echo is not mistaken for command success.

User-mode QEMU is **not** ARM system emulation: it uses the host Linux kernel and cannot validate phone boot, kernel module ABI/probing, configfs, USB hardware or ramdisk composition.

Smoke-test command:

```bash
python3 tools/smoke_arm64.py --qemu /path/to/qemu-aarch64 \
  --apk build/input/busybox-static-1.37.0-r30.apk
```

Local QEMU provenance: `qemu-user-10.2.2-1.fc44.x86_64.rpm`, downloaded from configured Fedora repositories without installation or privilege escalation. `rpmkeys --checksig` reported digests/signatures OK. RPM SHA-256: `c219034b5b42008daea98da0508001114617423eeb397fbe1f2f75b7c60b2dcf`. Only `qemu-aarch64` was copied from that package into ignored host tools. CI remains offline unit/syntax checks; it does not fetch firmware, BusyBox or QEMU.

## Remaining gates

See [restore preparation](restore-plan.md). Physical bootloader recovery must be deliberately demonstrated before a custom write. Live-slot state, installed artifacts and temporary-boot behavior still need confirmation. Any first hardware test must be short and supervised; this userspace has no validated Android-equivalent thermal or power management.

**No experimental boot, image flashing, root enablement, slot change or vbmeta modification was performed.**

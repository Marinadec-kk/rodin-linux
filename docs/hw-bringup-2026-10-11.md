# Hardware bring-up log, 2026-10-11

All experiments wrote only `init_boot_b` (plus restores of the original image) through
bootloader fastboot, with the owner present. Boot, vendor_boot, vbmeta, dtbo-less DT,
dlkm and firmware partitions stayed original. Results below are observations on one
device running crDroid 12.12 (kernel `6.6.89-android15-8-g8e4be6b47e40-ab14134548-4k`).

## Outcome

A host-built init_boot reaches our own PID 1 on the stock GKI kernel, loads the
vendor_boot module set, and exposes a USB ACM root shell and an NCM network function.
UFS storage, the fuel gauge, the charger stack (battery reports `Charging` on USB) and
cpufreq work. A Linux rootfs on userdata, Wi-Fi, SSH and thermal management are not
done yet. The owner reported strong heating in this state; SoC temperature is not
observable until thermal drivers from vendor_dlkm are loaded.

## Findings, in the order they were discovered

| # | Experiment | Observation | Conclusion |
| - | --- | --- | --- |
| 1 | gzip ramdisk, `/init` = static ELF that sleeps 5 s and calls `reboot(RESTART2,"bootloader")` | ~8.6 s preloader loop, logo cut at once | gzip generic ramdisk never reaches `/init` |
| 2 | same probe, LZ4 legacy (`lz4 -l`) | loop period grows to ~15.4 s (+5 s sleep) | `/init` runs; AVB footer with algorithm NONE is accepted; `bootloader` reboot reason ignored without reboot-mode driver |
| 3 | candidate with 90-module plan, LZ4 | ~61 s loop, no gadget | `mtk_scpsys_mt6899` blocks on VCP (no `vcp.ko`), watchdog resets |
| 4 | full Android `modules.load` order | ~33 s loop | an unrelated module resets the phone |
| 5 | early `mtk_wdt` + `reboot-mode` + `syscon-reboot-mode`, `reboot("bootloader")` | phone returns to fastboot by itself in ~17 s | button-free recovery and bisection possible |
| 6 | bisection (load N modules, hold 40 s, reboot to bootloader) | N≤161 survive, N=162 resets | `bq28z610` (fuel gauge) triggers the reset |
| 7 | USB brought up before the remaining modules | gadget enumerates; shell works | ACM console demonstrated |
| 8 | `reset-ti-syscon` loaded from the shell (UFS reset-controller supplier) | UFS probes, then NULL-pointer Oops in Xiaomi EUA code | `strnstr(product_name_get(), "rodin")` with NULL product name |
| 9 | pass `<module>.<param>=` words from `/proc/cmdline` to insmod | `hwid.project_name=rodin` set; UFS, `bq28z610`, charger stack load cleanly | busybox insmod lacks libmodprobe's cmdline handling; this was the root cause of #6 and #8 |

## Practical notes

- The bootloader cmdline contains module parameters that must reach modules:
  `hwid.*`, `aee_aed.*`, `panel-…-dsc-vdo.oled_*`, `ufshcd_core.poll_queues`. It also
  configures `block2mtd`/`mtdoops` to write to the `oops` partition; those modules are
  skipped.
- pstore/ramoops is configured on the cmdline (`0x6ec00000`) but the readable record
  stayed frozen at one early boot; it is not a usable crash channel. Live
  `cat /proc/kmsg` over the ACM shell captured the UFS Oops.
- mtu3 starts in host role; `usb-init` writes `device` to the role-switch sysfs node.
  With the charger stack loaded before USB setup, the charger detects the host port.
- `CONFIG_DEVTMPFS`, `CONFIG_VT` and `CONFIG_CFG80211` are not built in; `cfg80211.ko`
  and the MediaTek Wi-Fi stack (`conninfra`, `wlan_drv_gen4m_6899`) are vendor_dlkm
  modules. The OTA carries separate `connsys_wifi`, `connsys_bt` and `connsys_gnss`
  partitions.
- The host's ModemManager probes the ACM port for ~30 s after each enumeration.

## Recheck 2026-10-31

- After ~20 days unplugged the battery was at 1 %; Linux boot-looped on USB power
  (boot load exceeds the ~340 mA a PC port delivers). Recovered by charging in LK
  fastboot (`off-mode-charge: 1`); at 4 % Linux then booted and stayed up.
- Battery `current_now` is negative while charging (voltage rises; the charger log
  `CHARGE_LOOP BMS = [.. 3651 -437 ..]` reports the same sign).
- Load average ~15 at idle comes from D-state kernel threads (`wdtk-*`,
  `mitee_worker/*`); CPU is ~96 % idle. It is not a real load.
- Touch coordinate mode: on boot the panel-on hook's `enable_touch_raw=0` succeeded
  (`goodix_htc_enable_touch_coord: success send touch data cmd 1`).
- Power-key wake **failed for the user**: the kernel logged UNBLANK, the Goodix
  resume and the hook's `enable_touch_raw 0` at +2 s, but the user saw no picture and
  a second press 4 s later blanked again. During those 4 s, `cat /dev/input/event2`
  (goodix_ts) captured 0 bytes. The 2026-10-11 backlight fix is therefore not reliable;
  touch input is still unconfirmed.

## Not verified

Touch input events from a finger, reliable wake from blank, Wi-Fi association,
audio, modem, camera, suspend, long-run stability.

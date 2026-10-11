"""Configuration payloads for the host rootfs builder."""
INITTAB = '''# BRING-UP ONLY: unauthenticated root shell on the USB console before OpenRC runs.
::sysinit:/bin/sh -c "/bin/busybox setsid /sbin/getty -n -l /bin/sh 115200 ttyGS0 vt100 &"
::sysinit:/sbin/openrc sysinit
::sysinit:/sbin/openrc boot
::wait:/sbin/openrc default
::ctrlaltdel:/sbin/reboot
::shutdown:/sbin/openrc shutdown
'''
THERMAL_CONF = '''SOC_HIGH=70
SOC_LOW=60
SOC_CRITICAL=90
BAT_HIGH=45
BAT_LOW=42
BAT_CRITICAL=55
INTERVAL=5
# Available-frequency entries to lower on each hot sample.
STEP_DOWN=2
# Available-frequency entries to raise on each cool sample.
STEP_UP=1
# Linear steps between cpuinfo_min_freq and the ceiling when no table is available.
STEP_COUNT=8
# Consecutive critical readings before poweroff (one bogus sample must not loop).
CRITICAL_COUNT=3
# Boot-time cap on every cpufreq policy until the policy is validated on hardware.
MAX_FREQ_PERCENT=70
# Backlight brightness written at start (0 = off; no display stack yet).
BACKLIGHT=0
'''
DEV = '''#!/sbin/openrc-run
description="Rodin tmpfs devices (kernel has no devtmpfs)"
depend() {
    provide dev dev-mount
    need sysfs procfs
    before localmount bootmisc rodin-thermal sshd
}
start() {
    # Stage 1 normally hands over a populated tmpfs /dev; only mount one if it did not.
    if ! mountpoint -q /dev; then
        mount -t tmpfs -o mode=0755,size=10M,nosuid tmpfs /dev || return 1
    fi
    mkdir -p /dev/pts /dev/shm
    mountpoint -q /dev/pts || mount -t devpts -o gid=5,mode=0620,nosuid,noexec devpts /dev/pts || return 1
    mountpoint -q /dev/shm || mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm || return 1
    [ -c /dev/console ] || mknod -m 600 /dev/console c 5 1
    [ -c /dev/null ] || { rm -f /dev/null; mknod -m 666 /dev/null c 1 3; }
    [ -c /dev/tty ] || mknod -m 666 /dev/tty c 5 0
    ln -sfn /proc/self/fd /dev/fd
    ln -sf /proc/self/fd/0 /dev/stdin
    ln -sf /proc/self/fd/1 /dev/stdout
    ln -sf /proc/self/fd/2 /dev/stderr
    /sbin/mdev -s
    # No CONFIG_UEVENT_HELPER (no /proc/sys/kernel/hotplug): listen on netlink instead.
    start-stop-daemon --start --background --make-pidfile --pidfile /run/rodin-mdev.pid \
        --exec /sbin/mdev -- -d -f || ewarn "mdev netlink daemon not started"
    # mdev creates the nodes; eudev's udevd only provides the property database
    # (ID_INPUT_* etc.) that libinput needs to accept devices.
    if [ -x /sbin/udevd ]; then
        /sbin/udevd --daemon && udevadm trigger --action=add && udevadm settle -t 15 || ewarn "udev property database incomplete"
    fi
    # Let the desktop session (group video) set the brightness.
    for led in /sys/class/leds/*/brightness; do
        [ -e "$led" ] && chgrp video "$led" && chmod g+w "$led"
    done
}
'''
USBNET = '''#!/sbin/openrc-run
description="Local USB NCM DHCP (no default route or DNS)"
depend() { need dev; after rodin-thermal; }
start() {
    if [ ! -e /sys/class/net/usb0 ]; then
        ewarn "usb0 absent; leaving USB networking disabled"
        return 0
    fi
    ip link set usb0 up || return 1
    ip addr replace 172.16.42.1/24 dev usb0 || return 1
    # Internet through the host when it shares its connection (NM "shared");
    # high metric so Wi-Fi (udhcpc, metric 0) wins whenever it is up.
    ip route replace default via 172.16.42.2 dev usb0 metric 1000 2>/dev/null || :
    [ -s /etc/resolv.conf ] || echo 'nameserver 172.16.42.2' > /etc/resolv.conf
    mkdir -p /var/lib/udhcpd
    start-stop-daemon --start --background --make-pidfile --pidfile /run/rodin-udhcpd.pid \
        --exec /usr/sbin/udhcpd -- -f /etc/udhcpd.conf
}
stop() {
    start-stop-daemon --stop --pidfile /run/rodin-udhcpd.pid
}
'''
THERMAL_SERVICE = '''#!/sbin/openrc-run
description="Experimental thermal safety net"
command=/usr/local/sbin/rodin-thermald
command_background=yes
pidfile=/run/rodin-thermald.pid
depend() {
    need dev
    use logger
    before sshd rodin-usbnet
}
start_pre() {
    while read -r module rest; do
        case "$module" in ''|\\#*) continue;; esac
        modprobe "$module" || eerror "thermal module failed: $module; continuing safety monitor"
    done < /etc/rodin/thermal-modules.list
}
'''
THERMALD = '''#!/bin/sh
. /etc/conf.d/rodin-thermal
state=/run/rodin-thermal
mkdir -p "$state"
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [ -d "$policy" ] || continue
    name=${policy##*/}
    echo schedutil > "$policy/scaling_governor" 2>/dev/null || :
    top=$(cat "$policy/cpuinfo_max_freq" 2>/dev/null)
    case "$top" in
        ''|*[!0-9]*) ;;
        *)
            cap=$((top * ${MAX_FREQ_PERCENT:-100} / 100))
            best=
            for frequency in $(cat "$policy/scaling_available_frequencies" 2>/dev/null); do
                case "$frequency" in ''|*[!0-9]*) continue;; esac
                [ "$frequency" -le "$cap" ] || continue
                if [ -z "$best" ] || [ "$frequency" -gt "$best" ]; then best=$frequency; fi
            done
            [ -n "$best" ] || best=$cap
            echo "$best" > "$policy/scaling_max_freq" 2>/dev/null || :
            ;;
    esac
    # The restore target is the boot cap, not the hardware maximum.
    cat "$policy/scaling_max_freq" > "$state/$name.max" 2>/dev/null
    # Cache the sorted table once; cpufreq tables do not change between samples.
    awk '{ for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+$/ && $i > 0) print $i }' \
        "$policy/scaling_available_frequencies" 2>/dev/null | sort -nu > "$state/$name.freqs"
done
if [ -n "${BACKLIGHT:-}" ]; then
    for led in /sys/class/leds/lcd-backlight/brightness; do
        [ -w "$led" ] && echo "$BACKLIGHT" > "$led"
    done
fi
sensors_missing=no
critical_seen=0
while :; do
    hot=no
    cool=yes
    critical=no
    readable=0
    hottest=0
    battery=0
    for sensor in /sys/class/thermal/thermal_zone*/temp /sys/class/power_supply/battery/temp; do
        [ -r "$sensor" ] || continue
        value=
        read -r value < "$sensor" 2>/dev/null || :
        case "$value" in ''|*[!0-9]*) continue;; esac
        readable=$((readable + 1))
        case "$sensor" in
            */battery/temp) value=$((value * 100)); battery=$value
               high=$((BAT_HIGH * 1000)); low=$((BAT_LOW * 1000)); limit=$((BAT_CRITICAL * 1000));;
            *) [ "$value" -gt 1000 ] || value=$((value * 1000))
               [ "$value" -le "$hottest" ] || hottest=$value
               high=$((SOC_HIGH * 1000)); low=$((SOC_LOW * 1000)); limit=$((SOC_CRITICAL * 1000));;
        esac
        [ "$value" -le "$high" ] || hot=yes
        [ "$value" -lt "$low" ] || cool=no
        [ "$value" -le "$limit" ] || critical=yes
    done
    if [ "$readable" -eq 0 ]; then
        hot=yes
        cool=no
        if [ "$sensors_missing" = no ]; then
            logger -t rodin-thermal "No readable temperature sensors; stepping down"
        fi
        sensors_missing=yes
    else
        sensors_missing=no
    fi
    if [ "$critical" = yes ]; then
        critical_seen=$((critical_seen + 1))
        logger -t rodin-thermal "critical temperature reading $critical_seen/${CRITICAL_COUNT:-3}"
        if [ "$critical_seen" -ge "${CRITICAL_COUNT:-3}" ]; then
            logger -t rodin-thermal "CRITICAL temperature; powering off"
            /sbin/poweroff
        fi
    else
        critical_seen=0
    fi
    direction=hold
    if [ "$hot" = yes ]; then
        direction=down
        steps=${STEP_DOWN:-2}
    elif [ "$cool" = yes ]; then
        direction=up
        steps=${STEP_UP:-1}
    fi
    if [ "$direction" != hold ]; then
        for policy in /sys/devices/system/cpu/cpufreq/policy*; do
            [ -d "$policy" ] || continue
            name=${policy##*/}
            old=
            ceiling=
            read -r old < "$policy/scaling_max_freq" 2>/dev/null || :
            read -r ceiling < "$state/$name.max" 2>/dev/null || :
            case "$old:$ceiling" in *[!0-9:]*|:*|*:) continue;; esac
            if [ -s "$state/$name.freqs" ]; then
                new=$(awk -v old="$old" -v ceiling="$ceiling" \
                    -v direction="$direction" -v steps="$steps" '
                    { f[NR]=$1; if ($1 <= old) pos=NR }
                    END {
                        if (!pos) pos=1
                        if (direction == "down") pos-=steps; else pos+=steps
                        if (pos < 1) pos=1
                        if (pos > NR) pos=NR
                        value=f[pos]
                        if (value > ceiling) value=ceiling
                        print value
                    }' "$state/$name.freqs") || continue
            else
                minimum=
                read -r minimum < "$policy/cpuinfo_min_freq" 2>/dev/null || :
                case "$minimum" in ''|*[!0-9]*) continue;; esac
                stride=$(((ceiling - minimum) / ${STEP_COUNT:-8}))
                [ "$stride" -gt 0 ] || stride=1
                if [ "$direction" = down ]; then
                    new=$((old - stride * steps))
                else
                    new=$((old + stride * steps))
                fi
                [ "$new" -ge "$minimum" ] || new=$minimum
                [ "$new" -le "$ceiling" ] || new=$ceiling
            fi
            if [ "$new" != "$old" ]; then
                if echo "$new" > "$policy/scaling_max_freq" 2>/dev/null; then
                    logger -t rodin-thermal "$name $old->$new kHz; SoC=$hottest battery=$battery mC readable=$readable"
                fi
            fi
        done
    fi
    sleep "$INTERVAL"
done
'''
WIFI = '''#!/sbin/openrc-run
description="Wi-Fi hardware power-on (association is NetworkManager's job)"
depend() { need dev; after rodin-thermal; before networkmanager; }
start() {
    ebegin "Powering on Wi-Fi"
    while read -r module rest; do
        case "$module" in ''|\\#*) continue;; esac
        modprobe "$module" || { eend 1 "modprobe $module failed"; return 1; }
    done < /etc/rodin/wifi-modules.list
    /etc/rodin/wifi-poweron.sh
    eend $?
}
stop() {
    ebegin "Powering off Wi-Fi"
    [ -c /dev/wmtWifi ] && echo 0 > /dev/wmtWifi
    eend 0
}
'''
WIFI_POWERON = '''#!/bin/sh
# Wi-Fi power-on as validated on hardware 2026-10-11. The Wi-Fi RAM code is
# preloaded by LK; the kernel side needs (1) the calibration NVRAM pushed through
# /dev/wmtWifi ("WR-BUF:NVRAM" + data in ONE write) so gen4m registers with WMT,
# (2) conninfra pre-calibration, which only starts once BOTH Wi-Fi and BT
# (bt_drv_6899) are registered, then (3) "1" written to /dev/wmtWifi.
set -e
log() { logger -t rodin-wifi "$*"; echo "rodin-wifi: $*"; }
nvdev=
for uevent in /sys/class/block/*/uevent; do
    grep -qx PARTNAME=nvdata "$uevent" && nvdev=/dev/$(basename "${uevent%/uevent}")
done
[ -b "$nvdev" ] || { log "nvdata partition not found"; exit 1; }
mkdir -p /run/rodin-nvdata
# noload: never replay the journal; this partition belongs to the modem/factory data.
mount -o ro,noload "$nvdev" /run/rodin-nvdata
trap 'umount /run/rodin-nvdata 2>/dev/null || :' EXIT
{ printf 'WR-BUF:NVRAM'; cat /run/rodin-nvdata/APCFG/APRDEB/WIFI; } > /run/rodin-wifi-nvram.bin
umount /run/rodin-nvdata
trap - EXIT
if [ ! -c /dev/wmtWifi ]; then
    major=$(awk '$2 == "mtk_wmt_wifi_chrdev" {print $1}' /proc/devices)
    [ -n "$major" ] || { log "mtk_wmt_wifi_chrdev not registered"; exit 1; }
    mknod -m 600 /dev/wmtWifi c "$major" 0
fi
dd if=/run/rodin-wifi-nvram.bin of=/dev/wmtWifi bs=16384 count=1 2>/dev/null
rm -f /run/rodin-wifi-nvram.bin
# Pre-calibration runs asynchronously after the NVRAM push; power-on retries until done.
attempt=0
until echo 1 > /dev/wmtWifi 2>/dev/null; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 6 ] || { log "WMT power-on failed"; exit 1; }
    sleep 5
done
count=0
until [ -e /sys/class/net/wlan0 ]; do
    count=$((count + 1))
    [ "$count" -lt 20 ] || { log "wlan0 did not appear"; exit 1; }
    sleep 1
done
log "wlan0 is up"
'''
PHOSH_SESSION = '''#!/bin/sh
# Phosh session for rodin: no usable GPU driver on Linux (Mali blob is Android-only),
# so wlroots renders with pixman and GTK4 with cairo.
export WLR_RENDERER=pixman
export GSK_RENDERER=cairo
export GDK_DEBUG=gl-disable
export LIBGL_ALWAYS_SOFTWARE=1
export XDG_CURRENT_DESKTOP=Phosh:GNOME
export XDG_SESSION_TYPE=wayland
# Initial brightness (thermald leaves the backlight alone when BACKLIGHT is empty).
[ -w /sys/class/leds/lcd-backlight/brightness ] && echo 3000 > /sys/class/leds/lcd-backlight/brightness
exec /usr/bin/phosh-session
'''
GREETD_CONFIG = '''[terminal]
# The kernel has no CONFIG_VT.
vt = "none"

[default_session]
command = "agreety --cmd /bin/sh"
user = "greetd"

[initial_session]
command = "/usr/local/bin/rodin-phosh-session"
user = "marinadec"
'''
NM_CONF = '''[main]
plugins=keyfile
dhcp=internal

[keyfile]
# usb0 is configured by rodin-usbnet; the others are modem/virtual interfaces.
unmanaged-devices=interface-name:usb0;interface-name:ccmni*;interface-name:p2p*;interface-name:dummy*;interface-name:ifb*;interface-name:*tnl*;interface-name:*gre*;interface-name:sit*;interface-name:ip_vti*;interface-name:ip6_vti*;interface-name:erspan*

[device]
wifi.backend=wpa_supplicant
wifi.scan-rand-mac-address=no
'''
PANEL_HOOK = r'''#!/bin/sh
# React to panel power-on. Blanking makes the kernel set the backlight to 0
# directly (sysfs keeps the old value) and nothing restores it, because Phosh
# drives /sys/class/backlight and this panel only has leds/lcd-backlight.
# Trigger: the Xiaomi touch notifier's "touchpanel resume" line in /dev/kmsg.
led=/sys/class/leds/lcd-backlight/brightness
start_us=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
restore_backlight() {
    level=$(cat "$led" 2>/dev/null)
    case "$level" in ''|*[!0-9]*|0) level=3000 ;; esac
    # Writing the unchanged value is ignored by the driver; force a real update.
    echo $((level - 1)) > "$led"
    echo "$level" > "$led"
}
# /dev/kmsg returns whole records only (a 1-byte read, as ash's `read` does,
# fails with EINVAL), so let cat read it; reopen if it ever exits (EPIPE).
while :; do
cat /dev/kmsg | while IFS= read -r line; do
    case "$line" in
        *"touchpanel resume"*) ;;
        *) continue ;;
    esac
    stamp=$(echo "$line" | cut -d, -f3)
    case "$stamp" in ''|*[!0-9]*) continue ;; esac
    [ "$stamp" -ge "$start_us" ] || continue
    # The panel ignores brightness until its first frames are out; retry.
    for delay in 0.3 0.7 1; do
        sleep "$delay"
        restore_backlight
    done
    [ -x /etc/rodin/panel-on.d/touch ] && /etc/rodin/panel-on.d/touch
done
sleep 1
done
'''
PANEL_HOOK_INIT = r'''#!/sbin/openrc-run
description="Restore backlight (and touch mode) when the panel powers on"
command=/usr/local/sbin/rodin-panel-hook
command_background=yes
pidfile=/run/rodin-panel-hook.pid
depend() { need dev; before greetd; }
'''
FILES = {
    '/etc/inittab': INITTAB,
    '/etc/hostname': 'rodin\n',
    '/etc/hosts': '127.0.0.1 localhost rodin\n::1 localhost rodin\n',
    '/etc/timezone': 'UTC\n',
    '/etc/doas.d/rodin.conf': 'permit nopass :wheel\n',
    '/etc/ssh/sshd_config': 'PasswordAuthentication no\nPermitRootLogin no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nSubsystem sftp /usr/lib/ssh/sftp-server\n',
    '/etc/conf.d/sshd': 'rc_need="rodin-thermal dev"\n',
    '/etc/conf.d/rodin-thermal': THERMAL_CONF,
    # No CONFIG_VT: a VT-bound seat never becomes active.
    '/etc/conf.d/seatd': 'export SEATD_VTBOUND=0\n',
    '/etc/greetd/config.toml': GREETD_CONFIG,
    '/etc/NetworkManager/conf.d/rodin.conf': NM_CONF,
    '/etc/conf.d/networkmanager': 'rc_after="rodin-wifi"\n',
    '/etc/conf.d/greetd': 'rc_need="dbus elogind"\nrc_after="rodin-touch"\n',
    '/etc/apk/repositories': 'https://mirrors.dotsrc.org/alpine/v3.23/main\nhttps://mirrors.dotsrc.org/alpine/v3.23/community\n',
    '/etc/udhcpd.conf': 'start 172.16.42.2\nend 172.16.42.2\ninterface usb0\nmax_leases 1\nlease_file /var/lib/udhcpd/leases\npidfile /run/udhcpd.pid\noption subnet 255.255.255.0\n',
    '/etc/wpa_supplicant/wpa_supplicant.conf': 'ctrl_interface=/run/wpa_supplicant\nupdate_config=1\ncountry=RU\n# Add networks locally; Wi-Fi service is not enabled.\n',
    '/etc/rodin/thermal-modules.list': '# Pending hardware-specific thermal module investigation.\n',
    '/etc/rodin/wifi-modules.list': '# Pending hardware-specific Wi-Fi module investigation.\n',
    '/etc/fstab': '# Stage 1 mounts the userdata root read/write; no /dev/root dependency.\ntmpfs /tmp tmpfs nosuid,nodev 0 0\n',
    '/etc/motd': 'EXPERIMENTAL native Linux on rodin. Thermal policy is a safety net only.\nSupervised local use only; wheel has temporary passwordless doas.\n',
    '/etc/rodin/README': 'rodin-dev populates tmpfs /dev with mdev. Stage 1 keeps the watchdog alive.\nrodin-thermal starts in boot; fill thermal-modules.list before hardware use.\nrodin-usbnet offers 172.16.42.2 to the USB host; SSH keys only.\nrodin-wifi is NOT enabled; fill its module list, power hook and network config.\nRoot is already writable: root/fsck services disabled; host fsck validates the image.\nNo hardware validation, suspend or complete thermal management is implied.\n',
}
TOUCH = '''#!/sbin/openrc-run
description="Goodix GT9916 touchscreen (Xiaomi THP driver) handed to the AP"
depend() { need dev; after rodin-thermal; }
start() {
    ebegin "Loading touch drivers"
    for module in xiaomi_touch_rodin goodix_core_rodin; do
        modprobe "$module" || { eend 1 "modprobe $module failed"; return 1; }
    done
    # Late init (firmware/config check) runs asynchronously after probe.
    count=0
    until [ -w /sys/class/touch/touch_dev/touch_doze_analysis ]; do
        count=$((count + 1))
        [ "$count" -lt 30 ] || { eend 1 "touch_doze_analysis missing"; return 1; }
        sleep 1
    done
    sleep 2
    # Without Android's display HAL no panel DPMS-on event follows boot, so the
    # sensor hub (SCP) keeps the touch bus (scptp_cur_state=2) and no input events
    # reach /dev/input. "0" = driver suspend+resume cycle: switches the bus to the
    # AP and resets the IC into touch-data mode.
    echo 0 > /sys/class/touch/touch_dev/touch_doze_analysis
    eend $?
}
'''
SCRIPTS = {
    '/etc/init.d/rodin-dev': DEV,
    '/etc/init.d/rodin-usbnet': USBNET,
    '/etc/init.d/rodin-thermal': THERMAL_SERVICE,
    '/etc/init.d/rodin-wifi': WIFI,
    '/etc/init.d/rodin-touch': TOUCH,
    '/usr/local/sbin/rodin-panel-hook': PANEL_HOOK,
    '/etc/init.d/rodin-panel-hook': PANEL_HOOK_INIT,
    '/usr/local/bin/rodin-phosh-session': PHOSH_SESSION,
    '/usr/local/sbin/rodin-thermald': THERMALD,
    '/etc/rodin/wifi-poweron.sh': WIFI_POWERON,
}
RUNLEVELS = {
    'sysinit': ['procfs', 'sysfs', 'rodin-dev', 'syslog', 'klogd'],
    'boot': ['rodin-thermal', 'hostname', 'bootmisc', 'localmount', 'sysctl'],
    'default': ['rodin-usbnet', 'sshd', 'rodin-touch', 'dbus', 'elogind', 'greetd', 'rodin-wifi', 'networkmanager', 'rodin-panel-hook'],
    'shutdown': ['killprocs', 'mount-ro'],
}


def with_inputs(inputs):
    """FILES with module lists taken from build/rootfs-inputs/*.list when present."""
    files = dict(FILES)
    for name in ('thermal-modules.list', 'wifi-modules.list'):
        source = inputs / name
        if source.is_file():
            files['/etc/rodin/' + name] = source.read_text()
    return files

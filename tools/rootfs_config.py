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
    mkdir -p /var/lib/udhcpd
    start-stop-daemon --start --background --make-pidfile --pidfile /run/rodin-udhcpd.pid \
        --exec /bin/busybox -- udhcpd -f /etc/udhcpd.conf
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
            cap=$((top / 100 * ${MAX_FREQ_PERCENT:-100}))
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
done
if [ -n "${BACKLIGHT:-}" ]; then
    for led in /sys/class/leds/lcd-backlight/brightness; do
        [ -w "$led" ] && echo "$BACKLIGHT" > "$led"
    done
fi
throttled=no
critical_seen=0
while :; do
    hot=no
    cool=yes
    critical=no
    readable=0
    for sensor in /sys/class/thermal/thermal_zone*/temp /sys/class/power_supply/battery/temp; do
        [ -r "$sensor" ] || continue
        value=$(cat "$sensor" 2>/dev/null) || continue
        case "$value" in ''|*[!0-9]*) continue;; esac
        readable=$((readable + 1))
        case "$sensor" in
            */battery/temp) value=$((value * 100)); high=$((BAT_HIGH * 1000)); low=$((BAT_LOW * 1000)); limit=$((BAT_CRITICAL * 1000));;
            *) [ "$value" -gt 1000 ] || value=$((value * 1000))
               high=$((SOC_HIGH * 1000)); low=$((SOC_LOW * 1000)); limit=$((SOC_CRITICAL * 1000));;
        esac
        [ "$value" -le "$high" ] || hot=yes
        [ "$value" -lt "$low" ] || cool=no
        [ "$value" -le "$limit" ] || critical=yes
    done
    [ "$readable" -gt 0 ] || cool=no
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
    if [ "$hot" = yes ] && [ "$throttled" = no ]; then
        logger -t rodin-thermal "High temperature; limiting CPU frequencies"
        for policy in /sys/devices/system/cpu/cpufreq/policy*; do
            [ -d "$policy" ] || continue
            minimum=
            for frequency in $(cat "$policy/scaling_available_frequencies" 2>/dev/null); do
                case "$frequency" in ''|*[!0-9]*) continue;; esac
                if [ -z "$minimum" ] || [ "$frequency" -lt "$minimum" ]; then minimum=$frequency; fi
            done
            [ -n "$minimum" ] || minimum=$(cat "$policy/cpuinfo_min_freq" 2>/dev/null)
            case "$minimum" in ''|*[!0-9]*) continue;; esac
            echo "$minimum" > "$policy/scaling_max_freq" 2>/dev/null || logger -t rodin-thermal "Cannot throttle $policy"
        done
        throttled=yes
    elif [ "$cool" = yes ] && [ "$throttled" = yes ]; then
        logger -t rodin-thermal "Temperature recovered; restoring CPU limits"
        for policy in /sys/devices/system/cpu/cpufreq/policy*; do
            [ -d "$policy" ] || continue
            name=${policy##*/}
            cat "$state/$name.max" > "$policy/scaling_max_freq" 2>/dev/null || :
        done
        throttled=no
    fi
    sleep "$INTERVAL"
done
'''
WIFI = '''#!/sbin/openrc-run
description="Experimental Wi-Fi (opt-in only)"
depend() { need dev; after rodin-thermal; }
start() {
    while read -r module rest; do
        case "$module" in ''|\\#*) continue;; esac
        modprobe "$module" || return 1
    done < /etc/rodin/wifi-modules.list
    if [ -x /etc/rodin/wifi-poweron.sh ]; then
        /etc/rodin/wifi-poweron.sh || return 1
    fi
    wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/wpa_supplicant.conf -P /run/rodin-wpa.pid || return 1
    udhcpc -i wlan0 -b -p /run/rodin-udhcpc.pid
}
stop() {
    start-stop-daemon --stop --pidfile /run/rodin-udhcpc.pid
    start-stop-daemon --stop --pidfile /run/rodin-wpa.pid
}
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
    '/etc/udhcpd.conf': 'start 172.16.42.2\nend 172.16.42.2\ninterface usb0\nmax_leases 1\nlease_file /var/lib/udhcpd/leases\npidfile /run/udhcpd.pid\noption subnet 255.255.255.0\n',
    '/etc/wpa_supplicant/wpa_supplicant.conf': 'ctrl_interface=/run/wpa_supplicant\nupdate_config=1\ncountry=RU\n# Add networks locally; Wi-Fi service is not enabled.\n',
    '/etc/rodin/thermal-modules.list': '# Pending hardware-specific thermal module investigation.\n',
    '/etc/rodin/wifi-modules.list': '# Pending hardware-specific Wi-Fi module investigation.\n',
    '/etc/fstab': '# Stage 1 mounts the userdata root read/write; no /dev/root dependency.\ntmpfs /tmp tmpfs nosuid,nodev 0 0\n',
    '/etc/motd': 'EXPERIMENTAL native Linux on rodin. Thermal policy is a safety net only.\nSupervised local use only; wheel has temporary passwordless doas.\n',
    '/etc/rodin/README': 'rodin-dev populates tmpfs /dev with mdev. Stage 1 keeps the watchdog alive.\nrodin-thermal starts in boot; fill thermal-modules.list before hardware use.\nrodin-usbnet offers 172.16.42.2 to the USB host; SSH keys only.\nrodin-wifi is NOT enabled; fill its module list, power hook and network config.\nRoot is already writable: root/fsck services disabled; host fsck validates the image.\nNo hardware validation, suspend or complete thermal management is implied.\n',
}
SCRIPTS = {
    '/etc/init.d/rodin-dev': DEV,
    '/etc/init.d/rodin-usbnet': USBNET,
    '/etc/init.d/rodin-thermal': THERMAL_SERVICE,
    '/etc/init.d/rodin-wifi': WIFI,
    '/usr/local/sbin/rodin-thermald': THERMALD,
    '/etc/rodin/wifi-poweron.sh': '#!/bin/sh\n# Placeholder: no power-on sequence has been validated.\nexit 0\n',
}
RUNLEVELS = {
    'sysinit': ['procfs', 'sysfs', 'rodin-dev', 'syslog', 'klogd'],
    'boot': ['rodin-thermal', 'hostname', 'bootmisc', 'localmount', 'sysctl'],
    'default': ['rodin-usbnet', 'sshd'],
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

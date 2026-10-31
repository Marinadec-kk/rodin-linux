import gzip
import base64
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_rootfs as builder
import rootfs_config as config


class RootfsTests(unittest.TestCase):
    def test_records(self):
        self.assertEqual(builder.records('P:one\nV:1\n\nP:two\nV:2\n'), [{'P': 'one', 'V': '1'}, {'P': 'two', 'V': '2'}])

    def test_gzip_members(self):
        parts = [gzip.compress(b'one'), gzip.compress(b'two')]
        self.assertEqual(list(builder.gzip_members(b''.join(parts))), list(zip(parts, [b'one', b'two'])))
        with self.assertRaises((ValueError, __import__('zlib').error)):
            list(builder.gzip_members(parts[0][:-5]))

    def test_apk_control_and_payload_integrity(self):
        def archive(name, body):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as tar:
                item = tarfile.TarInfo(name)
                item.size = len(body)
                tar.addfile(item, io.BytesIO(body))
            return gzip.compress(stream.getvalue())
        signature = archive('.SIGN.RSA.key', b'signature')
        payload = archive('sbin/apk.static', b'executable')
        control = archive('.PKGINFO', ('datahash = ' + hashlib.sha256(payload).hexdigest() + '\n').encode())
        checksum = 'Q1' + base64.b64encode(hashlib.sha1(control).digest()).decode()
        builder.verify_apk(signature + control + payload, checksum)
        with self.assertRaisesRegex(ValueError, 'payload checksum'):
            builder.verify_apk(signature + control + archive('sbin/apk.static', b'tampered'), checksum)
        with self.assertRaisesRegex(ValueError, 'control checksum'):
            builder.verify_apk(signature + control + payload, 'Q1wrong')

    def test_module_dep(self):
        text = '/vendor/lib/modules/wlan.ko: /vendor/lib/modules/cfg80211.ko\nother.ko:\n'
        self.assertEqual(builder.rewrite_dep(text, 'vendor_dlkm'), 'vendor_dlkm/wlan.ko: vendor_dlkm/cfg80211.ko\nvendor_dlkm/other.ko:\n')
        with self.assertRaises(ValueError):
            builder.rewrite_dep('../escape.ko:', 'system_dlkm')

    def test_package_ownership(self):
        text = 'F:usr/bin\nR:doas\na:0:0:4755\nR:wall\na:0:5:2755\nF:var/empty\nM:0:0:755\n'
        self.assertEqual(builder.package_ownership(text), {'/usr/bin/doas': (0, 0, 0o4755), '/usr/bin/wall': (0, 5, 0o2755), '/var/empty': (0, 0, 0o755)})

    def test_extract_root_symlink_and_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            tar = Path(directory) / 'root.tar'
            with tarfile.open(tar, 'w') as archive:
                entry = tarfile.TarInfo('bin/tool')
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/bin/busybox'
                archive.addfile(entry)
            root = Path(directory) / 'root'
            root.mkdir()
            builder.safe_extract(tar, root)
            self.assertEqual((root / 'bin/tool').readlink(), Path('busybox'))
            with tarfile.open(tar, 'w') as archive:
                entry = tarfile.TarInfo('../escape')
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(tarfile.FilterError):
                builder.safe_extract(tar, root)

    def test_manifest(self):
        value = builder.manifest([{'sha256': 'a'}], 'P:z\nV:1\n\nP:a\nV:2\n', ['effect'], ['warning'], [{'size': 6}])
        self.assertEqual([p['name'] for p in value['packages']], ['a', 'z'])
        self.assertEqual(value['emulated_scripts'], ['effect'])
        self.assertEqual(value['images'][0]['size'], 6)

    def test_config(self):
        self.assertNotRegex(config.INITTAB, r'tty[1-6]')
        # Bring-up: root shell on ttyGS0 is spawned before OpenRC so a stuck boot stays debuggable.
        self.assertLess(config.INITTAB.index('ttyGS0'), config.INITTAB.index('openrc sysinit'))
        # Wi-Fi hardware power-on is enabled; NetworkManager owns association.
        self.assertIn('rodin-wifi', config.RUNLEVELS['default'])
        self.assertIn('rodin-thermal', config.RUNLEVELS['boot'])
        self.assertIn('provide dev dev-mount', config.DEV)
        self.assertNotIn('devtmpfs /dev', config.DEV)
        dhcp = config.FILES['/etc/udhcpd.conf']
        self.assertIn('start 172.16.42.2\nend 172.16.42.2', dhcp)
        self.assertNotRegex(dhcp, r'option (router|dns)')
        self.assertIn('PasswordAuthentication no', config.FILES['/etc/ssh/sshd_config'])
        self.assertIn('PermitRootLogin no', config.FILES['/etc/ssh/sshd_config'])

    def test_shell_syntax(self):
        for name, text in config.SCRIPTS.items():
            with self.subTest(name=name):
                subprocess.run(['sh', '-n'], input=text, text=True, check=True)

    def test_cached_input_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            dl = Path(directory)
            (dl / 'file').write_bytes(b'tampered')
            (dl / 'inputs.json').write_text('[{"file":"file","sha256":"wrong"}]')
            with patch.object(builder, 'DL', dl):
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    builder.prepare()

    def run_thermal_steps(self, samples, *, available=True, tunables=''):
        """Fake sleep advances sensor snapshots and records both policy limits."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policies = []
            for index, multiplier in enumerate((1, 2)):
                policy = root / f'sys/devices/system/cpu/cpufreq/policy{index}'
                policy.mkdir(parents=True)
                values = {'scaling_max_freq': str(2000 * multiplier),
                          'scaling_governor': 'performance',
                          'cpuinfo_min_freq': str(200 * multiplier),
                          'cpuinfo_max_freq': str(2000 * multiplier)}
                if available:
                    values['scaling_available_frequencies'] = ' '.join(
                        str(f * multiplier) for f in (2000, 600, 200, 1400, 400, 1000, 800, 1200, 1600, 600)) + ' bogus'
                for name, value in values.items():
                    (policy / name).write_text(value)
                policies.append(policy)
            sensors = [root / 'sys/class/thermal/thermal_zone0/temp',
                       root / 'sys/class/thermal/thermal_zone1/temp',
                       root / 'sys/class/power_supply/battery/temp']
            for sensor in sensors:
                sensor.parent.mkdir(parents=True, exist_ok=True)

            def set_sample(sample):
                return '; '.join(f'rm -f "{sensor}"' if value is None else
                                 f'printf "%s" "{value}" > "{sensor}"'
                                 for sensor, value in zip(sensors, sample))

            conf = root / 'etc/conf.d/rodin-thermal'
            conf.parent.mkdir(parents=True)
            conf.write_text(config.THERMAL_CONF + tunables)
            backlight = root / 'sys/class/leds/lcd-backlight/brightness'
            backlight.parent.mkdir(parents=True)
            backlight.write_text('100')
            text = config.THERMALD
            for prefix in ['/etc', '/run', '/sys']:
                text = text.replace(prefix + '/', str(root) + prefix + '/')
            text = text.replace('/sbin/poweroff', 'fake_poweroff')
            cases = '\n'.join(f'{i}) {set_sample(sample)};;'
                              for i, sample in enumerate(samples[1:], 1))
            record = '\n'.join(f'cat "{policy}/scaling_max_freq" >> "{root}/limits{i}"'
                               for i, policy in enumerate(policies))
            harness = f'''count=0
logger() {{ echo "$*" >> "{root}/log"; }}
fake_poweroff() {{ echo shutdown >> "{root}/log"; exit 0; }}
sleep() {{
    count=$((count + 1))
    {record}
    case "$count" in
        {cases}
        *) exit 99;;
    esac
}}
{set_sample(samples[0])}
'''
            result = subprocess.run(['sh'], input=harness + text, text=True,
                                    capture_output=True, timeout=10)
            self.assertIn(result.returncode, (0, 99), result.stderr)
            self.assertEqual(result.stderr, '')
            for policy, ceiling in zip(policies, (1400, 2800)):
                self.assertEqual((policy / 'scaling_governor').read_text().strip(), 'schedutil')
                self.assertEqual((root / f'run/rodin-thermal/{policy.name}.max').read_text().strip(), str(ceiling))
            self.assertEqual(backlight.read_text().strip(), '0')
            limits = [[int(v) for v in (root / f'limits{i}').read_text().splitlines()]
                      for i in range(2)]
            log = (root / 'log').read_text() if (root / 'log').exists() else ''
            return limits, log, result.returncode

    def test_thermal_gradual_step_down_to_floor(self):
        limits, log, code = self.run_thermal_steps([('70001', '50000', '400')] * 5)
        self.assertEqual(limits, [[1000, 600, 200, 200, 200], [2000, 1200, 400, 400, 400]])
        self.assertEqual(log.count('kHz;'), 6)
        self.assertIn('policy0 1400->1000 kHz; SoC=70001 battery=40000', log)
        self.assertEqual(code, 99)

    def test_thermal_step_up_to_ceiling(self):
        limits, log, _ = self.run_thermal_steps(
            [('50000', '75000', '400')] * 3 + [('59', '50000', '400')] * 8)
        self.assertEqual(limits[0], [1000, 600, 200, 400, 600, 800, 1000, 1200, 1400, 1400, 1400])
        self.assertEqual(limits[1], [v * 2 for v in limits[0]])
        self.assertEqual(log.count('kHz;'), 18)

    def test_thermal_hold_band_and_threshold_boundaries(self):
        limits, log, _ = self.run_thermal_steps([
            ('75000', '50000', '400'), ('65000', '50000', '400'),
            ('60000', '50000', '400'), ('70000', '50000', '400'),
            ('59000', '50000', '420'), ('59000', '50000', '450')])
        self.assertEqual(limits[0], [1000] * 6)
        self.assertEqual(log.count('kHz;'), 2)

    def test_thermal_missing_frequency_table_fallback(self):
        # Ceiling 1400, minimum 200: default stride = 1200 / 8 = 150.
        limits, _, _ = self.run_thermal_steps(
            [('75000', None, '400')] * 5 + [('50000', None, '400')] * 10,
            available=False)
        self.assertEqual(limits[0], [1100, 800, 500, 200, 200,
                                    350, 500, 650, 800, 950, 1100, 1250, 1400, 1400, 1400])
        self.assertEqual(limits[1], [v * 2 for v in limits[0]])

    def test_thermal_no_readable_sensors_steps_down(self):
        limits, log, _ = self.run_thermal_steps(
            [(None, 'bogus', None)] * 5 + [('50000', None, None), (None, None, None)])
        self.assertEqual(limits[0], [1000, 600, 200, 200, 200, 400, 200])
        self.assertEqual(log.count('No readable temperature sensors'), 2)
        self.assertEqual(log.count('kHz;'), 10)

    def test_thermal_battery_thresholds(self):
        limits, _, _ = self.run_thermal_steps([
            ('50000', None, '451'), ('50000', None, '430'),
            ('50000', None, '419')])
        self.assertEqual(limits[0], [1000, 1000, 1200])

    def test_thermal_critical_counter_resets(self):
        _, log, code = self.run_thermal_steps([
            ('91000', None, None), ('50000', None, None),
            ('91000', None, None), ('91000', None, None),
            ('bogus', None, None)] + [('91', None, None)] * 3)
        self.assertEqual(code, 0)
        self.assertEqual(log.count('critical temperature reading 1/3'), 3)
        self.assertIn('critical temperature reading 3/3', log)
        self.assertIn('shutdown', log)

    def test_thermal_custom_steps(self):
        limits, _, _ = self.run_thermal_steps(
            [('75000', None, None), ('50000', None, None)],
            tunables='STEP_DOWN=1\nSTEP_UP=2\n')
        self.assertEqual(limits[0], [1200, 1400])
        limits, _, _ = self.run_thermal_steps(
            [('75000', None, None), ('50000', None, None)], available=False,
            tunables='STEP_DOWN=1\nSTEP_UP=1\nSTEP_COUNT=4\n')
        self.assertEqual(limits[0], [1100, 1400])

    def test_thermal_hysteresis_and_critical_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / 'sys/devices/system/cpu/cpufreq/policy0'
            policy.mkdir(parents=True)
            for name, value in {'scaling_max_freq': '2000', 'scaling_governor': 'performance', 'scaling_available_frequencies': '2000 500 1000 1400', 'cpuinfo_min_freq': '500', 'cpuinfo_max_freq': '2000'}.items():
                (policy / name).write_text(value)
            sensor = root / 'sys/class/thermal/thermal_zone0/temp'
            sensor.parent.mkdir(parents=True)
            sensor.write_text('70001')
            battery = root / 'sys/class/power_supply/battery/temp'
            battery.parent.mkdir(parents=True)
            battery.write_text('400')
            conf = root / 'etc/conf.d/rodin-thermal'
            conf.parent.mkdir(parents=True)
            conf.write_text(config.THERMAL_CONF)
            text = config.THERMALD
            for prefix in ['/etc', '/run', '/sys']:
                text = text.replace(prefix + '/', str(root) + prefix + '/')
            text = text.replace('/sbin/poweroff', 'fake_poweroff')
            harness = f'''count=0
logger() {{ printf '%s\\n' "$*" >> "{root}/log"; }}
fake_poweroff() {{ echo shutdown >> "{root}/log"; exit 0; }}
sleep() {{
    count=$((count + 1))
    cat "{policy}/scaling_max_freq" >> "{root}/limits"
    case "$count" in
        1) echo 65000 > "{sensor}";;
        2) rm "{sensor}" "{battery}";;
        3) echo 59000 > "{sensor}"; echo 400 > "{battery}";;
        4) echo 91000 > "{sensor}";;
        5|6) ;;
        *) exit 99;;
    esac
}}
'''
            subprocess.run(['sh'], input=harness + text, text=True, check=True, timeout=10)
            # Boot cap 1400; two entries down reaches 500. Cooling raises only
            # one entry to 1000; critical shutdown still needs three samples.
            self.assertEqual((root / 'limits').read_text().splitlines(), ['500', '500', '500', '1000', '500', '500'])
            self.assertEqual((root / 'log').read_text().count('critical temperature reading'), 3)
            self.assertEqual((policy / 'scaling_governor').read_text().strip(), 'schedutil')
            self.assertIn('shutdown', (root / 'log').read_text())

    def test_thermal_battery_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'etc/conf.d/rodin-thermal'
            conf.parent.mkdir(parents=True)
            conf.write_text(config.THERMAL_CONF)
            battery = root / 'sys/class/power_supply/battery/temp'
            battery.parent.mkdir(parents=True)
            battery.write_text('551')
            text = config.THERMALD
            for prefix in ['/etc', '/run', '/sys']:
                text = text.replace(prefix + '/', str(root) + prefix + '/')
            text = text.replace('/sbin/poweroff', 'fake_poweroff')
            harness = f'''count=0
logger() {{ echo "$*" >> "{root}/log"; }}
fake_poweroff() {{ echo shutdown >> "{root}/log"; exit 0; }}
sleep() {{ count=$((count + 1)); [ "$count" -lt 3 ] || exit 99; }}
'''
            subprocess.run(['sh'], input=harness + text, text=True, check=True, timeout=10)
            self.assertIn('CRITICAL temperature', (root / 'log').read_text())
            self.assertIn('shutdown', (root / 'log').read_text())

    def test_single_bogus_critical_reading_does_not_power_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'etc/conf.d/rodin-thermal'
            conf.parent.mkdir(parents=True)
            conf.write_text(config.THERMAL_CONF)
            sensor = root / 'sys/class/thermal/thermal_zone0/temp'
            sensor.parent.mkdir(parents=True)
            sensor.write_text('126000')
            text = config.THERMALD
            for prefix in ['/etc', '/run', '/sys']:
                text = text.replace(prefix + '/', str(root) + prefix + '/')
            text = text.replace('/sbin/poweroff', 'fake_poweroff')
            harness = f'''count=0
logger() {{ echo "$*" >> "{root}/log"; }}
fake_poweroff() {{ echo shutdown >> "{root}/log"; exit 0; }}
sleep() {{ count=$((count + 1)); echo 50000 > "{sensor}"; [ "$count" -lt 4 ] || exit 99; }}
'''
            result = subprocess.run(['sh'], input=harness + text, text=True, timeout=10)
            self.assertEqual(result.returncode, 99)
            self.assertNotIn('shutdown', (root / 'log').read_text())


if __name__ == '__main__':
    unittest.main()

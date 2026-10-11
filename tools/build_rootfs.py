#!/usr/bin/env python3
"""Host-only Alpine rootfs builder; reuse verified downloads for subsequent builds."""
import argparse
import base64
import hashlib
import io
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zlib

sys.dont_write_bytecode = True
from rootfs_config import SCRIPTS, RUNLEVELS, with_inputs

# dl-cdn measured ~230 KB/s from the build host; dotsrc ~8 MB/s (2026-10-11).
BASE = 'https://mirrors.dotsrc.org/alpine/v3.23'
RELEASE = '6.6.89-android15-8-g8e4be6b47e40-ab14134548-4k'
REPO = Path(__file__).resolve().parents[1]
DL = REPO / 'build/rootfs-dl'
OUT = REPO / 'build/rootfs'
ROOT = OUT / 'root'
PACKAGES = 'alpine-base openrc busybox-mdev-openrc openssh wpa_supplicant iw wireless-regdb kmod e2fsprogs e2fsprogs-extra util-linux-misc doas ca-certificates tzdata nano htop iproute2 bash procps-ng ethtool busybox-extras seatd'.split()


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def records(text):
    return [dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
            for block in text.strip().split('\n\n')]


def gzip_members(data):
    while data:
        decoder = zlib.decompressobj(31)
        plain = decoder.decompress(data) + decoder.flush()
        if not decoder.eof:
            raise ValueError('truncated gzip member')
        used = len(data) - len(decoder.unused_data)
        yield data[:used], plain
        data = decoder.unused_data


def tar_files(data):
    result = {}
    for _, plain in gzip_members(data):
        with tarfile.open(fileobj=io.BytesIO(plain)) as archive:
            for entry in archive:
                if entry.isfile():
                    result[entry.name.lstrip('./')] = archive.extractfile(entry).read()
    return result


def verify_apk(data, checksum):
    members = list(gzip_members(data))
    expected = 'Q1' + base64.b64encode(hashlib.sha1(members[1][0]).digest()).decode()
    if expected != checksum:
        raise ValueError('apk control checksum mismatch against APKINDEX')
    metadata = tar_files(data)['PKGINFO'].decode()
    expected_data = re.search(r'^datahash = ([0-9a-f]{64})$', metadata, re.M)
    if not expected_data or hashlib.sha256(members[2][0]).hexdigest() != expected_data[1]:
        raise ValueError('apk payload checksum mismatch')


def download(url, path):
    with urllib.request.urlopen(url, timeout=120) as response, path.open('wb') as target:
        shutil.copyfileobj(response, target)
    return {'url': url, 'file': path.name, 'sha256': sha256(path)}


def prepare():
    lock = DL / 'inputs.json'
    if lock.exists():
        inputs = json.loads(lock.read_text())
        for item in inputs:
            if sha256(DL / item['file']) != item['sha256']:
                raise ValueError('cached input checksum mismatch: ' + item['file'])
        return inputs
    if DL.exists() and any(DL.iterdir()):
        raise ValueError('first download requires an empty build/rootfs-dl')
    DL.mkdir(parents=True, exist_ok=True)
    inputs = [download(BASE + '/releases/aarch64/latest-releases.yaml', DL / 'latest-releases.yaml')]
    text = (DL / 'latest-releases.yaml').read_text()
    names = re.findall(r'file: (alpine-minirootfs-(3\.23\.\d+)-aarch64\.tar\.gz)', text)
    name, _ = max(names, key=lambda x: tuple(map(int, x[1].split('.'))))
    inputs.append(download(BASE + '/releases/aarch64/' + name + '.sha256', DL / (name + '.sha256')))
    inputs.append(download(BASE + '/releases/aarch64/' + name, DL / name))
    expected = (DL / (name + '.sha256')).read_text().split()[0]
    if inputs[-1]['sha256'] != expected:
        raise ValueError('minirootfs checksum mismatch')
    inputs.append(download(BASE + '/main/x86_64/APKINDEX.tar.gz', DL / 'APKINDEX.tar.gz'))
    index = records(tar_files((DL / 'APKINDEX.tar.gz').read_bytes())['APKINDEX'].decode())
    package = next(p for p in index if p['P'] == 'apk-tools-static')
    name = f"apk-tools-static-{package['V']}.apk"
    inputs.append(download(BASE + '/main/x86_64/' + name, DL / name))
    verify_apk((DL / name).read_bytes(), package['C'])
    inputs[-1]['apkindex_checksum'] = package['C']
    (DL / 'apk.static').write_bytes(tar_files((DL / name).read_bytes())['sbin/apk.static'])
    (DL / 'apk.static').chmod(0o755)
    inputs.append({'url': 'extracted from ' + name, 'file': 'apk.static', 'sha256': sha256(DL / 'apk.static')})
    signer = next(x for x in tar_files((DL / name).read_bytes()) if x.startswith('SIGN.RSA.')).removeprefix('SIGN.RSA.')
    if not re.fullmatch(r'alpine-devel@lists\.alpinelinux\.org-[0-9a-f]+\.rsa\.pub', signer):
        raise ValueError('unexpected Alpine signing key name')
    (DL / 'keys').mkdir()
    key_input = download('https://alpinelinux.org/keys/' + signer, DL / 'keys' / signer)
    key_input['file'] = 'keys/' + signer
    inputs.append(key_input)
    run(DL / 'apk.static', '--keys-dir', DL / 'keys', 'verify', DL / name)
    lock.write_text(json.dumps(inputs, indent=2) + '\n')
    return inputs


def safe_extract(path, destination):
    with tarfile.open(path) as archive:
        def root_filter(entry, target):
            if entry.issym() and entry.linkname.startswith('/'):
                entry = entry.replace(linkname=posixpath.relpath(entry.linkname.lstrip('/'), posixpath.dirname(entry.name)))
            return tarfile.data_filter(entry, target)
        archive.extractall(destination, filter=root_filter)


def rewrite_dep(text, source):
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        module, dependencies = line.split(':', 1)
        def rewrite(value):
            name = value.rsplit('/lib/modules/', 1)[-1].lstrip('/')
            if '..' in PurePosixPath(name).parts:
                raise ValueError('unsafe module path')
            return source + '/' + name
        lines.append(rewrite(module) + ':' + ''.join(' ' + rewrite(x) for x in dependencies.split()))
    return '\n'.join(lines) + '\n'


def put(name, text, mode=0o644):
    path = ROOT / name.lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def link(name, target):
    path = ROOT / name.lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    path.symlink_to(target)


def package_ownership(text):
    result = {}
    directory = ''
    path = None
    for line in text.splitlines():
        if line.startswith('F:'):
            directory = line[2:]
            path = '/' + directory
        elif line.startswith('R:'):
            path = '/' + directory + '/' + line[2:]
        elif line.startswith(('a:', 'M:')) and path:
            uid, gid, mode = line[2:].split(':')
            result[path] = (int(uid), int(gid), int(mode, 8))
    return result


def manifest(inputs, installed, scripts, warnings, images):
    return {'alpine_branch': 'v3.23', 'architecture': 'aarch64', 'kernel_release': RELEASE,
            'inputs': inputs, 'packages': sorted([{'name': p['P'], 'version': p['V']} for p in records(installed)], key=lambda p: p['name']),
            'emulated_scripts': scripts, 'warnings': warnings, 'images': images,
            'rebuild': 'python3 tools/build_rootfs.py (preserve rootfs-dl; move existing rootfs aside)',
            'reproducibility': 'Pinned inputs/package cache; filesystem UUID, timestamps and SSH host keys are not bit-reproducible.'}


def configure(inputs):
    warnings = ['Single-ID user namespace: tree is mapped-root owned; nonzero package/user ownership is applied to ext4 with debugfs.',
                'Thermal and Wi-Fi module lists are placeholders. No hardware validation; empty thermal list does not provide thermal drivers.',
                'Temporary passwordless doas for wheel. Root and user passwords locked.',
                'OpenSSH may reject a fully locked account even for public keys; target login remains unverified.']
    installed = (ROOT / 'lib/apk/db/installed').read_text()
    ownership = package_ownership(installed)
    for path, text in with_inputs(REPO / 'build/rootfs-inputs').items():
        put(path, text)
    for path, text in SCRIPTS.items():
        put(path, text, 0o755)
    # Boot log on disk: the only record when no console is attached.
    rc_conf = ROOT / 'etc/rc.conf'
    text = rc_conf.read_text()
    if 'rc_logger="YES"' not in text:
        rc_conf.write_text(text + '\n# rodin: keep an OpenRC boot log\nrc_logger="YES"\nrc_log_path="/var/log/rc.log"\n')
    # rc_need adds dependencies; it does not override the service's depend() function.
    for name, old, new in [('localmount', 'need fsck root', 'need dev'), ('syslog', 'need clock hostname localmount', 'need dev hostname localmount'), ('klogd', 'need clock hostname localmount logger', 'need dev hostname localmount logger')]:
        path = ROOT / 'etc/init.d' / name
        text = path.read_text()
        if text.count(old) != 1:
            raise ValueError('unexpected Alpine service dependencies: ' + name)
        path.write_text(text.replace(old, new))
    for name in ['devfs', 'mdev']:
        (ROOT / 'etc/init.d' / name).chmod(0o644)
    (ROOT / 'etc/runlevels').mkdir(exist_ok=True)
    for old in (ROOT / 'etc/runlevels').glob('*/*'):
        if old.is_symlink():
            old.unlink()
    for level, services in RUNLEVELS.items():
        for service in services:
            link('/etc/runlevels/' + level + '/' + service, '../../init.d/' + service)
    link('/etc/localtime', '../usr/share/zoneinfo/UTC')
    group_lines = (ROOT / 'etc/group').read_text().splitlines()
    names = {line.split(':')[0] for line in group_lines}
    for name, gid in [('netdev', 28), ('klogd', 31), ('marinadec', 1000)]:
        if name not in names:
            if any(int(line.split(':')[2]) == gid for line in group_lines):
                raise ValueError('group ID collision')
            group_lines.append(f'{name}:x:{gid}:')
    for i, line in enumerate(group_lines):
        fields = line.split(':')
        if fields[0] in ('wheel', 'video', 'audio', 'input', 'netdev'):
            fields[3] = ','.join(filter(None, [fields[3], 'marinadec']))
            group_lines[i] = ':'.join(fields)
    put('/etc/group', '\n'.join(group_lines) + '\n')
    passwd = (ROOT / 'etc/passwd').read_text()
    passwd += 'marinadec:x:1000:1000:marinadec:/home/marinadec:/bin/bash\n'
    if 'klogd:' not in passwd:
        passwd += 'klogd:x:31:31:klogd:/dev/null:/sbin/nologin\n'
    put('/etc/passwd', passwd)
    shadow = (ROOT / 'etc/shadow').read_text().splitlines()
    shadow = [(':'.join([line.split(':')[0], '!', *line.split(':')[2:]]) if line.startswith('root:') else line) for line in shadow]
    # '*' = no password but not locked (OpenSSH refuses '!' even for keys); no forced change.
    shadow += ['marinadec:*:20372:0:99999:7:::', 'klogd:!:0:0:99999:7:::']
    put('/etc/shadow', '\n'.join(shadow) + '\n', 0o640)
    ownership['/etc/shadow'] = (0, 42, 0o640)
    shells = (ROOT / 'etc/shells').read_text()
    if '/bin/bash' not in shells.splitlines():
        put('/etc/shells', shells + '/bin/bash\n')
    key = REPO / 'build/secrets/authorized_keys'
    if not key.is_file() or not key.read_text().strip():
        raise ValueError('missing/empty authorized_keys')
    put('/home/marinadec/.ssh/authorized_keys', key.read_text(), 0o600)
    for name, mode in [('/home/marinadec', 0o755), ('/home/marinadec/.ssh', 0o700), ('/home/marinadec/.ssh/authorized_keys', 0o600)]:
        (ROOT / name.lstrip('/')).chmod(mode)
        ownership[name] = (1000, 1000, mode)
    (ROOT / 'var/empty').mkdir(exist_ok=True)
    (ROOT / 'var/empty').chmod(0o755)
    ownership['/var/empty'] = (0, 0, 0o755)
    with (ROOT / 'etc/mdev.conf').open('a') as f:
        f.write('\nttyGS[0-9]+ root:dialout 0660\nwatchdog[0-9]+ root:root 0600\n')
    # iproute2's skipped post-install removes the BusyBox link, which can shadow full ip.
    link('/bin/ip', '../sbin/ip')
    if not (ROOT / 'etc/ssl/certs/ca-certificates.crt').is_file():
        raise ValueError('CA certificate bundle absent')
    scripts = []
    script_archive = ROOT / 'lib/apk/db/scripts.tar'
    if not script_archive.exists():
        script_archive = ROOT / 'lib/apk/db/scripts.tar.gz'
    with tarfile.open(script_archive) as archive:
        for entry in archive:
            if not entry.isfile():
                continue
            body = archive.extractfile(entry).read()
            effect = 'Not applicable: upgrade/deinstall script on a fresh image.'
            if entry.name.endswith(('.pre-install', '.post-install', '.trigger')):
                effect = 'No fresh-image action required; inspected and recorded.'
                if entry.name.startswith('alpine-baselayout-'):
                    effect = 'shadow group 42 already present; shadow mode/ownership fixed in image; obsolete color prompt link absent.'
                elif entry.name.startswith('busybox-'):
                    effect = 'Minirootfs applet links retained; klogd user/group added; kmod host depmod replaces module trigger; bbsuid links installed separately.'
                elif entry.name.startswith('bash-'):
                    effect = '/bin/bash added to /etc/shells.'
                elif entry.name.startswith('ca-certificates-'):
                    effect = 'Existing ca-certificates-bundle retained; individual certificate/hash links generated on host.'
                elif entry.name.startswith('iproute2-'):
                    effect = '/bin/ip points to packaged /sbin/ip, not BusyBox.'
                elif entry.name.startswith('kmod-'):
                    effect = 'Host depmod generates combined module indexes.'
                elif entry.name.startswith('openrc-'):
                    effect = 'No legacy rcS/rcL migration; runlevel links configured explicitly.'
            scripts.append({'script': entry.name, 'sha256': hashlib.sha256(body).hexdigest(), 'effect': effect})
    for certificate in sorted((ROOT / 'usr/share/ca-certificates').rglob('*.crt')):
        name = certificate.stem + '.pem'
        link('/etc/ssl/certs/' + name, posixpath.relpath('/' + str(certificate.relative_to(ROOT)), '/etc/ssl/certs'))
        digest = run('openssl', 'x509', '-hash', '-noout', '-in', certificate, capture_output=True, text=True).stdout.strip()
        suffix = 0
        while (ROOT / f'etc/ssl/certs/{digest}.{suffix}').is_symlink():
            suffix += 1
        link(f'/etc/ssl/certs/{digest}.{suffix}', name)
    qemu = REPO / 'build/host-tools/bin/qemu-aarch64'
    # bbsuid --install touches absolute host paths: reproduce its eight packaged applet links instead.
    for path in ['bin/mount', 'bin/umount', 'bin/su', 'usr/bin/crontab', 'usr/bin/passwd', 'usr/bin/traceroute', 'usr/bin/traceroute6', 'usr/bin/vlock']:
        link('/' + path, posixpath.relpath('bin/bbsuid', posixpath.dirname(path)))
    for path, (_, _, mode) in ownership.items():
        node = ROOT / path.lstrip('/')
        if node.is_file() and not node.is_symlink() and path not in ('/etc/init.d/devfs', '/etc/init.d/mdev'):
            node.chmod(mode)
    sources = {
        'vendor_dlkm': REPO / 'build/inspection/dlkm/vendor_dlkm/lib/modules',
        'system_dlkm': REPO / 'build/inspection/dlkm/system_dlkm/lib/modules',
        'odm_dlkm': REPO / 'build/ota-extra/tree/odm_dlkm/lib/modules',
    }
    module_root = ROOT / 'lib/modules' / RELEASE
    module_root.mkdir(parents=True)
    local_inputs = [{'file': str(key.relative_to(REPO)), 'sha256': sha256(key)}]
    if not sources['odm_dlkm'].exists():
        erofs = REPO / 'build/host-tools/erofs'
        image = REPO / 'build/ota-extra/odm_dlkm.img'
        if (erofs / 'usr/bin/fsck.erofs').exists() and image.exists():
            env = dict(os.environ, LD_LIBRARY_PATH=str(erofs / 'lib'))
            extracted = OUT / 'odm-extracted'
            extraction = subprocess.run([str(erofs / 'usr/bin/fsck.erofs'), '--extract=' + str(extracted), str(image)], env=env, capture_output=True, text=True)
            (OUT / 'odm-extraction.log').write_text(extraction.stdout + extraction.stderr)
            local_inputs.append({'file': str(image.relative_to(REPO)), 'sha256': sha256(image)})
            if not extraction.returncode:
                sources['odm_dlkm'] = extracted / 'lib/modules'
    for name, source in sources.items():
        if not source.exists():
            warnings.append(name + ' has no lib/modules tree; skipped (existing/extracted EROFS contains no modules, or extraction unavailable).')
            continue
        shutil.copytree(source, module_root / name, symlinks=True)
        local_inputs += [{'file': str(p.relative_to(REPO)), 'sha256': sha256(p)} for p in sorted(source.rglob('*')) if p.is_file() and not p.is_symlink()]
    depmod = subprocess.run(['depmod', '-b', str(ROOT), RELEASE], capture_output=True, text=True)
    (OUT / 'depmod.log').write_text(depmod.stdout + depmod.stderr)
    if depmod.stderr.strip():
        warnings.append('depmod diagnostics recorded in depmod.log: modules.order/builtin metadata may be absent.')
    if depmod.returncode:
        warnings.append('host depmod failed; using source modules.dep rewriting (no binary/alias generation).')
        combined = ''.join(rewrite_dep((module_root / name / 'modules.dep').read_text(), name) for name in sources if (module_root / name / 'modules.dep').exists())
        if not combined:
            raise ValueError('no usable module dependency data')
        (module_root / 'modules.dep').write_text(combined)
    resolution = run('modprobe', '-n', '-v', '-d', ROOT, '-S', RELEASE, 'cfg80211', capture_output=True, text=True).stdout
    if 'cfg80211.ko' not in resolution:
        raise ValueError('cfg80211 dry-run did not resolve module')
    (OUT / 'modprobe-cfg80211.log').write_text(resolution)
    for source, target in [('firmware', '/lib/firmware'), ('vendor-etc', '/vendor/etc')]:
        origin = REPO / 'build/rootfs-inputs' / source
        shutil.copytree(origin, ROOT / target.lstrip('/'), dirs_exist_ok=True, symlinks=True)
        local_inputs += [{'file': str(p.relative_to(REPO)), 'sha256': sha256(p)} for p in sorted(origin.rglob('*')) if p.is_file() and not p.is_symlink()]
    link('/vendor/firmware', '/lib/firmware')
    link('/odm/firmware', '/lib/firmware')
    validate(qemu)
    image = OUT / 'rootfs.img'
    sparse = OUT / 'rootfs.simg'
    run('mke2fs', '-t', 'ext4', '-L', 'rodin-root', '-d', ROOT, '-E', 'root_owner=0:0', image, '6G')
    commands = []
    for path, (uid, gid, mode) in sorted(ownership.items()):
        node = ROOT / path.lstrip('/')
        if not node.exists() and not node.is_symlink():
            continue
        if uid or gid:
            commands.extend([f'set_inode_field "{path}" uid {uid}', f'set_inode_field "{path}" gid {gid}'])
    fixups = OUT / 'ownership.debugfs'
    fixups.write_text('\n'.join(commands) + '\n')
    result = run('debugfs', '-w', '-f', fixups, image, capture_output=True, text=True)
    (OUT / 'ownership.log').write_text(result.stdout + result.stderr)
    checks = [('/etc/shadow', 0, 42, 0o640), ('/home/marinadec/.ssh', 1000, 1000, 0o700), ('/home/marinadec/.ssh/authorized_keys', 1000, 1000, 0o600), ('/usr/bin/doas', 0, 0, 0o4755), ('/var/empty', 0, 0, 0o755), ('/usr/bin/wall', 0, 5, 0o2755)]
    stats = []
    for path, uid, gid, mode in checks:
        stat = run('debugfs', '-R', 'stat ' + path, image, capture_output=True, text=True).stdout
        actual = re.search(r'User:\s*(\d+)\s+Group:\s*(\d+)', stat)
        actual_mode = re.search(r'Mode:\s*0?([0-7]+)', stat)
        if not actual or tuple(map(int, actual.groups())) != (uid, gid) or not actual_mode or int(actual_mode[1], 8) != mode:
            raise ValueError('image ownership/mode mismatch: ' + path + '\n' + stat)
        stats.append(path + '\n' + stat)
    (OUT / 'image-stat.log').write_text('\n'.join(stats))
    result = run('e2fsck', '-fn', image, capture_output=True, text=True)
    (OUT / 'e2fsck.log').write_text(result.stdout + result.stderr)
    run('img2simg', image, sparse)
    images = [{'path': str(p.relative_to(REPO)), 'size': p.stat().st_size, 'sha256': sha256(p)} for p in [image, sparse]]
    inputs += json.loads((DL / 'packages.json').read_text()) + local_inputs
    sshd_log = (OUT / 'sshd-smoke.log').read_text()
    if sshd_log.strip():
        warnings.append('QEMU sshd -t did not complete: ' + sshd_log.strip())
    result = manifest(inputs, installed, scripts, warnings, images)
    result['disabled_services'] = {'devfs/mdev': 'Alpine scripts retained non-executable; rodin-dev provides dev/dev-mount', 'root/fsck': 'stage 1 supplies writable root; localmount depend() changed to dev', 'hwclock/swclock': 'no validated RTC; UTC configured, no clock provider dependency'}
    (OUT / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(images, indent=2))


def validate(qemu):
    init = ROOT / 'sbin/init'
    if not init.is_symlink() or 'busybox' not in os.readlink(init):
        raise ValueError('init is not BusyBox')
    if re.search(r'tty[1-6]', (ROOT / 'etc/inittab').read_text()):
        raise ValueError('VT getty configured')
    for level, services in RUNLEVELS.items():
        for service in services:
            if not (ROOT / 'etc/runlevels' / level / service).is_symlink() or not (ROOT / 'etc/init.d' / service).is_file():
                raise ValueError('invalid runlevel service')
    for name, expected in with_inputs(REPO / 'build/rootfs-inputs').items():
        if (ROOT / name.lstrip('/')).read_text() != expected:
            raise ValueError('configuration mismatch: ' + name)
    passwd = {line.split(':')[0]: line.split(':') for line in (ROOT / 'etc/passwd').read_text().splitlines()}
    if passwd['marinadec'][2:4] != ['1000', '1000'] or passwd['marinadec'][5:] != ['/home/marinadec', '/bin/bash']:
        raise ValueError('invalid user account')
    shadow = {line.split(':')[0]: line.split(':')[1] for line in (ROOT / 'etc/shadow').read_text().splitlines()}
    if shadow['root'] != '!' or shadow['marinadec'] != '*':
        raise ValueError('passwords not locked')
    groups = {line.split(':')[0]: line.split(':') for line in (ROOT / 'etc/group').read_text().splitlines()}
    if groups['marinadec'][2] != '1000' or any('marinadec' not in groups[name][3].split(',') for name in ['wheel', 'video', 'audio', 'input', 'netdev']):
        raise ValueError('invalid user groups')
    for name, mode in [('/etc/shadow', 0o640), ('/home/marinadec/.ssh', 0o700), ('/home/marinadec/.ssh/authorized_keys', 0o600)]:
        if (ROOT / name.lstrip('/')).stat().st_mode & 0o7777 != mode:
            raise ValueError('invalid protected file mode: ' + name)
    for name in [*SCRIPTS, '/etc/init.d/localmount', '/etc/init.d/syslog', '/etc/init.d/klogd']:
        path = ROOT / name.lstrip('/')
        run('sh', '-n', path)
        run(qemu, '-L', ROOT, ROOT / 'bin/busybox', 'sh', '-n', path)
    run(qemu, '-L', ROOT, ROOT / 'bin/busybox', 'true')
    scratch = Path('/tmp/claude-1000/x/pi')
    scratch.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        key = Path(directory) / 'hostkey'
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key)
        # QEMU -L is not a chroot: use an explicit privilege-separation directory for this host check.
        result = subprocess.run([str(qemu), '-L', str(ROOT), '-E', 'LD_LIBRARY_PATH=' + str(ROOT / 'lib') + ':' + str(ROOT / 'usr/lib'), str(ROOT / 'usr/sbin/sshd'), '-t', '-f', str(ROOT / 'etc/ssh/sshd_config'), '-h', str(key)], capture_output=True, text=True)
        (OUT / 'sshd-smoke.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            if 'Privilege separation user sshd does not exist' not in result.stderr and 'Missing privilege separation directory' not in result.stderr:
                raise RuntimeError('sshd smoke failed: ' + result.stderr)
            print('WARNING: sshd -t cannot use target passwd/paths under QEMU -L: ' + result.stderr.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--namespace', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.chdir(REPO)
    inputs = prepare()
    if args.download_only:
        return
    if not args.namespace:
        unshare = shutil.which('unshare') or '/usr/bin/unshare'
        run(unshare, '-r', sys.executable, Path(__file__).resolve(), '--namespace')
        return
    if os.getuid() != 0 or Path('/proc/self/uid_map').read_text().split()[1] == '0':
        raise RuntimeError('requires an unprivileged mapped-root user namespace')
    if ROOT.exists():
        raise RuntimeError('refusing to overwrite existing root tree; move build/rootfs aside first')
    ROOT.mkdir(parents=True)
    tarball = next(DL / x['file'] for x in inputs if x['file'].endswith('.tar.gz') and x['file'].startswith('alpine-minirootfs'))
    safe_extract(tarball, ROOT)
    cache = DL / 'packages'
    cache.mkdir(exist_ok=True)
    package_lock = DL / 'packages.json'
    extra = ['--cache-dir', cache, '--cache-packages']
    packages = PACKAGES
    if package_lock.exists():
        pinned = json.loads(package_lock.read_text())
        for item in pinned:
            if sha256(cache / item['file']) != item['sha256']:
                raise ValueError('cached package checksum mismatch')
        extra += ['--no-network']
        packages = [cache / item['file'] for item in pinned]
    run(DL / 'apk.static', '--arch', 'aarch64', '--root', ROOT, '--no-scripts', '--no-chown', '--repository', BASE + '/main', '--repository', BASE + '/community', '--keys-dir', ROOT / 'etc/apk/keys', *extra, 'add', *packages)
    installed_names = {p['P'] for p in records((ROOT / 'lib/apk/db/installed').read_text())}
    if not set(PACKAGES) <= installed_names:
        raise ValueError('requested packages missing: ' + ', '.join(sorted(set(PACKAGES) - installed_names)))
    if not package_lock.exists():
        # apk does not cache data-less metapackages, but the offline rebuild still needs them.
        run(DL / 'apk.static', '--arch', 'aarch64', '--root', ROOT, '--keys-dir', ROOT / 'etc/apk/keys', '--repository', BASE + '/main', 'fetch', '--output', cache, 'alpine-base=' + next(p['V'] for p in records((ROOT / 'lib/apk/db/installed').read_text()) if p['P'] == 'alpine-base'))
        package_repos = {}
        for repository in ['main', 'community']:
            index_path = DL / (repository + '-aarch64-APKINDEX.tar.gz')
            inputs.append(download(BASE + '/' + repository + '/aarch64/APKINDEX.tar.gz', index_path))
            for package in records(tar_files(index_path.read_bytes())['APKINDEX'].decode()):
                package_repos[package['P'] + '-' + package['V'] + '.apk'] = repository
        pinned = []
        for p in sorted(cache.glob('*.apk')):
            filename = re.sub(r'\.[0-9a-f]{8}\.apk$', '.apk', p.name)
            pinned.append({'file': p.name, 'sha256': sha256(p), 'url': BASE + '/' + package_repos[filename] + '/aarch64/' + filename})
        package_lock.write_text(json.dumps(pinned, indent=2) + '\n')
        (DL / 'inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
    configure(inputs)


if __name__ == '__main__':
    main()

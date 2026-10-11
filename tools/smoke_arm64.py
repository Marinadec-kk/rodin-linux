#!/usr/bin/env python3
"""Run pinned ARM64 BusyBox under user-mode QEMU; never execute init as PID 1.

The PTY probe tests ARM getty -> native host launcher -> ARM interactive shell.
It does NOT emulate the phone kernel, module loading, configfs or physical USB.
"""
import argparse
import hashlib
import os
from pathlib import Path
import pty
import re
import select
import shlex
import subprocess
import tempfile
import time

import build_initramfs as ramfs
import build_candidate as candidate

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"ash", "sh", "mount", "insmod", "sha256sum", "setsid", "getty", "mknod", "sleep", "mkdir", "ln"}


def pty_probe(qemu, binary, work):
    login = work / "login-probe"
    # Forward getty's exact login arguments to ash, as /bin/sh receives them on the phone.
    login.write_text("#!/bin/sh\nexport PS1='RODIN-GETTY-READY> '\nexport HOME="
                     + shlex.quote(str(work)) + "\nunset ENV BASH_ENV\nexec "
                     + shlex.quote(str(qemu)) + " " + shlex.quote(str(binary)) + ' sh "$@"\n')
    login.chmod(0o755)
    master, slave = pty.openpty()
    child = None
    try:
        environment = dict(os.environ, HOME=str(work), ENV="", BASH_ENV="",
                           PS1="RODIN-GETTY-READY> ")
        child = subprocess.Popen([str(qemu), str(binary), "getty", "-L", "-n", "-i", "-l",
                                  str(login), "115200", os.ttyname(slave), "vt100"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True, env=environment)
        data, sent, deadline = b"", False, time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                data += os.read(master, 4096)
                if b"RODIN-GETTY-READY" in data and not sent:
                    os.write(master, b"printf 'RODIN-GETTY-OK\\n'; exit\n")
                    sent = True
                # Do not mistake echoed input containing the marker for shell execution.
                if b"\r\nRODIN-GETTY-OK\r\n" in data:
                    return
            if child.poll() is not None:
                break
        raise ValueError("ARM getty/shell PTY probe failed: " + repr(data))
    finally:
        if child:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
        os.close(master)
        os.close(slave)


def checksum_probe(command, listing, expected_success):
    result = subprocess.run(command + ["sha256sum", "-c", str(listing)], capture_output=True)
    if (result.returncode == 0) != expected_success:
        raise ValueError("Unexpected ARM sha256sum result: " + repr(result))


def characterize(command, work):
    payload = work / "checksum-payload"
    payload.write_bytes(b"ARM checksum fixture\n")
    listing = work / "checksums"
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    for checksum, path, success in ((digest, payload, True), ("0" * 64, payload, False),
                                    (digest, work / "absent", False)):
        listing.write_text(f"{checksum}  {path}\n")
        checksum_probe(command, listing, success)
    result = subprocess.check_output(command + ["ash", "-c",
        'while read -r module name; do printf "<%s>|<%s>\\n" "$module" "$name"; done'],
        input=b"\n/path\n/path name extra\n/path name\r\n")
    if result != b"<>|<>\n</path>|<>\n</path>|<name extra>\n</path>|<name\r>\n":
        raise ValueError("Unexpected ARM ash read semantics: " + repr(result))
    script = (ROOT / "initramfs" / "usb-init").read_text()
    rule = next(line.strip() for line in script.splitlines() if 'case "$major:$minor"' in line)
    for value, valid in (("4:1", True), ("123:456", True), ("", False), ("x:y", False),
                         ("4:", False), (":1", False), ("4:1:", False), ("4:1:2", False)):
        major, _, minor = value.partition(":")
        result = subprocess.run(command + ["ash", "-c",
            'major=$1; minor=$2; validate() { ' + rule + '; }; validate', "probe", major, minor])
        if (result.returncode == 0) != valid:
            raise ValueError("Unexpected tty device-number validation: " + repr(value))


def module_checksum_probe(command, work, build_dir):
    vendor = build_dir / "ota" / "vendor_boot.img"
    listing = build_dir / "candidate" / "modules.sha256"
    if not vendor.is_file() or not listing.is_file():
        print("SKIP: module artifact checksum probe (inputs absent)")
        return False
    data = vendor.read_bytes()
    if candidate.digest(data) != candidate.ORIGINAL_HASHES["vendor_boot.img"]:
        raise ValueError("Original vendor_boot.img hash mismatch")
    files = candidate.platform_members(data, "lz4")
    staged = work / "platform"
    staged.mkdir()
    checksums = []
    for line in listing.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (/lib/modules/[A-Za-z0-9_-]+\.ko)", line)
        if not match:
            raise ValueError("Malformed module checksum entry")
        digest, path = match.groups()
        target = staged / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[path.lstrip("/")])
        checksums.append(f"{digest}  {target}\n")
    if not checksums:
        raise ValueError("Empty module checksum plan")
    rewritten = work / "platform.sha256"
    rewritten.write_text("".join(checksums))
    checksum_probe(command, rewritten, True)
    print("PASS: ARM sha256sum parses builder module checksums against PLATFORM files")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--module-artifacts", type=Path, nargs="?", const=ROOT / "build",
                        help="Also check ota/vendor_boot.img against candidate/modules.sha256 in this build directory; skip absent inputs")
    args = parser.parse_args()
    qemu = args.qemu.resolve()
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        binary = work / "busybox-aarch64"
        binary.write_bytes(ramfs.read_busybox(args.apk))
        binary.chmod(0o755)
        command = [str(qemu), str(binary)]
        applets = set(subprocess.check_output(command + ["--list"], text=True).splitlines())
        if not REQUIRED <= applets:
            raise ValueError("Missing required BusyBox applets: " + str(REQUIRED - applets))
        subprocess.run(command + ["ash", "-n", str(ROOT / "initramfs" / "init")], check=True)
        subprocess.run(command + ["ash", "-n", str(ROOT / "initramfs" / "usb-init")], check=True)
        characterize(command, work)
        if args.module_artifacts is not None:
            module_checksum_probe(command, work, args.module_artifacts.resolve())
        refusal = subprocess.run(command + ["ash", str(ROOT / "initramfs" / "usb-init")],
                                 capture_output=True, text=True)
        if refusal.returncode != 1 or "must be PID 1" not in refusal.stderr:
            raise ValueError("Init must refuse non-PID-1 execution before any mounts")
        links = work / "applets"
        links.mkdir()
        subprocess.run(command + ["--install", "-s", str(links)], check=True)
        if not (links / "getty").is_symlink() or not (links / "sha256sum").is_symlink():
            raise ValueError("BusyBox applet installation failed")
        pty_probe(qemu, binary, work)
    print("PASS: ARM64 applets, both ash syntax checks, sha256sum, read semantics, tty number validation, non-PID-1 refusal, applet installer, getty/shell PTY")
    print("NOT TESTED: kernel init, module ABI/probe, ramdisk composition, AVB chain or USB hardware")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run pinned ARM64 BusyBox under user-mode QEMU; never execute init as PID 1.

The PTY probe tests ARM getty -> native host launcher -> ARM interactive shell.
It does NOT emulate the phone kernel, module loading, configfs or physical USB.
"""
import argparse
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import tempfile
import time

import build_initramfs as ramfs

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
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
        subprocess.run(command + ["ash", "-n", str(ROOT / "initramfs" / "usb-init")], check=True)
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
    print("PASS: ARM64 applets, ash syntax, non-PID-1 refusal, applet installer, getty/shell PTY")
    print("NOT TESTED: kernel init, module ABI/probe, ramdisk composition, AVB chain or USB hardware")


if __name__ == "__main__":
    main()

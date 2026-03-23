#!/usr/bin/env python3
"""
mtp_debug.py — See exactly what each detection method returns.

Usage:
    python3 mtp_debug.py
"""

import subprocess
import os

SEP = "-" * 60

def run(cmd):
    r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    return r.stdout, r.stderr, r.returncode


print(SEP)
print("1. gio mount -l")
print(SEP)
out, err, rc = run("gio mount -l")
print(f"  returncode: {rc}")
print(f"  stdout:\n{out}")
if err:
    print(f"  stderr:\n{err}")

print()
print(SEP)
print("2. gio mount -li  (extended info)")
print(SEP)
out, err, rc = run("gio mount -li")
print(f"  returncode: {rc}")
print(f"  stdout:\n{out}")
if err:
    print(f"  stderr:\n{err}")

print()
print(SEP)
print("3. gvfs directory listing")
print(SEP)
uid = os.getuid()
gvfs_dir = f"/run/user/{uid}/gvfs"
print(f"  path: {gvfs_dir}")
if os.path.isdir(gvfs_dir):
    entries = os.listdir(gvfs_dir)
    if entries:
        for e in entries:
            print(f"  {e}")
    else:
        print("  (empty)")
else:
    print("  (directory does not exist)")

print()
print(SEP)
print("4. lsusb  (look for MTP/Android)")
print(SEP)
out, err, rc = run("lsusb")
print(f"  returncode: {rc}")
print(out)

print()
print(SEP)
print("5. find /dev -name 'bus' 2>/dev/null (usbfs)")
print(SEP)
out, _, _ = run("ls /dev/bus/usb/ 2>/dev/null || echo '(not found)'")
print(out)

print()
print(SEP)
print("6. systemctl --user status gvfs-mtp-volume-monitor")
print(SEP)
out, err, rc = run("systemctl --user status gvfs-mtp-volume-monitor 2>&1 | head -20")
print(out or err)

print()
print(SEP)
print("7. python3-glib gio volume listing")
print(SEP)
try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    vm = Gio.VolumeMonitor.get()

    print("  Mounts:")
    for m in vm.get_mounts():
        root = m.get_root()
        uri  = root.get_uri() if root else "?"
        print(f"    name={m.get_name()!r}  uri={uri!r}")

    print("  Volumes:")
    for v in vm.get_volumes():
        print(f"    name={v.get_name()!r}  id={v.get_identifier('unix-device')!r}")

    print("  Drives (with volumes):")
    for d in vm.get_connected_drives():
        print(f"    drive={d.get_name()!r}")
        for v in d.get_volumes():
            print(f"      volume={v.get_name()!r}")

except Exception as e:
    print(f"  Error: {e}")

print()
print(SEP)
print("Done.")
print(SEP)

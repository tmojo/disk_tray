#!/usr/bin/env python3
"""
disk_tray.py — System tray applet for managing disks, partitions, and MTP devices.

Dependencies:
    sudo apt install python3-gi gir1.2-appindicator3-0.1 gir1.2-gtk-3.0 udisks2

Optional (for MTP):
    sudo apt install gvfs-backends

Usage:
    python3 disk_tray.py &
"""

import subprocess
import json
import os
import signal
import threading
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Gio", "2.0")

from gi.repository import Gtk, AppIndicator3, GLib, Gio


# ── Config ────────────────────────────────────────────────────────────────────

ICON_TRAY       = "drive-harddisk-symbolic"
REFRESH_SECONDS = 10

# Filesystems to skip entirely
SKIP_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "devpts", "proc", "sysfs",
    "cgroup", "cgroup2", "pstore", "securityfs", "debugfs",
    "hugetlbfs", "mqueue", "fusectl", "bpf", "tracefs",
    "efivarfs", "configfs", "overlay", "ramfs", "autofs",
    "swap",
}

# Mount points to always skip
SKIP_MOUNTPOINTS = {
    "/", "/home", "/boot", "/boot/efi", "/boot/firmware",
    "/run", "/tmp", "/var", "/var/log", "/var/tmp",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd):
    result = subprocess.run(
        cmd, shell=isinstance(cmd, str),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def notify(title, body, icon="drive-harddisk"):
    subprocess.Popen(["notify-send", "-t", "1000", "-i", icon, title, body])


def _dev_icon(dev):
    """Return the appropriate icon name for a device."""
    kind   = dev.get("kind", "block")
    fstype = dev.get("fstype", "")
    if kind == "mtp":
        return "phone"
    if kind == "network":
        return "folder-remote"
    if fstype in ("iso9660", "udf"):
        return "drive-optical"
    if dev.get("removable"):
        return "drive-removable-media"
    return "drive-harddisk"


def open_in_filemanager(path):
    subprocess.Popen(["xdg-open", path])


def is_skip_mountpoint(mp):
    if not mp:
        return False
    if mp in SKIP_MOUNTPOINTS:
        return True
    # lsblk reports swap partitions/zram as '[SWAP]'
    if mp.upper() == "[SWAP]":
        return True
    for prefix in ("/run/", "/sys/", "/proc/", "/dev/"):
        if mp.startswith(prefix):
            return True
    return False


# ── Disk discovery ────────────────────────────────────────────────────────────

def resolve_fstab_device(dev):
    if dev.startswith("UUID="):
        link = f"/dev/disk/by-uuid/{dev[5:]}"
        try:
            return os.path.realpath(link)
        except Exception:
            return dev
    if dev.startswith("LABEL="):
        link = f"/dev/disk/by-label/{dev[6:]}"
        try:
            return os.path.realpath(link)
        except Exception:
            return dev
    return dev


def get_fstab_special_devices():
    """Return sets of resolved device paths for /home and swap from fstab."""
    home_devs = set()
    swap_devs = set()
    try:
        with open("/etc/fstab") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mp, fstype = parts[0], parts[1], parts[2]
                resolved = resolve_fstab_device(dev)
                if mp == "/home":
                    home_devs.add(resolved)
                if fstype == "swap":
                    swap_devs.add(resolved)
    except Exception:
        pass
    return home_devs, swap_devs


def get_block_devices():
    out, _, rc = run(
        "lsblk -J -o NAME,PATH,FSTYPE,LABEL,SIZE,MOUNTPOINT,HOTPLUG,TYPE,RM,PARTTYPE"
    )
    if rc != 0 or not out:
        return []

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []

    fstab_home, fstab_swap = get_fstab_special_devices()
    SWAP_PART_GUID = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
    devices = []

    def walk(nodes, parent_rm=False):
        for node in nodes:
            ntype      = node.get("type", "")
            fstype     = node.get("fstype") or ""
            mountpoint = node.get("mountpoint") or ""
            name       = node.get("name", "")
            path       = node.get("path", f"/dev/{name}")
            label      = node.get("label") or name
            size       = node.get("size", "?")
            hotplug    = node.get("hotplug", False) or parent_rm
            parttype   = (node.get("parttype") or "").lower()
            children   = node.get("children") or []

            # Skip swap by fstype, partition GUID, fstab, zram, or [SWAP] mountpoint
            if (fstype == "swap"
                    or parttype == SWAP_PART_GUID
                    or path in fstab_swap
                    or path.startswith("/dev/zram")
                    or mountpoint.upper() == "[SWAP]"):
                walk(children, hotplug)
                continue

            # Skip noisy fstypes
            if fstype in SKIP_FSTYPES:
                walk(children, hotplug)
                continue

            # Skip system mountpoints
            if is_skip_mountpoint(mountpoint):
                walk(children, hotplug)
                continue

            # Skip /home device from fstab
            if path in fstab_home:
                walk(children, hotplug)
                continue

            # Skip bare disks — just recurse
            if ntype == "disk" and not fstype and not mountpoint:
                walk(children, hotplug)
                continue

            if fstype or mountpoint:
                devices.append({
                    "name":       label,
                    "path":       path,
                    "fstype":     fstype,
                    "size":       size,
                    "mountpoint": mountpoint,
                    "mounted":    bool(mountpoint),
                    "removable":  hotplug,
                    "kind":       "block",
                })

            walk(children, hotplug)

    walk(data.get("blockdevices", []))
    return devices


def get_gio_devices():
    """
    Detect non-block volumes/mounts via Gio.VolumeMonitor:
      - MTP devices (phones, cameras) identified by mtp:// activation root
        or unix-device under /dev/bus/usb/
      - Network volumes (NFS, SMB, SFTP, etc.) identified by class='network'
        or activation root with a network URI scheme

    Two passes:
      1. Volumes — covers unmounted network shares and MTP devices.
         Skips MTP volumes whose shadow mount is already handled in pass 2.
      2. Volume-less mounts (GDaemonMount) — catches SFTP/gvfs mounts that
         were opened directly (e.g. via a file manager) and have no Volume
         object.  Skips shadowed mounts to avoid MTP duplicates.
    """
    devices = []
    vm = Gio.VolumeMonitor.get()

    # Network URI schemes gvfs exposes as volumes or mounts
    NETWORK_SCHEMES = {"smb", "sftp", "ftp", "nfs", "dav", "davs",
                       "network", "dns-sd", "afp"}

    # ── Pass 1: volumes ───────────────────────────────────────────────────────
    for volume in vm.get_volumes():
        name = volume.get_name() or "Unknown Volume"

        activation_root = volume.get_activation_root()
        unix_dev        = volume.get_identifier("unix-device") or ""
        vol_class       = volume.get_identifier("class") or ""
        uri             = activation_root.get_uri() if activation_root else ""
        scheme          = uri.split("://")[0].lower() if "://" in uri else ""

        # ── MTP ──────────────────────────────────────────────────────────────
        if uri.startswith("mtp://") or "/dev/bus/usb/" in unix_dev:
            kind   = "mtp"
            fstype = "mtp"

        # ── Network volumes ───────────────────────────────────────────────────
        elif vol_class == "network" or scheme in NETWORK_SCHEMES:
            kind   = "network"
            fstype = scheme or "network"

        else:
            continue

        # Check if currently mounted
        mount = volume.get_mount()
        if mount is not None:
            root       = mount.get_root()
            mountpoint = root.get_path() if root else ""
            # For network mounts get_path() may be None — fall back to URI
            if not mountpoint and root:
                mountpoint = root.get_uri() or ""
        else:
            mountpoint = ""

        devices.append({
            "name":       name,
            "path":       uri or name,
            "fstype":     fstype,
            "size":       "?",
            "mountpoint": mountpoint,
            "mounted":    bool(mountpoint),
            "removable":  True,
            "kind":       kind,
            "_volume":    volume,
            "_mount":     mount,
        })

    # ── Pass 2: volume-less mounts (e.g. GDaemonMount for SFTP) ──────────────
    # These are mounts opened directly by gvfs/file-managers with no Volume.
    # Shadowed mounts are the hidden duplicates gvfs creates for MTP — skip them.
    seen_uris = {d["path"] for d in devices}

    for mount in vm.get_mounts():
        # Skip if this mount belongs to a volume (already handled above)
        if mount.get_volume() is not None:
            continue
        # Skip the hidden shadow-mount gvfs creates for MTP
        if mount.is_shadowed():
            continue

        root   = mount.get_root()
        uri    = root.get_uri() if root else ""
        scheme = uri.split("://")[0].lower() if "://" in uri else ""

        # Only include network-scheme mounts (sftp, smb, ftp, nfs, …)
        if scheme not in NETWORK_SCHEMES:
            continue

        # Avoid duplicating anything already found via volumes
        if uri in seen_uris:
            continue

        name       = mount.get_name() or uri
        mountpoint = root.get_path() if root else ""
        if not mountpoint and root:
            mountpoint = uri  # gvfs URI as fallback (e.g. sftp://host/)

        devices.append({
            "name":       name,
            "path":       uri or name,
            "fstype":     scheme or "network",
            "size":       "?",
            "mountpoint": mountpoint,
            "mounted":    True,   # if it's in get_mounts() it is mounted
            "removable":  True,
            "kind":       "network",
            "_volume":    None,
            "_mount":     mount,
        })

    return devices


def get_all_devices():
    return get_block_devices() + get_gio_devices()


# ── Mount / Unmount ───────────────────────────────────────────────────────────

def mount_device(dev, callback):
    if dev["kind"] in ("mtp", "network"):
        # Use GLib Volume.mount() — same as file managers do
        volume = dev.get("_volume")
        if volume and volume.can_mount():
            def _on_mount_done(vol, result):
                try:
                    vol.mount_finish(result)
                    notify("Mounted", f"{dev['name']} mounted.", icon=_dev_icon(dev))
                    # Open in file manager — get the fresh mount path
                    mount = vol.get_mount()
                    if mount:
                        root = mount.get_root()
                        path = root.get_path() if root else None
                        if path:
                            open_in_filemanager(path)
                        else:
                            open_in_filemanager(root.get_uri())
                except Exception as e:
                    notify("Mount failed", str(e), icon=_dev_icon(dev))
                GLib.idle_add(callback)
            volume.mount(0, None, None, _on_mount_done)
        else:
            subprocess.Popen(["xdg-open", "computer:///"])
            GLib.idle_add(callback)
        return

    def _do():
        out, err, rc = run(f"udisksctl mount -b {dev['path']}")
        if rc == 0:
            # Parse the mountpoint from udisksctl output e.g.
            # "Mounted /dev/sda5 at /media/user/label"
            mp = ""
            if " at " in out:
                mp = out.split(" at ", 1)[1].strip().rstrip(".")
            notify("Mounted", f"{dev['name']} mounted successfully.", icon=_dev_icon(dev))
            if mp:
                open_in_filemanager(mp)
        else:
            notify("Mount failed", err or "Unknown error.", icon=_dev_icon(dev))
        GLib.idle_add(callback)
    threading.Thread(target=_do, daemon=True).start()


def unmount_device(dev, callback):
    if dev["kind"] in ("mtp", "network"):
        # Prefer the stored Gio.Mount object; fall back to volume.get_mount()
        mount_obj = dev.get("_mount")
        if mount_obj is None:
            volume = dev.get("_volume")
            mount_obj = volume.get_mount() if volume else None
        if mount_obj:
            def _on_unmount_done(mnt, result):
                try:
                    mnt.unmount_with_operation_finish(result)
                    notify("Unmounted", f"{dev['name']} unmounted.", icon=_dev_icon(dev))
                except Exception as e:
                    notify("Unmount failed", str(e), icon=_dev_icon(dev))
                GLib.idle_add(callback)
            mount_obj.unmount_with_operation(0, None, None, _on_unmount_done)
        else:
            mp = dev.get("mountpoint", "")
            if mp:
                run(f"gio mount -u '{mp}'")
            notify("Unmounted", f"{dev['name']} unmounted.", icon=_dev_icon(dev))
            GLib.idle_add(callback)
        return

    def _do():
        out, err, rc = run(f"udisksctl unmount -b {dev['path']}")
        if rc == 0:
            notify("Unmounted", f"{dev['name']} unmounted.", icon=_dev_icon(dev))
        else:
            notify("Unmount failed", err or "Unknown error.", icon=_dev_icon(dev))
        GLib.idle_add(callback)
    threading.Thread(target=_do, daemon=True).start()


# ── Tray Applet ───────────────────────────────────────────────────────────────

class DiskTrayApplet:

    def __init__(self):
        self.indicator = AppIndicator3.Indicator.new(
            "disk-tray",
            ICON_TRAY,
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Disk Manager")

        # settings = Gtk.Settings.get_default()
        # settings.set_property("gtk-menu-images", True)

        self._menu_open = False
        self._last_devs  = []

        self.menu = Gtk.Menu()
        self.menu.connect("show", self._on_menu_show)
        self.menu.connect("hide", self._on_menu_hide)
        self.indicator.set_menu(self.menu)

        # Initial build — also seeds _last_devs for change detection
        devs = get_all_devices()
        self._last_devs = devs
        self._build_menu(devs)

        # ── GVolumeMonitor signals — instant response to device events ────────
        # These fire on the main thread via GLib, so no threading concerns.
        self._vm = Gio.VolumeMonitor.get()
        self._vm.connect("volume-added",   self._on_volume_event)
        self._vm.connect("volume-removed", self._on_volume_event)
        self._vm.connect("mount-added",    self._on_volume_event)
        self._vm.connect("mount-removed",  self._on_volume_event)
        self._vm.connect("drive-connected",    self._on_volume_event)
        self._vm.connect("drive-disconnected", self._on_volume_event)

        # ── Periodic timer — catches block device mount state changes ─────────
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._schedule_bg_refresh)

    # ── Menu visibility tracking ──────────────────────────────────────────────

    def _on_menu_show(self, _):
        self._menu_open = True

    def _on_menu_hide(self, _):
        self._menu_open = False

    # ── Volume monitor signal handler ─────────────────────────────────────────

    def _on_volume_event(self, monitor, *args):
        """Called instantly on the main thread when any device event fires."""
        # Small delay lets gvfs finish updating before we query it
        GLib.timeout_add(300, self._do_immediate_refresh)

    def _do_immediate_refresh(self):
        """Main thread: full refresh triggered by a device event."""
        mtp_devs = get_gio_devices()
        # Block devices also need a refresh (e.g. drive connected)
        threading.Thread(
            target=self._fetch_block_for_event,
            args=(mtp_devs,),
            daemon=True
        ).start()
        return False  # one-shot timeout

    def _fetch_block_for_event(self, mtp_devs):
        block_devs = get_block_devices()
        GLib.idle_add(self._apply_event_refresh, block_devs, mtp_devs)

    def _apply_event_refresh(self, block_devs, mtp_devs):
        devs = block_devs + mtp_devs
        if not self._devices_changed(devs):
            return
        self._last_devs = devs
        self._build_menu(devs)

    # ── Periodic refresh logic ────────────────────────────────────────────────
    # get_block_devices() runs in a worker thread (safe: pure subprocess).
    # get_gio_devices() must run on the GTK main thread (uses Gio).
    # The menu is only rebuilt when the device list actually changes.

    def _start_refresh(self):
        threading.Thread(target=self._fetch_block_then_merge, daemon=True).start()

    def _fetch_block_then_merge(self):
        """Worker thread: fetch block devices, hand off to main thread."""
        block_devs = get_block_devices()
        GLib.idle_add(self._merge_with_mtp, block_devs)

    def _merge_with_mtp(self, block_devs):
        """Main thread: fetch MTP (Gio), compare, rebuild only if changed."""
        mtp_devs = get_gio_devices()
        devs = block_devs + mtp_devs
        if not self._devices_changed(devs):
            return  # nothing changed — leave the menu alone
        self._last_devs = devs
        self._build_menu(devs)

    def _devices_changed(self, new_devs):
        """Compare new device list to last known — ignore GLib objects."""
        def snapshot(devs):
            return [
                (d["name"], d["path"], d["mounted"], d["mountpoint"])
                for d in devs
            ]
        return snapshot(new_devs) != snapshot(self._last_devs)

    def _schedule_bg_refresh(self):
        """Periodic timer — always runs, rebuild only if something changed."""
        self._start_refresh()
        return True  # keep timer running

    # ── Menu construction ─────────────────────────────────────────────────────

    def _icon_item(self, icon_name, label_text):
        """Create a menu item with a themed icon using a Box (no deprecated ImageMenuItem)."""
        item = Gtk.MenuItem()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        lbl = Gtk.Label(label=label_text)
        lbl.set_halign(Gtk.Align.START)
        box.pack_start(img, False, False, 0)
        box.pack_start(lbl, True, True, 0)
        item.add(box)
        return item

    def _build_menu(self, devices):
        for child in self.menu.get_children():
            self.menu.remove(child)

        if not devices:
            item = Gtk.MenuItem(label="No disks found")
            item.set_sensitive(False)
            self.menu.append(item)
        else:
            first = True
            for dev in devices:
                if not first:
                    self.menu.append(Gtk.SeparatorMenuItem())
                first = False
                self._add_device_rows(dev)

        self.menu.append(Gtk.SeparatorMenuItem())

        ref = self._icon_item("view-refresh", "Refresh")
        ref.connect("activate", self._on_manual_refresh)
        self.menu.append(ref)

        about_item = self._icon_item("help-about", "About")
        about_item.connect("activate", self._on_about)
        self.menu.append(about_item)

        quit_item = self._icon_item("application-exit", "Quit")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()

    def _add_device_rows(self, dev):
        """Add a non-clickable header row + indented action rows for one device."""
        name       = dev["name"]
        size       = dev["size"]
        fstype     = dev["fstype"] or "?"
        mounted    = dev["mounted"]
        mountpoint = dev.get("mountpoint", "")
        kind       = dev["kind"]

        # Pick icon based on device type
        # header_icon = _dev_icon(dev)

        # Header row — CheckMenuItem: checked=mounted, click=toggle mount
        size_str = f"{size}, " if size and size != "?" else ""
        header_text = f"{name}  [{size_str}{fstype}]"
        if mounted and mountpoint and not mountpoint.startswith("/run/user/"):
            header_text += f"   ↳  {mountpoint}"

        header = Gtk.CheckMenuItem()
        header.set_active(mounted)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        # img = Gtk.Image.new_from_icon_name(header_icon, Gtk.IconSize.MENU)
        lbl = Gtk.Label()
        lbl.set_text(header_text)
        lbl.set_halign(Gtk.Align.START)
        # box.pack_start(img, False, False, 0)
        box.pack_start(lbl, True, True, 0)
        header.add(box)
        # box.show_all()
        if mounted:
            header.connect("activate", lambda _, d=dev: self._on_unmount(d))
        else:
            header.connect("activate", lambda _, d=dev: self._on_mount(d))
        self.menu.append(header)

        # Open in file manager (only when mounted)
        if mounted:
            ismtp = kind in ("mtp", "network")
            if ismtp or mountpoint:
                o = self._icon_item("fileopen", "Open in File Manager")
                if ismtp:
                    uri = dev.get("path", "")
                    o.connect("activate", lambda _, u=uri: open_in_filemanager(u))
                elif mountpoint:
                    o = self._icon_item("fileopen", "Open in File Manager")
                    o.connect("activate", lambda _, mp=mountpoint: open_in_filemanager(mp))
                self.menu.append(o)
    # ── Action handlers ───────────────────────────────────────────────────────

    def _on_mount(self, dev):
        self.menu.set_sensitive(False)
        mount_device(dev, self._on_action_done)

    def _on_unmount(self, dev):
        self.menu.set_sensitive(False)
        unmount_device(dev, self._on_action_done)

    def _on_action_done(self):
        self.menu.set_sensitive(True)
        self._start_refresh()

    def _on_manual_refresh(self, _):
        self._start_refresh()

    def _on_about(self, _):
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("Disk Tray")
        dlg.set_version("1.0")
        dlg.set_comments(
            "A system tray applet for mounting disks,\n"
            "partitions, and MTP devices."
        )
        dlg.set_license_type(Gtk.License.MIT_X11)
        dlg.set_logo_icon_name("drive-harddisk")
        dlg.set_website("https://github.com/tmojo/disk_tray")
        dlg.set_website_label("Source on GitHub")
        dlg.run()
        dlg.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    applet = DiskTrayApplet()
    Gtk.main()

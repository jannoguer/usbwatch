#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import platform
import logging
import logging.handlers
import threading
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
SNAPSHOT_MAX_DEPTH = 4

sys.stdout = open(os.devnull, "w")
sys.stderr = open(os.devnull, "w")

_root_handler = logging.handlers.RotatingFileHandler(
    str(LOGS_DIR / "monitor.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_root_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_root_handler)
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

# drive_id -> {"observer": Observer, "ev_log": Logger, "fh": FileHandler}
_active: dict = {}
_lock = threading.Lock()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe(s: str) -> str:
    """Strip characters unsafe for filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))


def _snapshot(mount: Path, label: str) -> None:
    path = LOGS_DIR / f"snapshot_{_safe(label)}_{_timestamp()}.txt"
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            for root, dirs, files in os.walk(mount):
                rel = os.path.relpath(root, mount)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth > SNAPSHOT_MAX_DEPTH:
                    dirs.clear()
                    continue
                dirs.sort()
                indent = "    " * depth
                name = str(mount) if rel == "." else os.path.basename(root)
                f.write(f"{indent}{name}/\n")
                for fname in sorted(files):
                    f.write(f"{'    ' * (depth + 1)}{fname}\n")
        log.info("Snapshot written: %s", path)
    except Exception:
        log.exception("Snapshot failed for %s", mount)


def _make_event_logger(label: str) -> tuple:
    ts = _timestamp()
    path = LOGS_DIR / f"events_{_safe(label)}_{ts}.log"
    lg = logging.getLogger(f"usb.{_safe(label)}.{ts}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    lg.addHandler(fh)
    return lg, fh


def _start_watcher(mount: Path, label: str) -> tuple:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    ev_log, fh = _make_event_logger(label)

    class _Handler(FileSystemEventHandler):
        def on_created(self, e):
            ev_log.info(
                "CREATED  %s  %s", "DIR" if e.is_directory else "FILE", e.src_path
            )

        def on_deleted(self, e):
            ev_log.info(
                "DELETED  %s  %s", "DIR" if e.is_directory else "FILE", e.src_path
            )

        def on_modified(self, e):
            ev_log.info(
                "MODIFIED %s  %s", "DIR" if e.is_directory else "FILE", e.src_path
            )

        def on_moved(self, e):
            ev_log.info(
                "MOVED    %s  %s -> %s",
                "DIR" if e.is_directory else "FILE",
                e.src_path,
                e.dest_path,
            )

    obs = Observer()
    obs.schedule(_Handler(), str(mount), recursive=True)
    obs.start()
    log.info("FS monitor started: %s", mount)
    return obs, ev_log, fh


def on_connect(drive_id: str, mount: Path, label: str) -> None:
    with _lock:
        if drive_id in _active:
            return
        _active[drive_id] = None  # reserve slot; prevents duplicate connect races

    log.info("USB connected: id=%s mount=%s label=%s", drive_id, mount, label)
    threading.Thread(target=_snapshot, args=(mount, label), daemon=True).start()
    obs, ev_log, fh = _start_watcher(mount, label)

    with _lock:
        _active[drive_id] = {"observer": obs, "ev_log": ev_log, "fh": fh}


def on_disconnect(drive_id: str) -> None:
    with _lock:
        entry = _active.pop(drive_id, None)
    if entry is None:
        return
    if not isinstance(entry, dict):
        log.warning(
            "Disconnect during connect setup for %s; watcher may not have started",
            drive_id,
        )
        return
    log.info("USB disconnected: id=%s", drive_id)
    try:
        entry["observer"].stop()
        entry["observer"].join(timeout=5)
    except Exception:
        log.exception("Error stopping observer for %s", drive_id)
    try:
        entry["ev_log"].removeHandler(entry["fh"])
        entry["fh"].close()
    except Exception:
        pass


SYSTEM = platform.system()

if SYSTEM == "Windows":

    def _wmi_thread(notification_type: str, callback) -> None:
        import pythoncom
        import wmi

        # Each thread that uses COM/WMI must call CoInitialize.
        pythoncom.CoInitialize()
        try:
            c = wmi.WMI()
            # DriveType 2 == Removable
            watcher = c.watch_for(
                notification_type=notification_type,
                wmi_class="Win32_LogicalDisk",
                DriveType=2,
            )
            while True:
                try:
                    disk = watcher()
                    callback(disk)
                except Exception:
                    log.exception("WMI event error (%s)", notification_type)
        finally:
            pythoncom.CoUninitialize()

    def _on_arrival(disk) -> None:
        drive_id = disk.DeviceID  # e.g. "E:"
        label = disk.VolumeName or drive_id.rstrip(":")
        on_connect(drive_id, Path(drive_id + "\\"), label)

    def _on_removal(disk) -> None:
        on_disconnect(disk.DeviceID)

    def run() -> None:
        log.info("Starting USB monitor (Windows/WMI)")
        t_add = threading.Thread(
            target=_wmi_thread, args=("Creation", _on_arrival), daemon=True
        )
        t_rem = threading.Thread(
            target=_wmi_thread, args=("Deletion", _on_removal), daemon=True
        )
        t_add.start()
        t_rem.start()
        # Block the main thread indefinitely; daemon threads exit with the process.
        threading.Event().wait()

elif SYSTEM == "Linux":
    import time

    def _find_mount(
        device_node: str, retries: int = 12, interval: float = 0.5
    ) -> str | None:
        # Poll /proc/mounts until the device appears.
        # Needed because automount (udisks2 etc.) happens slightly after the udev event.
        for _ in range(retries):
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2 and parts[0] == device_node:
                            return parts[1]
            except OSError:
                pass
            time.sleep(interval)
        return None

    def _is_usb(device) -> bool:
        # Walk the device hierarchy to check for USB bus membership.
        d = device
        while d is not None:
            if d.get("ID_BUS") == "usb":
                return True
            d = d.parent
        return False

    def _handle_add(device) -> None:
        if not _is_usb(device):
            return
        node = device.device_node
        label = device.get("ID_FS_LABEL") or device.get("ID_SERIAL") or device.sys_name
        mount = _find_mount(node)
        if mount:
            on_connect(node, Path(mount), label)
        else:
            log.warning("No mount point found for %s within timeout", node)

    def run() -> None:
        import pyudev

        ctx = pyudev.Context()
        mon = pyudev.Monitor.from_netlink(ctx)
        mon.filter_by(subsystem="block", device_type="partition")
        mon.start()
        log.info("Starting USB monitor (Linux/pyudev)")
        for action, device in mon:
            if action == "add":
                # Run in a thread so the udev loop is never blocked while waiting
                # for the automount to appear in /proc/mounts.
                threading.Thread(
                    target=_handle_add, args=(device,), daemon=True
                ).start()
            elif action == "remove":
                on_disconnect(device.device_node)

else:

    def run() -> None:
        log.error("Unsupported platform: %s", SYSTEM)
        sys.exit(1)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Fatal error in USB monitor")
        sys.exit(1)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import io
import logging
import logging.handlers
import os
import platform
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="USB monitor: logs drive contents and file-system activity to logs/."
    )
    args, _ = p.parse_known_args()
    return args


_args = _parse_args()

# Set up file logging BEFORE redirecting stdout/stderr so any setup failure
# (e.g. unwritable logs/) is still visible on the real stderr.
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

# Silence all console output now that logging is confirmed working.
_devnull_out = open(os.devnull, "w")
_devnull_err = open(os.devnull, "w")
sys.stdout = _devnull_out
sys.stderr = _devnull_err

# drive_id -> {"observer": Observer, "ev_log": Logger, "fh": FileHandler}
_active: dict = {}
_lock = threading.Lock()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe(s: str) -> str:
    """Strip characters unsafe for filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))


# Directory names to skip before recursing. These are system artefacts that
# appear on Windows and macOS-formatted drives and are never useful to scan.
_JUNK_DIRS: frozenset[str] = frozenset({
    "System Volume Information",
    "$RECYCLE.BIN",
    ".Trashes",
    ".fseventsd",
    ".Spotlight-V100",
    ".TemporaryItems",
})


def _snapshot(mount: Path, label: str) -> None:
    """Walk *mount* with os.scandir and write a tab-separated manifest.

    Output format — one line per entry::

        <relpath>\t<size>\t<mtime_iso8601_utc>\t<flag>\n

    where *flag* is ``D`` (directory), ``L`` (symlink), or ``F`` (file),
    *size* is the byte count for files or ``-`` for dirs/symlinks, and
    *mtime* is UTC in ``YYYY-MM-DDTHH:MM:SSZ`` format.

    Design note — deliberately single-threaded:
        USB mass-storage devices expose a single command queue. Running two or
        more scanner threads causes seek thrash on FAT/exFAT and NTFS-over-USB
        and is slower than a single ordered scan. Parallelism would only help
        if we were hashing file contents, which is a separate concern.
    """
    path = LOGS_DIR / f"snapshot_{_safe(label)}_{_timestamp()}.tsv"
    count = 0
    try:
        raw = open(path, "wb")  # noqa: WPS515 – closed via BufferedWriter
        with io.BufferedWriter(raw, buffer_size=1 << 20) as buf:
            stack: collections.deque[Path] = collections.deque([mount])
            while stack:
                current = stack.pop()
                try:
                    entries = list(os.scandir(current))
                except PermissionError:
                    log.warning("Permission denied scanning %s", current)
                    continue
                dirs_to_push: list[Path] = []
                for entry in entries:
                    # Skip symlinks to avoid reparse-point / junction loops.
                    if entry.is_symlink():
                        relpath = os.path.relpath(entry.path, mount)
                        try:
                            mtime_ts = entry.stat(follow_symlinks=False).st_mtime
                            mtime = datetime.fromtimestamp(
                                mtime_ts, tz=timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except OSError:
                            mtime = "-"
                        line = f"{relpath}\t-\t{mtime}\tL\n"
                        buf.write(line.encode("utf-8", errors="replace"))
                        count += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in _JUNK_DIRS:
                            continue
                        dirs_to_push.append(Path(entry.path))
                        relpath = os.path.relpath(entry.path, mount)
                        try:
                            mtime_ts = entry.stat(follow_symlinks=False).st_mtime
                            mtime = datetime.fromtimestamp(
                                mtime_ts, tz=timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except OSError:
                            mtime = "-"
                        line = f"{relpath}\t-\t{mtime}\tD\n"
                    else:
                        relpath = os.path.relpath(entry.path, mount)
                        try:
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size
                            mtime = datetime.fromtimestamp(
                                st.st_mtime, tz=timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except OSError:
                            size = -1
                            mtime = "-"
                        line = f"{relpath}\t{size}\t{mtime}\tF\n"
                    buf.write(line.encode("utf-8", errors="replace"))
                    count += 1
                # Push subdirectories in reverse-sorted order so the stack
                # processes them in sorted order (leftmost first).
                dirs_to_push.sort(key=lambda p: p.name, reverse=True)
                stack.extend(dirs_to_push)
                if count % 10_000 == 0 and count > 0:
                    log.info("Snapshot progress: %d entries scanned", count)
        log.info("Snapshot written: %s (%d entries)", path, count)
    except Exception:
        log.exception("Snapshot failed for %s", mount)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


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


def _close_event_logger(ev_log: logging.Logger, fh: logging.FileHandler) -> None:
    try:
        ev_log.removeHandler(fh)
        fh.close()
        # Remove from the global logger registry to prevent accumulation over
        # many plug/unplug cycles.
        logging.Logger.manager.loggerDict.pop(ev_log.name, None)
    except Exception:
        pass


def _start_watcher(mount: Path, label: str) -> tuple:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

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
        if drive_id not in _active:
            # Drive was disconnected while the watcher was starting; the slot was
            # already popped by on_disconnect, so stop the orphaned observer now.
            log.warning(
                "Drive %s disconnected during setup; tearing down observer", drive_id
            )
            obs.stop()
            obs.join(timeout=5)
            _close_event_logger(ev_log, fh)
            return
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
    _close_event_logger(entry["ev_log"], entry["fh"])


SYSTEM = platform.system()

if SYSTEM == "Windows":

    def _wmi_thread(notification_type: str, callback) -> None:
        import time

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
                    time.sleep(1)
        finally:
            pythoncom.CoUninitialize()

    def _on_arrival(disk) -> None:
        drive_id = disk.DeviceID  # e.g. "E:"
        label = disk.VolumeName or drive_id.rstrip(":")
        on_connect(drive_id, Path(drive_id + "\\"), label)

    def _on_removal(disk) -> None:
        on_disconnect(disk.DeviceID)

    def _scan_existing() -> None:
        import pythoncom
        import wmi

        pythoncom.CoInitialize()
        try:
            c = wmi.WMI()
            for disk in c.Win32_LogicalDisk(DriveType=2):
                try:
                    _on_arrival(disk)
                except Exception:
                    log.exception(
                        "Error scanning existing drive %s",
                        getattr(disk, "DeviceID", "?"),
                    )
        except Exception:
            log.exception("Error during startup drive scan")
        finally:
            pythoncom.CoUninitialize()

    def run() -> None:
        log.info("Starting USB monitor (Windows/WMI)")
        threading.Thread(
            target=_scan_existing, name="scan-existing", daemon=True
        ).start()
        t_add = threading.Thread(
            target=_wmi_thread,
            args=("Creation", _on_arrival),
            name="wmi-add",
            daemon=True,
        )
        t_rem = threading.Thread(
            target=_wmi_thread,
            args=("Deletion", _on_removal),
            name="wmi-remove",
            daemon=True,
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
        if not node:
            return
        label = device.get("ID_FS_LABEL") or device.get("ID_SERIAL") or device.sys_name
        mount = _find_mount(node)
        if mount:
            on_connect(node, Path(mount), label)
        else:
            log.warning("No mount point found for %s within timeout", node)

    def _scan_existing() -> None:
        import pyudev

        ctx = pyudev.Context()
        for device in ctx.list_devices(subsystem="block", DEVTYPE="partition"):
            if _is_usb(device):
                threading.Thread(
                    target=_handle_add,
                    args=(device,),
                    name=f"handle-add-{device.sys_name}",
                    daemon=True,
                ).start()

    def run() -> None:
        import pyudev

        ctx = pyudev.Context()
        mon = pyudev.Monitor.from_netlink(ctx)
        mon.filter_by(subsystem="block", device_type="partition")
        mon.start()
        log.info("Starting USB monitor (Linux/pyudev)")
        threading.Thread(
            target=_scan_existing, name="scan-existing", daemon=True
        ).start()
        for action, device in mon:
            if action == "add":
                # Run in a thread so the udev loop is never blocked while waiting
                # for the automount to appear in /proc/mounts.
                threading.Thread(
                    target=_handle_add,
                    args=(device,),
                    name=f"handle-add-{device.sys_name}",
                    daemon=True,
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

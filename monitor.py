#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import collections
import ctypes
import gzip
import logging
import logging.handlers
import os
import platform
import signal
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

# Set up file logging before redirecting stdout/stderr.
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

# Silence console output; keep a real fallback for the logging lastResort handler.
_devnull_fd = os.open(os.devnull, os.O_WRONLY)
_devnull_out = os.fdopen(_devnull_fd, "w", closefd=False)
sys.stdout = _devnull_out
# Do NOT redirect stderr — the logging lastResort handler writes there, and
# silencing it would swallow any errors that occur before the log file opens.
atexit.register(lambda: os.close(_devnull_fd))
atexit.register(_devnull_out.close)

# Global shutdown event — set by signal handlers to trigger clean exit.
_shutdown = threading.Event()
_is_shutting_down = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    """Signal handler: request graceful shutdown."""
    _shutdown.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# drive_id -> {"observer": Observer, "ev_log": Logger, "fh": FileHandler}
_active: dict = {}
_lock = threading.Lock()


def _teardown_all() -> None:
    """Stop all active observers and close event loggers on shutdown."""
    global _is_shutting_down
    with _lock:
        _is_shutting_down = True
        items = list(_active.items())
        _active.clear()
    for drive_id, entry in items:
        if not isinstance(entry, dict):
            continue
        if "cancel_evt" in entry:
            entry["cancel_evt"].set()
        if entry.get("status") == "connecting":
            if "snapshot_thread" in entry:
                entry["snapshot_thread"].join(timeout=10)
            continue
        log.info("Shutdown: stopping observer for %s", drive_id)
        try:
            if entry.get("observer"):
                entry["observer"].stop()
                entry["observer"].join(timeout=5)
        except Exception:
            log.exception("Error stopping observer for %s on shutdown", drive_id)
        if "ev_log" in entry and "fh" in entry:
            _close_event_logger(entry["ev_log"], entry["fh"])
        if "snapshot_thread" in entry:
            entry["snapshot_thread"].join(timeout=10)


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _safe(s: str) -> str:
    """Strip characters unsafe for filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))


def _escape_path(p: str) -> str:
    return p.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _unescape_path(p: str) -> str:
    return p.replace("\\t", "\t").replace("\\r", "\r").replace("\\n", "\n").replace("\\\\", "\\")


# System directories to skip.
_JUNK_DIRS: frozenset[str] = frozenset(
    {
        "System Volume Information",
        "$RECYCLE.BIN",
        ".Trashes",
        ".fseventsd",
        ".Spotlight-V100",
        ".TemporaryItems",
    }
)
_JUNK_DIRS_UPPER: frozenset[str] = frozenset(d.upper() for d in _JUNK_DIRS)


def _volume_serial(mount: Path) -> str | None:
    """Get stable volume identifier."""
    if platform.system() == "Windows":
        # GetVolumeInformationW requires a root path ending with a backslash.
        root = str(mount).rstrip("\\") + "\\"
        serial = ctypes.c_ulong(0)
        rc = ctypes.windll.kernel32.GetVolumeInformationW(
            root,
            None,
            0,
            ctypes.byref(serial),
            None,
            None,
            None,
            0,
        )
        if rc:
            return format(serial.value, "08x")
        log.warning("GetVolumeInformationW failed for %s (rc=%s)", mount, rc)
    return None


def _load_manifest(serial: str) -> dict[str, tuple[int | str, str, str]]:
    """Load latest snapshot manifest."""
    pattern = f"snapshot_*_{serial}_*.tsv.gz"
    candidates = sorted(LOGS_DIR.glob(pattern), key=lambda p: p.name, reverse=True)
    for cand in candidates:
        manifest: dict[str, tuple[int | str, str, str]] = {}
        try:
            with gzip.open(cand, "rb") as gz:
                for raw_line in gz:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    parts = line.split("\t")
                    if len(parts) != 4:
                        continue
                    relpath, size_s, mtime, flag = parts
                    size: int | str = (
                        int(size_s)
                        if size_s.lstrip("-").isdigit() and size_s != "-"
                        else "-"
                    )
                    manifest[_unescape_path(relpath)] = (size, mtime, flag)
            return manifest
        except Exception:
            log.exception("Failed to load manifest %s", cand)
    return {}


def _scan_entries(
    mount: Path, cancel_evt: threading.Event
) -> dict[str, tuple[int | str, str, str]]:
    """Scan directory and return file metadata map."""
    entries_map: dict[str, tuple[int | str, str, str]] = {}
    stack: collections.deque[Path] = collections.deque([mount])
    heartbeat = 0
    while stack:
        if cancel_evt.is_set():
            log.info("Scan cancelled for %s", mount)
            return {}
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                dir_entries = list(it)
        except OSError:
            log.warning("Cannot scan %s", current)
            continue
        dirs_to_push: list[Path] = []
        for entry in dir_entries:
            relpath = os.path.relpath(entry.path, mount)
            if entry.is_symlink():
                try:
                    mtime_ts = entry.stat(follow_symlinks=False).st_mtime
                    mtime = datetime.fromtimestamp(mtime_ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                except (OSError, ValueError, OverflowError):
                    mtime = "-"
                entries_map[relpath] = ("-", mtime, "L")
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name.upper() in _JUNK_DIRS_UPPER:
                    continue
                dirs_to_push.append(Path(entry.path))
                try:
                    mtime_ts = entry.stat(follow_symlinks=False).st_mtime
                    mtime = datetime.fromtimestamp(mtime_ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                except (OSError, ValueError, OverflowError):
                    mtime = "-"
                entries_map[relpath] = ("-", mtime, "D")
            else:
                try:
                    st = entry.stat(follow_symlinks=False)
                    size: int | str = st.st_size
                    mtime = datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                except (OSError, ValueError, OverflowError):
                    size = -1
                    mtime = "-"
                entries_map[relpath] = (size, mtime, "F")
        dirs_to_push.sort(key=lambda p: p.name, reverse=True)
        stack.extend(dirs_to_push)
        heartbeat += len(dir_entries)
        if heartbeat >= 10_000:
            log.info("Scan progress: %d entries scanned", len(entries_map))
            heartbeat = 0
    return entries_map


def _write_delta(
    mount: Path,
    label: str,
    serial: str,
    old_manifest: dict[str, tuple[int | str, str, str]],
    cancel_evt: threading.Event,
) -> None:
    """Write differential changes against old manifest."""
    ts = _timestamp()
    final = LOGS_DIR / f"delta_{_safe(label)}_{serial}_{ts}.tsv.gz"
    tmp = final.with_suffix(".tsv.gz.tmp")
    count = 0
    try:
        current_entries = _scan_entries(mount, cancel_evt)
        if cancel_evt.is_set():
            tmp.unlink(missing_ok=True)
            return
        with gzip.open(tmp, "wb", compresslevel=1) as gz:
            for relpath, (size, mtime, flag) in current_entries.items():
                enc_relpath = _escape_path(relpath)
                if relpath not in old_manifest:
                    line = f"+\t{enc_relpath}\t{size}\t{mtime}\t{flag}\n"
                else:
                    old_size, old_mtime, old_flag = old_manifest[relpath]
                    if size != old_size or mtime != old_mtime or flag != old_flag:
                        line = f"~\t{enc_relpath}\t{size}\t{mtime}\t{flag}\n"
                    else:
                        continue
                gz.write(line.encode("utf-8", errors="replace"))
                count += 1
            for relpath in old_manifest:
                if relpath not in current_entries:
                    line = f"-\t{_escape_path(relpath)}\t-\t-\t-\n"
                    gz.write(line.encode("utf-8", errors="replace"))
                    count += 1
        os.replace(tmp, final)
        log.info("Delta written: %s (%d changes)", final, count)
    except Exception:
        log.exception("Delta failed for %s", mount)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _snapshot(
    mount: Path,
    label: str,
    serial: str | None = None,
    cancel_evt: threading.Event | None = None,
) -> None:
    """Write complete snapshot manifest."""
    if cancel_evt is None:
        cancel_evt = threading.Event()
    serial_tag = f"_{serial}" if serial else ""
    ts = _timestamp()
    final = LOGS_DIR / f"snapshot_{_safe(label)}{serial_tag}_{ts}.tsv.gz"
    tmp = final.with_suffix(".tsv.gz.tmp")
    count = 0
    try:
        entries_map = _scan_entries(mount, cancel_evt)
        if cancel_evt.is_set():
            tmp.unlink(missing_ok=True)
            return
        with gzip.open(tmp, "wb", compresslevel=1) as gz:
            for relpath, (size, mtime, flag) in entries_map.items():
                line = f"{_escape_path(relpath)}\t{size}\t{mtime}\t{flag}\n"
                gz.write(line.encode("utf-8", errors="replace"))
                count += 1
        os.replace(tmp, final)
        log.info("Snapshot written: %s (%d entries)", final, count)
    except Exception:
        log.exception("Snapshot failed for %s", mount)
        try:
            tmp.unlink(missing_ok=True)
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
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass
    # Remove the logger from the logging Manager to avoid a slow memory leak
    # (Manager holds a permanent reference to every logger created by name).
    try:
        del logging.Logger.manager.loggerDict[ev_log.name]
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
    try:
        obs.schedule(_Handler(), str(mount), recursive=True)
        obs.start()
        log.info("FS monitor started: %s", mount)
    except OSError:
        log.warning("Failed to start FS monitor for %s", mount)
        return None, ev_log, fh
    return obs, ev_log, fh


def _do_snapshot(
    mount: Path, label: str, serial: str | None, cancel_evt: threading.Event
) -> None:
    """Execute snapshot or delta."""
    if serial:
        old = _load_manifest(serial)
        if old:
            log.info("Prior manifest found for serial %s; writing delta", serial)
            _write_delta(mount, label, serial, old, cancel_evt)
            return
    _snapshot(mount, label, serial, cancel_evt)


def on_connect(
    drive_id: str, mount: Path, label: str, serial: str | None = None
) -> None:
    if serial is None:
        serial = _volume_serial(mount)

    cancel_evt = threading.Event()
    t_snap = threading.Thread(
        target=_do_snapshot, args=(mount, label, serial, cancel_evt), daemon=True
    )
    
    with _lock:
        if _is_shutting_down or drive_id in _active:
            return
        _active[drive_id] = {
            "cancel_evt": cancel_evt,
            "status": "connecting",
            "snapshot_thread": t_snap,
        }

    log.info(
        "USB connected: id=%s mount=%s label=%s serial=%s",
        drive_id,
        mount,
        label,
        serial or "unknown",
    )
    t_snap.start()
    obs, ev_log, fh = _start_watcher(mount, label)

    with _lock:
        entry = _active.get(drive_id)
        if entry is None or entry.get("cancel_evt") is not cancel_evt:
            # Teardown observer if drive disconnected during setup.
            log.warning(
                "Drive %s disconnected during setup; tearing down observer", drive_id
            )
            cancel_evt.set()
            if obs:
                obs.stop()
                obs.join(timeout=5)
            _close_event_logger(ev_log, fh)
            return
        _active[drive_id] = {
            "observer": obs,
            "ev_log": ev_log,
            "fh": fh,
            "serial": serial,
            "cancel_evt": cancel_evt,
            "status": "connected",
            "snapshot_thread": t_snap,
        }


def on_disconnect(drive_id: str) -> None:
    with _lock:
        entry = _active.pop(drive_id, None)
    if not entry:
        return
    log.info("USB disconnected: id=%s", drive_id)
    if "cancel_evt" in entry:
        entry["cancel_evt"].set()
    if entry.get("status") == "connecting":
        log.warning(
            "Disconnect during connect setup for %s; watcher may not have started",
            drive_id,
        )
        return
    try:
        if entry.get("observer"):
            entry["observer"].stop()
            entry["observer"].join(timeout=5)
    except Exception:
        log.exception("Error stopping observer for %s", drive_id)
    if "ev_log" in entry and "fh" in entry:
        _close_event_logger(entry["ev_log"], entry["fh"])
    if "snapshot_thread" in entry:
        entry["snapshot_thread"].join(timeout=10)


SYSTEM = platform.system()

if SYSTEM == "Windows":

    def _wmi_thread(notification_type: str, callback) -> None:
        import time

        import pythoncom
        import wmi

        # Initialize COM.
        pythoncom.CoInitialize()
        try:
            while not _shutdown.is_set():
                try:
                    c = wmi.WMI()
                    # DriveType 2 == Removable
                    watcher = c.watch_for(
                        notification_type=notification_type,
                        wmi_class="Win32_LogicalDisk",
                        DriveType=2,
                    )
                    while not _shutdown.is_set():
                        disk = watcher(timeout_ms=1000)
                        if disk is not None:
                            callback(disk)
                except Exception:
                    if not _shutdown.is_set():
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
            target=_scan_existing, name="scan-existing", daemon=False
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
        while not _shutdown.wait(timeout=1.0):
            pass
        _teardown_all()
        log.info("USB monitor stopped")

elif SYSTEM == "Linux":
    import time

    def _find_mount(
        device_node: str, retries: int = 12, interval: float = 0.5
    ) -> str | None:
        # Poll /proc/mounts until device appears.
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
        # Check for USB bus membership.
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
        # Use filesystem UUID as stable volume serial.
        serial = device.get("ID_FS_UUID")
        mount = _find_mount(node)
        if mount:
            on_connect(node, Path(mount), label, serial=serial)
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
            target=_scan_existing, name="scan-existing", daemon=False
        ).start()
        while not _shutdown.is_set():
            device = mon.poll(timeout=1.0)
            if device is None:
                continue
            action = device.action
            if action == "add":
                # Run in thread to avoid blocking udev loop.
                threading.Thread(
                    target=_handle_add,
                    args=(device,),
                    name=f"handle-add-{device.sys_name}",
                    daemon=True,
                ).start()
            elif action == "remove":
                on_disconnect(device.device_node)
        _teardown_all()
        log.info("USB monitor stopped")

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

#!/usr/bin/env python3
from __future__ import annotations


import atexit
import collections
import ctypes
import glob as _glob
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

_devnull_fd = os.open(os.devnull, os.O_WRONLY)
_devnull_out = os.fdopen(_devnull_fd, "w", closefd=False)
sys.stdout = _devnull_out
# stderr is left alone: logging's lastResort handler writes there as a
# fallback before our log file opens.
atexit.register(lambda: os.close(_devnull_fd))
atexit.register(_devnull_out.close)

_shutdown = threading.Event()
_is_shutting_down = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    _shutdown.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# drive_id -> {"cancel_evt": Event, "snapshot_thread": Thread, "serial": str}
_active: dict = {}
_lock = threading.Lock()


def _teardown_all() -> None:
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
        log.info("Shutdown: cancelling snapshot for %s", drive_id)
        if "snapshot_thread" in entry:
            entry["snapshot_thread"].join(timeout=10)


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _safe(s: str) -> str:
    """Strip characters unsafe for filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))


def _escape_path(p: str) -> str:
    return (
        p.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _unescape_path(p: str) -> str:
    import re
    def repl(m):
        c = m.group(1)
        if c == "n": return "\n"
        if c == "r": return "\r"
        if c == "t": return "\t"
        if c == "\\": return "\\"
        return m.group(0)
    return re.sub(r"\\(.)", repl, p)


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
    pattern = f"snapshot_*_{_glob.escape(serial)}_*.tsv.gz"
    candidates = sorted(LOGS_DIR.glob(pattern), key=lambda p: p.name, reverse=True)
    for cand in candidates:
        try:
            with gzip.open(cand, "rb") as gz:
                data = gz.read()  # validates CRC; raises on truncation/corruption
        except Exception:
            log.exception("Failed to load manifest %s", cand)
            continue
        manifest: dict[str, tuple[int | str, str, str]] = {}
        for raw_line in data.splitlines():
            line = raw_line.decode("utf-8", errors="replace")
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
    return {}


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OSError, ValueError, OverflowError):
        return "-"


class ScanRootError(OSError):
    """Raised when os.scandir fails on the mount root itself."""


def _scan_entries(
    mount: Path, cancel_evt: threading.Event
) -> dict[str, tuple[int | str, str, str]]:
    entries_map: dict[str, tuple[int | str, str, str]] = {}
    stack: collections.deque[Path] = collections.deque([mount])
    # Distinguishes the mount-root scandir failure (fatal) from sub-dir failures (skip).
    root_scan = True
    heartbeat = 0
    while stack:
        if cancel_evt.is_set():
            log.info("Scan cancelled for %s", mount)
            return {}
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                dir_entries = list(it)
        except OSError as exc:
            if root_scan:
                raise ScanRootError(
                    f"Cannot scan mount root {mount}: {exc}"
                ) from exc
            log.warning("Cannot scan %s", current)
            continue
        finally:
            root_scan = False
        dirs_to_push: list[Path] = []
        for entry in dir_entries:
            relpath = os.path.relpath(entry.path, mount)
            try:
                is_symlink = entry.is_symlink()
                is_dir = not is_symlink and entry.is_dir(follow_symlinks=False)
            except OSError:
                log.warning("Cannot stat %s", entry.path)
                continue

            if is_dir and entry.name.upper() in _JUNK_DIRS_UPPER:
                continue

            try:
                st = entry.stat(follow_symlinks=False)
                mtime = _fmt_time(st.st_mtime)
            except OSError:
                st = None
                mtime = "-"

            if is_symlink:
                entries_map[relpath] = ("-", mtime, "L")
            elif is_dir:
                dirs_to_push.append(Path(entry.path))
                entries_map[relpath] = ("-", mtime, "D")
            else:
                size = st.st_size if st else -1
                entries_map[relpath] = (size, mtime, "F")
        dirs_to_push.sort(key=lambda p: p.name, reverse=True)
        stack.extend(dirs_to_push)
        heartbeat += len(dir_entries)
        if heartbeat >= 10_000:
            log.info("Scan progress: %d entries scanned", len(entries_map))
            heartbeat = 0
    return entries_map


def _write_atomic_gz(final: Path, lines_iter) -> int:
    """Write lines to a gzip file atomically via a .tmp sibling + os.replace."""
    tmp = final.parent / (final.name + ".tmp")
    count = 0
    try:
        with gzip.open(tmp, "wb", compresslevel=1) as gz:
            for line in lines_iter:
                gz.write(line.encode("utf-8", errors="replace"))
                count += 1
        os.replace(tmp, final)
        return count
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _write_delta(
    mount: Path,
    label: str,
    serial: str,
    old_manifest: dict[str, tuple[int | str, str, str]],
    cancel_evt: threading.Event,
) -> None:
    try:
        current_entries = _scan_entries(mount, cancel_evt)
    except ScanRootError:
        log.warning("Delta skipped: root scan failed for %s", mount)
        return
    if cancel_evt.is_set():
        return

    ts = _timestamp()
    final = LOGS_DIR / f"delta_{_safe(label)}_{serial}_{ts}.tsv.gz"

    def _lines():
        for relpath, (size, mtime, flag) in current_entries.items():
            enc_relpath = _escape_path(relpath)
            if relpath not in old_manifest:
                yield f"+\t{enc_relpath}\t{size}\t{mtime}\t{flag}\n"
            else:
                old_size, old_mtime, old_flag = old_manifest[relpath]
                if size != old_size or mtime != old_mtime or flag != old_flag:
                    yield f"~\t{enc_relpath}\t{size}\t{mtime}\t{flag}\n"
        for relpath in old_manifest:
            if relpath not in current_entries:
                yield f"-\t{_escape_path(relpath)}\t-\t-\t-\n"

    try:
        count = _write_atomic_gz(final, _lines())
        log.info("Delta written: %s (%d changes)", final, count)
    except Exception:
        log.exception("Delta failed for %s", mount)


def _snapshot(
    mount: Path,
    label: str,
    serial: str | None = None,
    cancel_evt: threading.Event | None = None,
) -> None:
    cancel_evt = cancel_evt or threading.Event()
    try:
        entries_map = _scan_entries(mount, cancel_evt)
    except ScanRootError:
        log.warning("Snapshot skipped: root scan failed for %s", mount)
        return
    if cancel_evt.is_set():
        return

    serial_tag = f"_{serial}" if serial else ""
    ts = _timestamp()
    final = LOGS_DIR / f"snapshot_{_safe(label)}{serial_tag}_{ts}.tsv.gz"

    def _lines():
        for relpath, (size, mtime, flag) in entries_map.items():
            yield f"{_escape_path(relpath)}\t{size}\t{mtime}\t{flag}\n"

    try:
        count = _write_atomic_gz(final, _lines())
        log.info("Snapshot written: %s (%d entries)", final, count)
    except Exception:
        log.exception("Snapshot failed for %s", mount)


def _do_snapshot(
    mount: Path, label: str, serial: str | None, cancel_evt: threading.Event
) -> None:
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
            "snapshot_thread": t_snap,
            "serial": serial,
        }
        # Start under the lock so every entry in _active has a started thread;
        # otherwise on_disconnect / _teardown_all could join() before start().
        t_snap.start()

    log.info(
        "USB connected: id=%s mount=%s label=%s serial=%s",
        drive_id,
        mount,
        label,
        serial or "unknown",
    )


def on_disconnect(drive_id: str) -> None:
    with _lock:
        entry = _active.pop(drive_id, None)
    if not entry:
        return
    log.info("USB disconnected: id=%s", drive_id)
    if "cancel_evt" in entry:
        entry["cancel_evt"].set()
    if "snapshot_thread" in entry:
        entry["snapshot_thread"].join(timeout=10)


SYSTEM = platform.system()

if SYSTEM == "Windows":

    def _wmi_thread(notification_type: str, callback) -> None:
        import time

        import pythoncom
        import wmi

        pythoncom.CoInitialize()
        try:
            while not _shutdown.is_set():
                try:
                    c = wmi.WMI()
                    # DriveType=2 → removable.
                    watcher = c.watch_for(
                        notification_type=notification_type,
                        wmi_class="Win32_LogicalDisk",
                        DriveType=2,
                    )
                    while not _shutdown.is_set():
                        try:
                            disk = watcher(timeout_ms=1000)
                        except wmi.x_wmi_timed_out:
                            continue
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

    import re

    def _decode_proc_mounts_field(field: str) -> str:
        """Decode octal escape sequences in /proc/mounts fields (e.g. \\040 → space)."""
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)

    def _find_mount(
        device_node: str, retries: int = 12, interval: float = 0.5
    ) -> str | None:
        # Poll /proc/mounts: udev fires before the kernel finishes mounting.
        # Limit the split to 3 so a mount path containing \040-encoded spaces stays intact.
        for _ in range(retries):
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split(" ", 3)
                        if len(parts) >= 2 and parts[0] == device_node:
                            return _decode_proc_mounts_field(parts[1])
            except OSError:
                pass
            time.sleep(interval)
        return None

    def _is_usb(device) -> bool:
        # Walk parents — ID_BUS is set on the USB device, not the partition.
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

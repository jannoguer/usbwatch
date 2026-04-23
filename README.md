<div align="center">

# USB Monitor

[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey?style=flat-square)](https://github.com)
[![Python](https://img.shields.io/badge/python-3.10%2B-yellow?style=flat-square)](https://www.python.org)

</div>

Script that detects USB drives and logs their contents and file activity. Prepared to be run as a background service.

## Requirements

- **Python 3.10+** and **pip**
- Dependencies (install once):

```
pip install -r requirements.txt
```

## Usage

Depending on the OS, run the script:

**Windows** (no console window)
```
pythonw monitor.py
```

**Linux**
```
python3 monitor.py &
```

For persistent background execution use Task Scheduler (Windows) or a systemd unit (Linux).

## Features

| Feature | Technology |
|---------|-----------|
| Drive arrival/removal detection (Windows) | WMI — `Win32_LogicalDisk` events via `pythoncom` |
| Drive arrival/removal detection (Linux) | `pyudev` netlink socket monitoring |
| File-system change events | `watchdog` — inotify (Linux), ReadDirectoryChangesW (Windows) |
| Full directory manifest on first connect | `os.scandir` iterative walk, gzip-compressed TSV |
| Differential delta on reconnect | Manifest comparison keyed by volume serial / UUID |
| Volume identity across reconnects (Windows) | `GetVolumeInformationW` serial number |
| Volume identity across reconnects (Linux) | `ID_FS_UUID` from udev |
| Atomic file writes | Write-to-temp then `os.replace` |
| Log rotation | `logging.handlers.RotatingFileHandler` (5 MB × 5 backups) |

## Output

All files are written to `logs/` next to the script.

| File | Contents |
|------|----------|
| `monitor.log` | Script events, errors, and scan progress |
| `snapshot_<label>_<serial>_<timestamp>.tsv.gz` | Full manifest on first connect (gzip-compressed TSV) |
| `delta_<label>_<serial>_<timestamp>.tsv.gz` | Differential changes on reconnect (gzip-compressed TSV) |
| `events_<label>_<timestamp>.log` | File create/delete/move/modify activity |

### Manifest format

**Snapshot files** (`snapshot_*.tsv.gz`) — 4 tab-separated columns:

```
<relpath>	<size>	<mtime>	<flag>
```

**Delta files** (`delta_*.tsv.gz`) — 5 tab-separated columns, prefixing the same data with a change status (`+` added, `-` deleted, `~` modified):

```
<change>	<relpath>	<size>	<mtime>	<flag>
```

Field details:
- `size`: byte count for files, `-` for directories and symlinks
- `mtime`: UTC timestamp in `YYYY-MM-DDTHH:MM:SSZ` format
- `flag`: `F` (file), `D` (directory), `L` (symlink)

To read compressed files:

```bash
# Linux / macOS
zcat logs/snapshot_*.tsv.gz | less

# Python (any platform)
python -c "import gzip, sys; sys.stdout.buffer.write(gzip.open(sys.argv[1]).read())" logs/snapshot_*.tsv.gz
```

## How it works

**Drive detection** — Looks for removable block devices: on Windows via WMI `Win32_LogicalDisk` events (DriveType 2), on Linux via a pyudev netlink socket filtered to `block/partition` events with `ID_BUS=usb`.

**Snapshotting** — On first connect it walks the entire drive with `os.scandir` and writes a gzip-compressed TSV manifest. Each entry records relative path, size, mtime, and type (file / directory / symlink). Junk system directories (`$RECYCLE.BIN`, `System Volume Information`, etc.) are skipped.

**Delta mode** — On subsequent reconnects of the same volume (identified by serial number or filesystem UUID), the previous manifest is loaded and compared against a fresh scan. Only additions (`+`), deletions (`-`), and modifications (`~`) are written, making reconnect scans significantly faster for large drives.

**File-system monitoring** — Once the snapshot completes, `watchdog` attaches a live watcher that logs every create, delete, move, and modify event to a per-session events file. No polling — kernel notifications only.

**Single-threaded scanning** — The scanner deliberately uses one thread per drive. USB mass-storage has a single command queue; parallel threads cause seek thrash on FAT/exFAT and NTFS-over-USB, degrading throughput on slow flash hardware.

**Atomic writes** — All output files are written to a `.tmp` sibling first and renamed into place with `os.replace`, so a crash or cancellation never leaves a partial or corrupt manifest on disk.

## To Do

- [ ] Add an optional `--debug` flag to enable console output (in addition to log files).
- [ ] Add an optional auto-sync feature to sync specific files or directories from the USB drive.
- [ ] Add macOS support.
- [ ] Implement file hashing (SHA-256) for more robust change detection.
- [ ] Implement optional webhook notifications for drive connections and file events (must use encrypted connections).

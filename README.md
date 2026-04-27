<div align="center">

# USB Monitor

[![Python](https://img.shields.io/badge/python-3.9%2B-yellow?style=flat-square)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey?style=flat-square)](https://github.com)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

Script that detects USB drives and logs their contents. Prepared to be run as a background service.

</div>

## Requirements

- **Python 3.9+** and **pip**
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

Pass `--debug` to mirror log records to stdout in addition to the log file (useful when running interactively).

For persistent background execution use Task Scheduler (Windows) or a systemd unit (Linux).

## Features

- Drive arrival/removal detection (Windows) using WMI, `Win32_LogicalDisk` events via `pythoncom`
- Drive arrival/removal detection (Linux) using `pyudev` netlink socket monitoring
- Full directory manifest on first connect using `os.scandir` iterative walk, gzip-compressed TSV
- Differential delta on reconnect using manifest comparison keyed by volume serial / UUID
- Volume identity across reconnects (Windows) using `GetVolumeInformationW` serial number
- Volume identity across reconnects (Linux) using `ID_FS_UUID` from udev
- Atomic file writes using Write-to-temp then `os.replace`
- Log rotation using `logging.handlers.RotatingFileHandler` (5 MB × 5 backups)

## Output

All files are written to `logs/` next to the script.

| File | Contents |
|------|----------|
| `monitor.log` | Script events, errors, and scan progress |
| `snapshot_<label>_<serial>_<timestamp>.tsv.gz` | Full manifest on first connect (gzip-compressed TSV) |
| `delta_<label>_<serial>_<timestamp>.tsv.gz` | Differential changes on reconnect (gzip-compressed TSV) |

### Manifest format

**Snapshot files** (`snapshot_*.tsv.gz`), 5 tab-separated columns:

```
<relpath>	<size>	<mtime>	<hash>	<flag>
```

**Delta files** (`delta_*.tsv.gz`), 6 tab-separated columns, prefixing the same data with a change status (`+` added, `-` deleted, `~` modified):

```
<change>	<relpath>	<size>	<mtime>	<hash>	<flag>
```

Field details:
- `size`: byte count for files, `-` for directories and symlinks
- `mtime`: UTC timestamp in `YYYY-MM-DDTHH:MM:SSZ` format
- `hash`: SHA-256 hex digest of file contents, `-` for directories, symlinks, and unreadable files
- `flag`: `F` (file), `D` (directory), `L` (symlink)

Legacy 4-column snapshots (no `hash`) are still loaded for backward compatibility; the hash check is skipped when either side is `-`, so size/mtime/flag remains the primary signal.

To read compressed files:

```bash
# Linux / macOS
zcat logs/snapshot_*.tsv.gz | less

# Python (any platform)
python -c "import gzip,sys,glob;[sys.stdout.buffer.write(gzip.open(f).read())for a in sys.argv[1:]for f in glob.glob(a)]" logs/snapshot_*.tsv.gz
```

## How it works

**Drive detection** - Looks for removable block devices: on Windows via WMI `Win32_LogicalDisk` events (DriveType 2), on Linux via a pyudev netlink socket filtered to `block/partition` events with `ID_BUS=usb`.

**Snapshotting** - On first connect it walks the entire drive with `os.scandir` and writes a gzip-compressed TSV manifest. Each entry records relative path, size, mtime, SHA-256 of file contents, and type (file / directory / symlink). Hashing reads the full content of every file, so first-connect scans are I/O-bound on the drive's read throughput rather than just metadata. Junk system directories (`$RECYCLE.BIN`, `System Volume Information`, etc.) are skipped.

**Delta mode** - On subsequent reconnects of the same volume (identified by serial number or filesystem UUID), the previous manifest is loaded and compared against a fresh scan. A file is flagged `~` when size, mtime, or flag differs, or when both sides recorded a SHA-256 and the digests differ; the hash check catches edits that preserve size and mtime. Only additions (`+`), deletions (`-`), and modifications (`~`) are written.

**Single-threaded scanning** - The scanner deliberately uses one thread per drive. USB mass-storage has a single command queue; parallel threads cause seek thrash on FAT/exFAT and NTFS-over-USB, degrading throughput on slow flash hardware.

**Atomic writes** - All output files are written to a `.tmp` sibling first and renamed into place with `os.replace`, so a crash or cancellation never leaves a partial or corrupt manifest on disk.

## To do

- [X] Add an optional `--debug` flag to enable console output (in addition to log files).
- [ ] Add an optional auto-sync feature to sync specific files or directories from the USB drive.
- [ ] Add macOS support.
- [X] Implement file hashing (SHA-256) for more robust change detection.
- [ ] Implement optional webhook notifications for drive connections (must use encrypted connections).
- [ ] Verify gzip integrity after write by reopening and decompressing the file before discarding the `.tmp`.
- [ ] Persist a `latest_serial → manifest_path` JSON index to replace glob+sort lookup in `_load_manifest`.
- [ ] Handle clock skew / mtime rollback by adding a monotonic tie-breaker to `_timestamp()` filenames.
- [ ] Add a scan timeout / circuit-breaker to self-cancel `_scan_entries` after a configurable wall-clock limit.
- [ ] Detect and handle remount-without-removal instead of silently ignoring duplicate `Creation` events.
- [ ] Separate the scan result from disk I/O so `_snapshot` and `_write_delta` become pure writers over a returned manifest.
- [ ] Add structured JSON log output alongside the human-readable log for machine parsing.
- [ ] Implement log/manifest retention policy to prune old snapshots and deltas from `logs/`.
- [ ] Add a health-check heartbeat file (`logs/heartbeat`) with PID + timestamp for external supervisors.

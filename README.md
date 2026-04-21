# USB Monitor

Background script that detects USB drives and logs their contents and file activity. Runs silently, no terminal output.

## Requirements

```
pip install -r requirements.txt
```

## Usage

**Windows** (no console window)
```
pythonw monitor.py
```

**Linux**
```
python3 monitor.py &
```

For persistent background execution use Task Scheduler (Windows) or a systemd unit (Linux).

## Output

All files are written to `logs/` next to the script.

| File | Contents |
|------|----------|
| `monitor.log` | Script events, errors, and snapshot progress |
| `snapshot_<label>_<serial>_<timestamp>.tsv.gz` | Full manifest captured on first connect (gzip-compressed TSV) |
| `delta_<label>_<serial>_<timestamp>.tsv.gz` | Differential changes on reconnect (gzip-compressed TSV) |
| `events_<label>_<timestamp>.log` | File create/delete/move/modify activity |

### Manifest format

**Snapshot files** (`snapshot_*.tsv.gz`) contain 4 tab-separated columns:

```
<relpath>	<size>	<mtime>	<flag>
```

**Delta files** (`delta_*.tsv.gz`) contain 5 tab-separated columns, prefixing the same data with a change status (`+` added, `-` deleted, `~` modified):

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

- **Windows:** WMI watches `Win32_LogicalDisk` creation/deletion events (DriveType 2). The volume serial number is read via `GetVolumeInformationW`.
- **Linux:** pyudev listens on a netlink socket for block/partition udev events. The filesystem UUID (`ID_FS_UUID`) is used as the stable volume identifier.
- File system monitoring uses `watchdog` (inotify on Linux, ReadDirectoryChangesW on Windows), no polling.
- On first connect a full manifest is written; on subsequent reconnects of the same volume a differential delta is produced instead, making reconnect scans significantly faster for large drives.
- The scanner is deliberately single-threaded: USB mass-storage devices have a single command queue, so multiple threads cause seek thrash on FAT/exFAT and NTFS-over-USB.

## To Do

- [ ] Add an optional `--debug` flag to enable console output (in addition to log files).
- [ ] Add an optional auto-sync feature to sync specific files or directories from the USB drive.
- [ ] Add MacOS support.
- [ ] Implement file hashing (SHA-256) for more robust change detection.
- [ ] Implement optional webhook notifications for drive connections and file events (must use encrypted connections).

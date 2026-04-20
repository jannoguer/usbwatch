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

Each line in a snapshot or delta file is tab-separated:

```
<relpath>	<size>	<mtime>	<flag>
```

- `flag`: `F` (file), `D` (directory), `L` (symlink)
- `size`: byte count for files, `-` for directories and symlinks
- `mtime`: UTC timestamp in `YYYY-MM-DDTHH:MM:SSZ` format

Delta files additionally prefix each line with `+` (added), `-` (deleted), or `~` (modified).

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

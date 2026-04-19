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
| `monitor.log` | Script events and errors |
| `snapshot_<label>_<timestamp>.txt` | Directory tree captured on connect |
| `events_<label>_<timestamp>.log` | File create/delete/move/modify activity |

## How it works

- **Windows:** WMI watches `Win32_LogicalDisk` creation/deletion events (DriveType 2).
- **Linux:** pyudev listens on a netlink socket for block/partition udev events.
- File system monitoring uses `watchdog` (inotify on Linux, ReadDirectoryChangesW on Windows), no polling.

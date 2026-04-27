<div align="center">

# USB Monitor

[![Python](https://img.shields.io/badge/python-3.8%2B-yellow?style=flat-square)](https://www.python.org)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey?style=flat-square)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

Script that detects USB drives and logs their contents. Prepared to be run as a background service.

</div>

## Setup & Execution

Requires **Python 3.8+** and **pip**.

**Install dependencies:** 
```bash
pip install -r requirements.txt
```

**Run in the background** (append `--help` for optional arguments):
* **Windows:** `pythonw monitor.py` *(Use Task Scheduler for persistent execution)*
* **Linux:** `python3 monitor.py &` *(Use a systemd unit for persistent execution)*

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

To read compressed files:

```bash
# Linux / macOS
zcat logs/snapshot_*.tsv.gz | less

# Python (any platform)
python -c "import gzip,sys,glob;[sys.stdout.buffer.write(gzip.open(f).read())for a in sys.argv[1:]for f in glob.glob(a)]" logs/snapshot_*.tsv.gz
```

## To do features

- [X] Add an optional `--debug` flag to enable console output (in addition to log files).
- [ ] Add an optional auto-sync feature to sync specific files or directories from the USB drive.
- [ ] Add macOS support.
- [X] Implement file hashing (SHA-256) for more robust change detection.
- [ ] Make a log visualizer.
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

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

**Run USB monitoring in the background** (append `--help` for optional arguments):
* **Windows:** `pythonw monitor.py` *(Use Task Scheduler for persistent execution)*
* **Linux:** `python3 monitor.py &` *(Use a systemd unit for persistent execution)*

**Reconstruct a snapshot by applying deltas:**
```bash
python monitor.py materialize <snapshot.tsv.gz>
```
This command takes a baseline snapshot and applies all deltas that reference it, producing a materialized snapshot in `logs/` showing the final state after all changes.

## Output

All files are written to `logs/` next to the script.

| File | Contents |
|------|----------|
| `monitor.log` | Script events, errors, and scan progress |
| `snapshot_<label>_<serial>_<timestamp>.tsv.gz` | Full manifest on first connect (gzip-compressed TSV) |
| `delta_<label>_<serial>_<timestamp>.tsv.gz` | Differential changes on reconnect (gzip-compressed TSV) |
| `materialized_<label>_<timestamp>.tsv.gz` | Reconstructed snapshot with deltas applied (gzip-compressed TSV) |

### Manifest format

All manifest files start with a JSON metadata header line (prefixed with `#`), followed by TSV data.

**Snapshot files** (`snapshot_*.tsv.gz`):
```
#{"id":"<uuid>","type":"snapshot","created_at":"<iso8601>","drive":{...},"entries_count":<n>,"entries_sha256":"<hash>"}
<relpath>	<size>	<mtime>	<hash>	<flag>
```

**Delta files** (`delta_*.tsv.gz`):
```
#{"id":"<uuid>","type":"delta","created_at":"<iso8601>","baseline_id":"<uuid>","drive":{...},"entries_count":<n>,"entries_sha256":"<hash>"}
<change>	<relpath>	<size>	<mtime>	<hash>	<flag>
```

**Materialized files** (`materialized_*.tsv.gz`):
```
#{"id":"<uuid>","type":"materialized","created_at":"<iso8601>","baseline_snapshot":"<filename>","applied_deltas":["<uuid>"...],"drive":{...},"entries_count":<n>,"entries_sha256":"<hash>"}
<relpath>	<size>	<mtime>	<hash>	<flag>
```

Field details:
- `size`: byte count for files, `-` for directories and symlinks
- `mtime`: UTC timestamp in `YYYY-MM-DDTHH:MM:SSZ` format
- `hash`: SHA-256 hex digest of file contents, `-` for directories, symlinks, and unreadable files
- `flag`: `F` (file), `D` (directory), `L` (symlink)
- `change` (deltas only): `+` (added), `-` (deleted), `~` (modified)

To read compressed files:

```bash
# Linux / macOS
zcat logs/snapshot_*.tsv.gz | less

# Python (any platform)
python -c "import gzip,sys,glob;[sys.stdout.buffer.write(gzip.open(f).read())for a in sys.argv[1:]for f in glob.glob(a)]" logs/snapshot_*.tsv.gz
```

## To do

### New features

- [X] Add an optional `--debug` flag to enable console output (in addition to log files).
- [X] Implement file hashing (SHA-256) for more robust change detection.
- [ ] Add an optional auto-sync feature to sync specific files or directories from the USB drive.
- [ ] Add macOS support.
- [ ] Make a log visualizer.
- [ ] Implement optional webhook notifications for drive connections (must use encrypted connections).
- [ ] Add structured JSON log output alongside the human-readable log for machine parsing.
- [ ] Implement log/manifest retention policy to prune old snapshots and deltas from `logs/`.
- [ ] Add a health-check heartbeat file (`logs/heartbeat`) with PID + timestamp for external supervisors.
- [ ] Optionally enforce read-only mounting (Linux `remount,ro`; Windows `WriteProtect` registry flag) before scanning so the scan itself cannot alter mtimes.
- [ ] Drive serial whitelist/blacklist so the monitor only scans (or explicitly refuses) specific volumes.
- [ ] Add a suspicious-file flag column to manifests for `autorun.inf`, hidden files, and known executable extensions.
- [X] `monitor.py materialize <snapshot.tsv.gz>` subcommand that outputs a reconstructed snapshot equal to the given baseline with every delta recorded matching snapshot id.
- [X] Embed a JSON metadata header in each snapshot and delta (type, serial, label, timestamp, format version, and a baseline reference for deltas) so `materialize` can validate inputs and locate related files without parsing filenames or globbing.
- [ ] Live file watching (`inotify` / `ReadDirectoryChangesW`) while the drive stays connected, to capture changes between connect and disconnect.
- [ ] Config file support (TOML) so non-trivial settings (filters, webhooks, retention) don't require long CLI invocations.
- [ ] Optional SQLite backend as an alternative to `tsv.gz` for queryable manifest history.
- [ ] Capture full USB hardware metadata (vendor ID, product ID, hardware serial) alongside the filesystem volume serial.
- [ ] Include/exclude glob filters so users can skip large or uninteresting paths and extensions.

### Improvements

- [ ] Verify gzip integrity after write by reopening and decompressing the file before discarding the `.tmp`.
- [ ] Persist a `latest_serial → manifest_path` JSON index to replace glob+sort lookup in `_load_manifest`.
- [ ] Handle clock skew / mtime rollback by adding a monotonic tie-breaker to `_timestamp()` filenames.
- [ ] Add a scan timeout / circuit-breaker to self-cancel `_scan_entries` after a configurable wall-clock limit.
- [ ] Detect and handle remount-without-removal instead of silently ignoring duplicate `Creation` events.
- [ ] Separate the scan result from disk I/O so `_snapshot` and `_write_delta` become pure writers over a returned manifest.

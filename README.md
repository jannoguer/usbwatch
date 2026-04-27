<div align="center">

# USB Monitor

[![Python](https://img.shields.io/badge/python-3.8%2B-yellow?style=flat-square)](https://www.python.org)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey?style=flat-square)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE.md)

Script that detects USB drives and logs their contents. Prepared to be run as a background service.

</div>

## Setup & Execution

Requires **Python 3.8+** and **pip**.

**Install dependencies:** 
```bash
pip install -r requirements.txt
```

**Run USB monitoring in the background**:
* **Windows:** *(Use Task Scheduler for persistent execution)*
    ```cmd
    pythonw monitor.py
    ```

* **Linux:** *(Use a systemd unit for persistent execution)*
    ```bash
    python3 monitor.py &
    ```

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
| `snapshot_<label>_<serial>_<timestamp>.tsv.gz` | Full manifest on first connect |
| `delta_<label>_<serial>_<timestamp>.tsv.gz` | Differential changes on reconnect |
| `materialized_<label>_<timestamp>.tsv.gz` | Reconstructed snapshot with deltas applied |

### Manifest format

All manifest files start with a JSON metadata header line (prefixed with `#`), followed by TSV data.

**Snapshot files** (`snapshot_*.tsv.gz`):
```
<relpath>	<size>	<mtime>	<hash>	<flag>
```

**Delta files** (`delta_*.tsv.gz`):
```
<change>	<relpath>	<size>	<mtime>	<hash>	<flag>
```

**Materialized files** (`materialized_*.tsv.gz`):
```
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

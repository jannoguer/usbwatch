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

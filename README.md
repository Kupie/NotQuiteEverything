# NotQuiteEverything

A file/folder search utility for Windows, written entirely using the Python
3.10+ standard library. Run it with Python without installing Everything,
requesting administrator privileges, or using third-party Python packages.

## Run

```powershell
python run.py
```

Requires Python with tkinter support (normally included with the standard
python.org Windows installer). Settings and index.db are stored under
%LOCALAPPDATA%\NotQuiteEverything. An existing legacy profile in %APPDATA% is
copied to the new location when possible.

## Features

- Recursive initial indexing via os.scandir with SQLite persistence.
- Fast subsequent starts: the cached index is immediately usable after
  background initialization, without a full directory walk.
- Incremental unprivileged Windows filesystem notifications via
  ReadDirectoryChangesW (ctypes, no pip packages). Periodic full reconciliation
  remains enabled to recover missed events and offline changes.
- No giant in-memory record list, and no GUI-thread SQL filtering or sorting.
- SQLite FTS5 trigram acceleration when available (fallback: SQLite LIKE).
- Incomplete or cancelled scans keep previously indexed entries instead of
  deleting the entire index at the start.
- Multiple scan roots, folder/basename exclusion globs, background indexing,
  per-column sorting, pages of 500 matches, and accurate match counts.
- Windows Explorer reveal, Open, Copy Full Path and keyboard navigation.

## Search syntax

| Search | Behavior |
| --- | --- |
| report | Filename contains "report" |
| backup 2026 | Filename contains BOTH words |
| "project plan" | Filename contains this exact substring including the space |
| *.log or ext:log | File extension is .log |
| path:Downloads | Full path contains Downloads |
| folder:Projects *.py | Python files somewhere under paths containing Projects |

Comparisons use case-insensitive SQLite LIKE semantics (ASCII case folding by
default). Quoted phrases containing spaces are treated as a single term.

Click a column heading to sort; click again to reverse the order.
Double-click or Enter reveals the selected file in Explorer. Right-click
for Open, Open Containing Folder and Copy Full Path. Ctrl+C with the result
table focused copies the selected full path.

## Settings

Use Settings to change scan roots (one per line), exclusions (one glob per
line), rescan interval (seconds), or live monitoring. Restart the application
to apply changes. Example settings.json:

```json
{
  "scan_roots": ["C:\\", "D:\\", "\\\\server\\share"],
  "exclude_patterns": ["*/node_modules/*", "*/AppData/Local/Temp/*"],
  "rescan_interval_seconds": 1800,
  "watch_changes": true
}
```

Patterns with no slash match basenames; patterns containing slashes match
full paths using forward slashes regardless of the OS separator.
Unreachable and protected paths cannot be indexed. Directory symlinks and
junctions are not traversed.

## Design limitations

Everything uses NTFS-level indexing for its speed. This project still needs
a normal initial directory walk, and access to directories the account may
not have. ReadDirectoryChangesW requires appropriate permissions on monitored
directories and is not a durable event journal. The periodic full scan is
intentionally retained to recover offline changes, monitor overflows, and
filesystem events missed while the application was closed.

## Tests

```powershell
python -m unittest discover -s tests -v
```

No nonstandard runtime or test packages are required.

# NotQuiteEverything

A small Python/tkinter clone of the core idea behind [Everything](https://www.voidtools.com/)
by voidtools: instant, search-as-you-type lookup of files and folders — but
without admin privileges, since it can't read the NTFS MFT directly. Instead
it does a plain recursive directory walk with `os.scandir` and keeps the
result in memory, persisted to SQLite so later launches load instantly.

## Requirements

- Windows (uses `explorer /select,` and defaults to scanning `C:\`)
- Python 3.10+ with tkinter (standard on the official python.org installer;
  no third-party packages required)

## Running

```
python run.py
```

On first launch it walks `C:\` in a background thread (the GUI is usable
immediately, with a live "Indexing... N files" counter in the status bar).
On later launches it loads the previously saved index from SQLite instantly
instead of rescanning, then does its first background rescan after the
configured interval to pick up new/deleted files.

## Settings

Settings live in `%APPDATA%\NotQuiteEverything\settings.json` (created with
defaults on first run) and can be edited there directly or via the
**Settings...** button in the app:

```json
{
	"scan_drive": "C:\\",
	"rescan_interval_seconds": 300
}
```

The SQLite index database (`index.db`) lives alongside it in the same
directory.

## Project layout

- `notquiteeverything/models.py` — `FileRecord` dataclass
- `notquiteeverything/config.py` — settings load/save
- `notquiteeverything/indexer.py` — directory walk + SQLite persistence
- `notquiteeverything/search_engine.py` — debounced-friendly background substring search
- `notquiteeverything/gui.py` — tkinter UI
- `notquiteeverything/main.py` — wiring / entry point

## Notes / known limitations

- Only `C:\` is scanned by default. `config.py` has a comment describing how
  to extend `scan_drive` into a list of drives if you want to scan more than
  one.
- There's no filesystem change-notification (that needs elevated
  privileges), so freshness between rescans is bounded by
  `rescan_interval_seconds`.
- The results Treeview caps display at 5,000 rows for very broad queries
  (e.g. a single letter) to keep the UI responsive; the status bar shows the
  true match count vs. the number actually displayed. Narrowing the query
  shows more specific results.

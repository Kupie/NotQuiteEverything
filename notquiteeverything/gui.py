"""Tkinter frontend: paged SQL results, live updates, filters and context actions."""

import datetime
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .config import Settings
from .indexer import Indexer
from .models import FileRecord, SearchResult
from .search_engine import SearchEngine
from .watcher import WindowsDirectoryWatcher

DEBOUNCE_MS = 150
POLL_MS = 75
PAGE_SIZE = 500


def human_readable_size(value: int) -> str:
    if value < 0:
        return ""
    if value == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return ("%.0f" if unit == "B" else "%.1f") % value + " " + unit
        value /= 1024
    return ""


def format_mtime(value: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


class App:
    def __init__(self, root: tk.Tk, settings: Settings, indexer: Indexer, search_engine: SearchEngine):
        self.root = root
        self.settings = settings
        self.indexer = indexer
        self.search_engine = search_engine
        self._status_queue = queue.Queue()
        self._changes_queue = queue.Queue()
        self._watcher = None
        self._rescan_requested = threading.Event()
        self._debounce_id = None
        self._records: dict[str, FileRecord] = {}
        self._result = SearchResult()
        self._sort_column = "name"
        self._sort_descending = False
        self._page = 0
        self._note = "Initializing index..."
        self._scan_progress = None

        self._build_widgets()
        self.root.after(POLL_MS, self._poll_status)
        self.root.after(POLL_MS, self._poll_results)
        self._update_status()

    def _build_widgets(self) -> None:
        self.root.title("NotQuiteEverything")
        self.root.geometry("1050x650")
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=7, pady=7)

        ttk.Label(top, text="Search:").pack(side=tk.LEFT, padx=(0, 5))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.search_var.trace_add("write", self._on_query_changed)
        self.search_entry.focus_set()

        self.rebuild_button = ttk.Button(top, text="Rebuild Index", command=self._rebuild)
        self.rebuild_button.pack(side=tk.LEFT, padx=(7, 0))
        ttk.Button(top, text="Settings...", command=self._settings_dialog).pack(
            side=tk.LEFT, padx=(7, 0)
        )

        table = ttk.Frame(self.root)
        table.pack(fill=tk.BOTH, expand=True, padx=7)
        cols = ("name", "path", "size", "modified")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse")
        for column, heading, width, anchor in (
            ("name", "Name", 240, tk.W),
            ("path", "Containing Folder", 475, tk.W),
            ("size", "Size", 90, tk.E),
            ("modified", "Date Modified", 155, tk.CENTER),
        ):
            self.tree.heading(column, text=heading, command=lambda c=column: self._sort(c))
            self.tree.column(column, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self._open_location)
        self.tree.bind("<Return>", self._open_location)
        self.tree.bind("<Control-c>", self._copy_path)
        self.tree.bind("<Button-3>", self._context_menu)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill=tk.X, padx=7, pady=(5, 3))
        self.prev_button = ttk.Button(bottom, text="Previous", command=self._previous)
        self.prev_button.pack(side=tk.LEFT)
        self.page_var = tk.StringVar(value="Page 1")
        ttk.Label(bottom, textvariable=self.page_var).pack(side=tk.LEFT, padx=12)
        self.next_button = ttk.Button(bottom, text="Next", command=self._next)
        self.next_button.pack(side=tk.LEFT)
        self.status_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(
            side=tk.BOTTOM, fill=tk.X
        )
        self._update_paging()

    def start_initial_index(self) -> None:
        def boot():
            try:
                count, up_to_date = self.indexer.initialize()
            except Exception as exc:
                self._status_queue.put(("error", str(exc)))
                return
            self._status_queue.put(("loaded", count))
            if self.settings.watch_changes:
                self._watcher = WindowsDirectoryWatcher(
                    self.indexer.roots,
                    on_changes=lambda paths: self._changes_queue.put(paths),
                    on_reconcile=lambda root: self._rescan_requested.set(),
                    on_error=lambda error: self._status_queue.put(("notice", error)),
                )
                self._watcher.start()
            threading.Thread(target=self._change_worker, daemon=True, name="IndexChangeWorker").start()
            if not count or not up_to_date:
                self._run_scan()
            self.indexer.start_periodic_rescan(
                self.settings.rescan_interval_seconds,
                progress_callback=lambda n: self._status_queue.put(("progress", n)),
                on_complete=lambda n, success, error: self._status_queue.put(
                    ("complete", n, success, error)
                ),
            )
        threading.Thread(target=boot, daemon=True, name="IndexBootstrap").start()

    def _change_worker(self) -> None:
        pending = set()
        while not self.indexer.stop_event.is_set():
            try:
                paths = self._changes_queue.get(timeout=0.3)
                pending.update(paths)
                while True:
                    pending.update(self._changes_queue.get_nowait())
            except queue.Empty:
                pass
            if self._rescan_requested.is_set() and not self.indexer.is_scanning:
                self._rescan_requested.clear()
                self._run_scan()
                pending.clear()
                continue
            if pending and self.indexer.apply_changes(list(pending)):
                pending.clear()
                self._status_queue.put(("updated", self.indexer.indexed_count))

    def _run_scan(self) -> None:
        self._status_queue.put(("scan-started",))
        count, success, error = self.indexer.scan(
            progress_callback=lambda n: self._status_queue.put(("progress", n))
        )
        self._status_queue.put(("complete", count, success, error))

    def _rebuild(self) -> None:
        if self.indexer.is_scanning or not self.indexer.ready.is_set():
            return
        self.rebuild_button.state(["disabled"])
        self.indexer.start_background_scan(
            progress_callback=lambda n: self._status_queue.put(("progress", n)),
            on_complete=lambda n, success, error: self._status_queue.put(
                ("complete", n, success, error)
            ),
        )

    def _on_query_changed(self, *_args) -> None:
        self._page = 0
        self._schedule_search()

    def _schedule_search(self) -> None:
        if self._debounce_id is not None:
            self.root.after_cancel(self._debounce_id)
        self._debounce_id = self.root.after(DEBOUNCE_MS, self._submit_search)

    def _submit_search(self) -> None:
        self._debounce_id = None
        query = self.search_var.get()
        if not query.strip():
            self._result = SearchResult()
            self._render()
            return
        self.search_engine.submit_query(
            query, self._sort_column, self._sort_descending, PAGE_SIZE,
            self._page * PAGE_SIZE,
        )

    def _sort(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_column = column
            self._sort_descending = False
        self._page = 0
        self._schedule_search()

    def _poll_results(self) -> None:
        try:
            while True:
                _generation, query, result = self.search_engine.result_queue.get_nowait()
                if query == self.search_var.get():
                    self._result = result
                    self._render()
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_results)

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._records.clear()
        for i, record in enumerate(self._result.records):
            iid = str(i)
            self._records[iid] = record
            self.tree.insert("", tk.END, iid=iid, values=(
                record.name,
                os.path.dirname(record.path),
                "" if record.is_dir else human_readable_size(record.size),
                format_mtime(record.mtime),
            ))
        if self._result.error:
            self._note = "Search error: " + self._result.error
        self._update_paging()
        self._update_status()

    def _update_paging(self) -> None:
        total = self._result.total
        last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page_var.set("Page %s of %s" % (self._page + 1, last_page))
        self.prev_button.state(["!disabled"] if self._page > 0 else ["disabled"])
        self.next_button.state(
            ["!disabled"] if (self._page + 1) * PAGE_SIZE < total else ["disabled"]
        )

    def _previous(self) -> None:
        if self._page:
            self._page -= 1
            self._schedule_search()

    def _next(self) -> None:
        if (self._page + 1) * PAGE_SIZE < self._result.total:
            self._page += 1
            self._schedule_search()

    def _poll_status(self) -> None:
        refresh = False
        try:
            while True:
                event = self._status_queue.get_nowait()
                kind = event[0]
                if kind == "loaded":
                    self._note = "Cached index loaded"
                    refresh = True
                elif kind == "scan-started":
                    self._note = "Scanning..."
                    self._scan_progress = 0
                elif kind == "progress":
                    self._scan_progress = event[1]
                elif kind == "complete":
                    _, count, success, error = event
                    self._note = (
                        "Index ready (%s entries)" % format(count, ",")
                        if success else "Incomplete scan: " + (error or "cancelled or busy")
                    )
                    self._scan_progress = None
                    self.rebuild_button.state(["!disabled"])
                    refresh = True
                elif kind == "updated":
                    self._note = "Live index updated"
                    refresh = True
                elif kind in ("notice", "error"):
                    self._note = event[1]
        except queue.Empty:
            pass
        if refresh and self.search_var.get().strip():
            self._schedule_search()
        self._update_status()
        self.root.after(POLL_MS, self._poll_status)

    def _update_status(self) -> None:
        matches = self._result.total
        shown = len(self._result.records)
        prefix = ("Scanning: %s seen" % format(self._scan_progress, ",")
                  if self._scan_progress is not None
                  else "Indexed: %s" % format(self.indexer.indexed_count, ","))
        self.status_var.set("%s  |  Showing %s of %s matches  |  %s" % (
            prefix, format(shown, ","), format(matches, ","), self._note
        ))

    def _selected_path(self) -> str | None:
        selection = self.tree.selection()
        record = self._records.get(selection[0]) if selection else None
        return record.path if record else None

    def _copy_path(self, _event=None) -> None:
        path = self._selected_path()
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)

    def _open_location(self, _event=None) -> None:
        path = self._selected_path()
        if not path:
            return
        if not os.path.exists(path):
            messagebox.showwarning("File missing", "This path no longer exists:\n" + path)
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer.exe", "/select," + path])
            else:
                messagebox.showinfo("Windows only", "Explorer integration requires Windows.")
        except OSError as exc:
            messagebox.showerror("Explorer error", str(exc))

    def _open_file(self) -> None:
        path = self._selected_path()
        if path and os.path.exists(path):
            try:
                os.startfile(path)
            except (OSError, AttributeError) as exc:
                messagebox.showerror("Open failed", str(exc))

    def _context_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Open", command=self._open_file)
        menu.add_command(label="Open Containing Folder", command=self._open_location)
        menu.add_separator()
        menu.add_command(label="Copy Full Path", command=self._copy_path)
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def _settings_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.geometry("600x455")
        dialog.transient(self.root)
        dialog.grab_set()
        layout = ttk.Frame(dialog, padding=12)
        layout.pack(fill=tk.BOTH, expand=True)

        ttk.Label(layout, text="Scan roots (one drive, network share or folder per line):").pack(anchor=tk.W)
        roots = tk.Text(layout, height=4, width=65)
        roots.insert("1.0", "\n".join(self.settings.scan_roots))
        roots.pack(fill=tk.X, pady=(4, 10))

        ttk.Label(layout, text="Exclude glob patterns (one per line; blank = index everything accessible):").pack(anchor=tk.W)
        excludes = tk.Text(layout, height=4, width=65)
        excludes.insert("1.0", "\n".join(self.settings.exclude_patterns))
        excludes.pack(fill=tk.X, pady=(4, 10))

        interval_row = ttk.Frame(layout)
        interval_row.pack(fill=tk.X)
        ttk.Label(interval_row, text="Fallback full rescan (seconds, minimum 30):").pack(side=tk.LEFT)
        interval_var = tk.StringVar(value=str(self.settings.rescan_interval_seconds))
        ttk.Entry(interval_row, textvariable=interval_var, width=10).pack(side=tk.LEFT, padx=10)

        watcher_var = tk.BooleanVar(value=self.settings.watch_changes)
        ttk.Checkbutton(layout, text="Monitor Windows filesystem changes in real time",
                        variable=watcher_var).pack(anchor=tk.W, pady=10)

        ttk.Label(layout, text="Restart the application to apply changes to roots, watches or interval.",
                  foreground="gray").pack(anchor=tk.W)

        def save() -> None:
            scan_roots = [line.strip() for line in roots.get("1.0", tk.END).splitlines() if line.strip()]
            exclude_patterns = [line.strip() for line in excludes.get("1.0", tk.END).splitlines()
                                if line.strip()]
            try:
                interval = int(interval_var.get())
                if interval < 30 or not scan_roots or any(not os.path.isabs(p) for p in scan_roots):
                    raise ValueError()
            except ValueError:
                messagebox.showerror("Invalid settings", "Use absolute scan roots and a rescan interval >= 30.")
                return
            self.settings.scan_roots = scan_roots
            self.settings.exclude_patterns = exclude_patterns
            self.settings.rescan_interval_seconds = interval
            self.settings.watch_changes = watcher_var.get()
            try:
                self.settings.save()
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc))
                return
            dialog.destroy()
            self._note = "Settings saved; restart to apply"
            self._update_status()

        buttons = ttk.Frame(layout)
        buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

    def on_close(self) -> None:
        self.indexer.stop()
        self.search_engine.stop()
        if self._watcher:
            self._watcher.stop()
        self.root.destroy()

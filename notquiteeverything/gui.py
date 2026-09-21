"""Tkinter GUI: search box, results Treeview, status bar."""

import datetime
import queue
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from .config import Settings
from .indexer import Indexer
from .models import FileRecord
from .search_engine import SearchEngine

DEBOUNCE_MS = 150
POLL_INTERVAL_MS = 50
MAX_DISPLAY_RESULTS = 5000  # cap Treeview rows so a broad query stays responsive

COLUMNS = ("name", "path", "size", "modified")
COLUMN_HEADINGS = {
	"name": "Name",
	"path": "Path",
	"size": "Size",
	"modified": "Date Modified",
}


def human_readable_size(num_bytes: int) -> str:
	if num_bytes <= 0:
		return ""
	units = ("B", "KB", "MB", "GB", "TB")
	size = float(num_bytes)
	for unit in units:
		if size < 1024 or unit == units[-1]:
			return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
		size /= 1024
	return f"{size:.1f} PB"


def format_mtime(mtime: float) -> str:
	try:
		return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
	except (OverflowError, OSError, ValueError):
		return ""


class App:
	def __init__(self, root: tk.Tk, settings: Settings, indexer: Indexer, search_engine: SearchEngine):
		self.root = root
		self.settings = settings
		self.indexer = indexer
		self.search_engine = search_engine

		self._progress_queue: "queue.Queue[tuple[str, int]]" = queue.Queue()
		self._debounce_after_id = None
		self._current_results: list[FileRecord] = []
		self._sort_column = "name"
		self._sort_reverse = False

		self._build_widgets()
		self.root.after(POLL_INTERVAL_MS, self._poll_search_results)
		self.root.after(POLL_INTERVAL_MS, self._poll_progress)
		self._update_status()

	# -- widget construction -------------------------------------------------

	def _build_widgets(self) -> None:
		self.root.title("NotQuiteEverything")
		self.root.geometry("900x600")

		top_frame = ttk.Frame(self.root)
		top_frame.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

		ttk.Label(top_frame, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
		self.search_var = tk.StringVar()
		self.search_entry = ttk.Entry(top_frame, textvariable=self.search_var)
		self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
		self.search_entry.bind("<KeyRelease>", self._on_search_key)
		self.search_entry.focus_set()

		self.rebuild_button = ttk.Button(top_frame, text="Rebuild Index", command=self._on_rebuild_clicked)
		self.rebuild_button.pack(side=tk.LEFT, padx=(6, 0))

		settings_button = ttk.Button(top_frame, text="Settings...", command=self._open_settings_dialog)
		settings_button.pack(side=tk.LEFT, padx=(6, 0))

		tree_frame = ttk.Frame(self.root)
		tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

		self.tree = ttk.Treeview(tree_frame, columns=COLUMNS, show="headings", selectmode="browse")
		for col in COLUMNS:
			self.tree.heading(col, text=COLUMN_HEADINGS[col], command=lambda c=col: self._on_heading_click(c))
		self.tree.column("name", width=220)
		self.tree.column("path", width=380)
		self.tree.column("size", width=90, anchor=tk.E)
		self.tree.column("modified", width=150, anchor=tk.CENTER)

		vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
		self.tree.configure(yscrollcommand=vsb.set)
		self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
		vsb.pack(side=tk.RIGHT, fill=tk.Y)

		self.tree.bind("<Double-1>", self._on_double_click)

		self.status_var = tk.StringVar()
		status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
		status_bar.pack(side=tk.BOTTOM, fill=tk.X)

	# -- search ---------------------------------------------------------------

	def _on_search_key(self, _event=None) -> None:
		if self._debounce_after_id is not None:
			self.root.after_cancel(self._debounce_after_id)
		self._debounce_after_id = self.root.after(DEBOUNCE_MS, self._trigger_search)

	def _trigger_search(self) -> None:
		self._debounce_after_id = None
		self.search_engine.submit_query(self.search_var.get())

	def _poll_search_results(self) -> None:
		try:
			while True:
				_generation, query, results = self.search_engine.result_queue.get_nowait()
				if query == self.search_var.get():
					self._display_results(results)
		except queue.Empty:
			pass
		self.root.after(POLL_INTERVAL_MS, self._poll_search_results)

	def _display_results(self, results: list[FileRecord]) -> None:
		self._current_results = results
		self._render_sorted()

	def _render_sorted(self) -> None:
		key_funcs = {
			"name": lambda r: r.name.lower(),
			"path": lambda r: r.path.lower(),
			"size": lambda r: r.size,
			"modified": lambda r: r.mtime,
		}
		ordered = sorted(self._current_results, key=key_funcs[self._sort_column], reverse=self._sort_reverse)

		self.tree.delete(*self.tree.get_children())
		for record in ordered[:MAX_DISPLAY_RESULTS]:
			self.tree.insert(
				"",
				tk.END,
				values=(
					record.name,
					record.path,
					human_readable_size(record.size) if not record.is_dir else "",
					format_mtime(record.mtime),
				),
			)
		self._update_status(matched=len(ordered))

	def _on_heading_click(self, column: str) -> None:
		if self._sort_column == column:
			self._sort_reverse = not self._sort_reverse
		else:
			self._sort_column = column
			self._sort_reverse = False
		self._render_sorted()

	# -- indexing ---------------------------------------------------------------

	def start_initial_index(self) -> None:
		loaded = self.indexer.load_from_db()
		if loaded:
			self._update_status()
			self._start_periodic_rescan()
		else:
			self._run_background_scan(then_start_periodic=True)

	def _run_background_scan(self, then_start_periodic: bool) -> None:
		def progress_callback(count: int) -> None:
			self._progress_queue.put(("progress", count))

		def on_complete(count: int) -> None:
			self._progress_queue.put(("complete", count))
			if then_start_periodic:
				self._start_periodic_rescan()

		self.indexer.start_background_scan(progress_callback=progress_callback, on_complete=on_complete)

	def _start_periodic_rescan(self) -> None:
		def progress_callback(count: int) -> None:
			self._progress_queue.put(("progress", count))

		def on_complete(count: int) -> None:
			self._progress_queue.put(("complete", count))

		self.indexer.start_periodic_rescan(
			self.settings.rescan_interval_seconds,
			progress_callback=progress_callback,
			on_complete=on_complete,
		)

	def _on_rebuild_clicked(self) -> None:
		self.rebuild_button.state(["disabled"])
		self._run_background_scan(then_start_periodic=False)

	def _poll_progress(self) -> None:
		try:
			while True:
				kind, count = self._progress_queue.get_nowait()
				if kind == "complete":
					self.rebuild_button.state(["!disabled"])
				self._update_status(indexing_count=count if kind == "progress" else None)
		except queue.Empty:
			pass
		self.root.after(POLL_INTERVAL_MS, self._poll_progress)

	# -- misc ---------------------------------------------------------------

	def _on_double_click(self, _event=None) -> None:
		selection = self.tree.selection()
		if not selection:
			return
		values = self.tree.item(selection[0], "values")
		path = values[1]
		if sys.platform != "win32":
			messagebox.showinfo("Not supported", "Opening File Explorer is only available on Windows.")
			return
		try:
			subprocess.Popen(["explorer", f"/select,{path}"])
		except OSError as exc:
			messagebox.showerror("Failed to open Explorer", str(exc))

	def _open_settings_dialog(self) -> None:
		dialog = tk.Toplevel(self.root)
		dialog.title("Settings")
		dialog.resizable(False, False)
		dialog.transient(self.root)

		ttk.Label(dialog, text="Scan drive:").grid(row=0, column=0, sticky=tk.W, padx=8, pady=(8, 4))
		drive_var = tk.StringVar(value=self.settings.scan_drive)
		ttk.Entry(dialog, textvariable=drive_var, width=30).grid(row=0, column=1, padx=8, pady=(8, 4))

		ttk.Label(dialog, text="Rescan interval (seconds):").grid(row=1, column=0, sticky=tk.W, padx=8, pady=4)
		interval_var = tk.StringVar(value=str(self.settings.rescan_interval_seconds))
		ttk.Entry(dialog, textvariable=interval_var, width=30).grid(row=1, column=1, padx=8, pady=4)

		note = ttk.Label(
			dialog,
			text="Changes take effect after clicking Rebuild Index\nand/or restarting the application.",
			foreground="gray",
		)
		note.grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 4))

		def on_save() -> None:
			try:
				interval = int(interval_var.get())
			except ValueError:
				messagebox.showerror("Invalid value", "Rescan interval must be a whole number of seconds.")
				return
			self.settings.scan_drive = drive_var.get().strip() or self.settings.scan_drive
			self.settings.rescan_interval_seconds = interval
			self.settings.save()
			self.indexer.scan_drive = self.settings.scan_drive
			dialog.destroy()

		button_frame = ttk.Frame(dialog)
		button_frame.grid(row=3, column=0, columnspan=2, pady=8)
		ttk.Button(button_frame, text="Save", command=on_save).pack(side=tk.LEFT, padx=4)
		ttk.Button(button_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=4)

	def _update_status(self, indexing_count: int | None = None, matched: int | None = None) -> None:
		total = self.indexer.indexed_count
		if matched is None:
			matched = len(self._current_results)

		parts = [f"Indexed: {total:,} files/folders"]
		if indexing_count is not None:
			parts = [f"Indexing... {indexing_count:,} files"]
		elif self.indexer.is_scanning:
			parts.append("(rescanning...)")
		parts.append(f"Showing: {min(matched, MAX_DISPLAY_RESULTS):,} of {matched:,} matches")
		self.status_var.set("  |  ".join(parts))

	def on_close(self) -> None:
		self.indexer.stop()
		self.search_engine.stop()
		self.root.destroy()

"""Builds and maintains the in-memory file/folder index, backed by SQLite.

Indexing approach
------------------
Without admin rights we can't read the NTFS Master File Table directly (that
needs raw access to \\.\C: and elevated privileges), so instead we do a plain
recursive directory walk starting at the configured scan root (``C:\`` by
default). We use ``os.scandir`` rather than ``os.walk`` because ``scandir``
yields ``DirEntry`` objects whose ``.is_dir()`` / ``.stat()`` are backed by
data already returned by the OS in the same readdir-style call on Windows,
avoiding a second per-entry ``stat`` syscall that ``os.walk`` + ``os.path``
would incur.

The walk is iterative (an explicit stack of directories to visit) rather
than recursive, both to avoid Python recursion-depth limits on deeply
nested trees and to make it trivial to interleave periodic progress
reporting and batched DB writes.

Any directory we can't read (``PermissionError`` / other ``OSError`` --
typical for things like ``C:\System Volume Information`` or per-user
protected folders when running unprivileged) is skipped: we log it and
keep walking siblings, rather than aborting the whole scan.
"""

import logging
import os
import sqlite3
import threading
from pathlib import Path

from .models import FileRecord

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
	path TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	is_dir INTEGER NOT NULL,
	size INTEGER NOT NULL,
	mtime REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
"""

# How many freshly-scanned entries to buffer before flushing to the
# in-memory list + SQLite. Keeps memory/DB writes batched without holding
# the *entire* scan result until the very end (so search can see partial
# results while a scan is still running).
_FLUSH_BATCH_SIZE = 2000


class Indexer:
	def __init__(self, scan_drive: str, db_path: Path):
		self.scan_drive = scan_drive
		self.db_path = db_path

		self._records: list[FileRecord] = []
		self._records_lock = threading.Lock()

		self.indexed_count = 0
		self.is_scanning = False
		self._stop_event = threading.Event()
		self._scan_lock = threading.Lock()  # only one scan (manual or periodic) at a time

	# -- reading the in-memory index -------------------------------------

	def get_records_snapshot(self) -> list[FileRecord]:
		"""Return the current list of records.

		The list is replaced wholesale (never mutated in place) whenever new
		data is flushed in, so handing out the live reference is safe for a
		reader even while a scan is concurrently appending -- the reader
		just sees the index as of the moment it asked.
		"""
		with self._records_lock:
			return self._records

	# -- persistence -------------------------------------------------------

	def _connect(self) -> sqlite3.Connection:
		con = sqlite3.connect(str(self.db_path))
		con.executescript(_SCHEMA)
		return con

	def load_from_db(self) -> bool:
		"""Load a previously persisted index instantly. Returns True on success."""
		if not self.db_path.exists():
			return False
		try:
			con = self._connect()
			cur = con.execute("SELECT name, path, is_dir, size, mtime FROM files")
			records = [
				FileRecord(name=row[0], path=row[1], is_dir=bool(row[2]), size=row[3], mtime=row[4])
				for row in cur
			]
			con.close()
		except sqlite3.Error:
			logger.exception("Failed to load index from %s", self.db_path)
			return False

		if not records:
			return False

		with self._records_lock:
			self._records = records
		self.indexed_count = len(records)
		return True

	def _flush(self, con: sqlite3.Connection, batch: list[FileRecord], replace_all: bool) -> None:
		if replace_all:
			con.execute("DELETE FROM files")
		con.executemany(
			"INSERT OR REPLACE INTO files (path, name, is_dir, size, mtime) VALUES (?, ?, ?, ?, ?)",
			[(r.path, r.name, int(r.is_dir), r.size, r.mtime) for r in batch],
		)
		con.commit()

		with self._records_lock:
			# First flush of a fresh scan replaces the old in-memory list;
			# subsequent flushes in the same scan append to it.
			if replace_all:
				self._records = list(batch)
			else:
				self._records = self._records + batch

	# -- scanning ------------------------------------------------------------

	def scan(self, progress_callback=None) -> int:
		"""Walk ``self.scan_drive`` and rebuild the index.

		Runs synchronously on the calling thread -- callers that want this
		off the GUI thread should invoke it from a background ``Thread``
		(see :meth:`start_background_scan`).

		If a scan (manual rebuild or periodic) is already in progress, this
		is a no-op that returns the current count, rather than letting two
		scans write to the database concurrently.
		"""
		if not self._scan_lock.acquire(blocking=False):
			return self.indexed_count

		self.is_scanning = True
		count = 0
		batch: list[FileRecord] = []
		first_flush = True

		con = self._connect()
		try:
			stack = [self.scan_drive]
			while stack and not self._stop_event.is_set():
				current_dir = stack.pop()
				try:
					with os.scandir(current_dir) as it:
						for entry in it:
							try:
								is_dir = entry.is_dir(follow_symlinks=False)
								stat = entry.stat(follow_symlinks=False)
							except (PermissionError, OSError) as exc:
								logger.debug("Skipping entry %s: %s", entry.path, exc)
								continue

							batch.append(
								FileRecord(
									name=entry.name,
									path=entry.path,
									is_dir=is_dir,
									size=0 if is_dir else stat.st_size,
									mtime=stat.st_mtime,
								)
							)
							count += 1

							if is_dir:
								stack.append(entry.path)

							if len(batch) >= _FLUSH_BATCH_SIZE:
								self._flush(con, batch, replace_all=first_flush)
								first_flush = False
								batch = []
								if progress_callback:
									progress_callback(count)
				except (PermissionError, OSError) as exc:
					# Protected/system directory -- log and move on.
					logger.debug("Skipping directory %s: %s", current_dir, exc)
					continue

			if batch or first_flush:
				self._flush(con, batch, replace_all=first_flush)
				if progress_callback:
					progress_callback(count)
		finally:
			con.close()
			self._scan_lock.release()

		self.indexed_count = count
		self.is_scanning = False
		return count

	def start_background_scan(self, progress_callback=None, on_complete=None) -> threading.Thread:
		def run():
			count = self.scan(progress_callback=progress_callback)
			if on_complete:
				on_complete(count)

		thread = threading.Thread(target=run, daemon=True, name="IndexerScan")
		thread.start()
		return thread

	def start_periodic_rescan(self, interval_seconds: int, progress_callback=None, on_complete=None) -> threading.Thread:
		"""Re-walk the tree every ``interval_seconds`` to catch new/deleted
		files, since there is no admin-level change notification available
		without elevated privileges.
		"""

		def loop():
			while not self._stop_event.wait(interval_seconds):
				self.scan(progress_callback=progress_callback)
				if on_complete:
					on_complete(self.indexed_count)

		thread = threading.Thread(target=loop, daemon=True, name="IndexerPeriodicRescan")
		thread.start()
		return thread

	def stop(self) -> None:
		self._stop_event.set()

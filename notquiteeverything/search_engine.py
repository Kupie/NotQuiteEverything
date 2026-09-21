"""Background substring search over the in-memory index.

The GUI debounces keystrokes (~150ms, see gui.py) before ever calling
``submit_query``, so this module doesn't debounce -- it just guarantees
that filtering a (possibly large) index never runs on the Tk main thread,
and that only the *latest* submitted query's results ever get delivered
(a "latest wins" pattern, so a burst of fast typing doesn't cause slow
filters to overwrite fresher results out of order).
"""

import queue
import threading

from .indexer import Indexer
from .models import FileRecord


class SearchEngine:
	def __init__(self, indexer: Indexer):
		self.indexer = indexer

		self._lock = threading.Lock()
		self._pending_query: str | None = None
		self._generation = 0
		self._wake_event = threading.Event()
		self._stop_event = threading.Event()

		self.result_queue: "queue.Queue[tuple[int, str, list[FileRecord]]]" = queue.Queue()

		self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="SearchWorker")

	def start(self) -> None:
		self._worker.start()

	def stop(self) -> None:
		self._stop_event.set()
		self._wake_event.set()

	def submit_query(self, query: str) -> None:
		"""Ask the worker thread to (re-)filter using ``query``.

		Safe to call rapidly; only the most recent call before the worker
		picks it up actually gets filtered.
		"""
		with self._lock:
			self._generation += 1
			self._pending_query = query
		self._wake_event.set()

	def _worker_loop(self) -> None:
		while not self._stop_event.is_set():
			self._wake_event.wait()
			self._wake_event.clear()
			if self._stop_event.is_set():
				return

			with self._lock:
				query = self._pending_query
				generation = self._generation
				self._pending_query = None
			if query is None:
				continue

			results = self._filter(query)

			with self._lock:
				stale = generation != self._generation
			if stale:
				continue  # a newer query has already been submitted; drop this result

			self.result_queue.put((generation, query, results))

	def _filter(self, query: str) -> list[FileRecord]:
		records = self.indexer.get_records_snapshot()
		q = query.strip().lower()
		if not q:
			return []
		return [r for r in records if q in r.name.lower()]

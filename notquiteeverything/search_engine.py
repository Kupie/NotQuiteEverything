"""Background search over the in-memory index.

The GUI debounces keystrokes (~150ms, see gui.py) before ever calling
``submit_query``, so this module doesn't debounce -- it just guarantees
that filtering a (possibly large) index never runs on the Tk main thread,
and that only the *latest* submitted query's results ever get delivered
(a "latest wins" pattern, so a burst of fast typing doesn't cause slow
filters to overwrite fresher results out of order).

Matching: a plain query (no ``*``/``?``/``[``) is a case-insensitive
substring match against the filename, e.g. ``commit`` matches
``AutoCommit_pro.html``. A query containing any of those characters is
instead treated as an Everything-style glob pattern matched against the
*whole* filename, e.g. ``*commit*html`` also matches ``AutoCommit_pro.html``.
"""

import fnmatch
import queue
import re
import threading

from .indexer import Indexer
from .models import FileRecord

_WILDCARD_CHARS = ("*", "?", "[")


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
		q = query.strip()
		if not q:
			return []

		if any(ch in q for ch in _WILDCARD_CHARS):
			# fnmatch.translate() -> a fully-anchored regex, matched against
			# the whole filename. Compiled via `re` directly (rather than
			# fnmatch.fnmatch) so case-insensitivity doesn't depend on
			# os.path.normcase, which is case-sensitive on non-Windows.
			pattern = re.compile(fnmatch.translate(q), re.IGNORECASE)
			return [r for r in records if pattern.match(r.name)]

		q_lower = q.lower()
		return [r for r in records if q_lower in r.name.lower()]

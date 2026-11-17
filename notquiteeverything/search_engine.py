"""Coalescing background queries; the Tk main thread never touches SQLite."""

import queue
import threading

from .indexer import Indexer
from .models import SearchResult


class SearchEngine:
    def __init__(self, indexer: Indexer):
        self.indexer = indexer
        self._lock = threading.Lock()
        self._pending = None
        self._generation = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.result_queue: queue.Queue[tuple[int, str, SearchResult]] = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True, name="SearchWorker")

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def submit_query(
        self, query: str, sort_column: str = "name",
        descending: bool = False, limit: int = 500, offset: int = 0,
    ) -> None:
        with self._lock:
            self._generation += 1
            self._pending = (self._generation, query, sort_column, descending, limit, offset)
        self._wake.set()

    def _work(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                request = self._pending
                self._pending = None
            if request is None:
                continue
            generation, query, sort, descending, limit, offset = request
            if not self.indexer.ready.wait(timeout=10):
                result = SearchResult(error="Index database is still initializing")
            else:
                try:
                    result = self.indexer.search(query, sort, descending, limit, offset)
                except Exception as exc:
                    result = SearchResult(error=str(exc))
            with self._lock:
                if generation != self._generation:
                    continue
            # Discard already queued older results, including after a rescan.
            try:
                while True:
                    self.result_queue.get_nowait()
            except queue.Empty:
                pass
            self.result_queue.put((generation, query, result))

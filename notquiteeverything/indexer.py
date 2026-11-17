"""Durable SQLite index, resilient full scans, and targeted filesystem updates.

The previous design copied an ever-growing Python list after every batch and
deleted the old index at the beginning of a new scan. This implementation
queries SQLite directly, maintains a trigram index when supported, and only
removes stale entries after a *successful* complete scan.
"""

import fnmatch
from contextlib import closing
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path

from .config import Settings
from .models import FileRecord, SearchResult

log = logging.getLogger(__name__)
BATCH_SIZE = 2000
PAGE_LIMIT = 1000
TERM_PATTERN = re.compile(r'"[^"]*"|\S+')


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _subtree_prefix(path: str) -> str:
    return path.rstrip("\\/") + os.sep


class Indexer:
    def __init__(self, settings: Settings):
        if settings.db_path is None:
            raise ValueError("settings.db_path is required")
        self.settings = settings
        self.db_path = Path(settings.db_path)
        self.roots = [os.path.normpath(os.path.abspath(os.path.expandvars(root)))
                      for root in settings.scan_roots]
        self.excludes = [pattern.replace("\\", "/").casefold() for pattern in settings.exclude_patterns]
        # The index is commonly inside a watched scan root (e.g. C:\\).
        # Ignore our own SQLite files or each DB write causes more watches/writes.
        self._internal_paths = {
            os.path.normcase(os.path.abspath(str(self.db_path) + suffix))
            for suffix in ("", "-wal", "-shm", "-journal")
        }
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self._scan_lock = threading.Lock()
        self.is_scanning = False
        self.indexed_count = 0
        self.fts_enabled = False
        self.last_error = ""
        self.last_scan_complete = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _signature(self) -> str:
        return json.dumps({"roots": self.roots, "excludes": self.excludes}, sort_keys=True)

    def initialize(self) -> tuple[int, bool]:
        """Initialize or migrate the database; return (cached count, cache matches settings)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY COLLATE NOCASE,
                    name TEXT NOT NULL, is_dir INTEGER NOT NULL,
                    size INTEGER NOT NULL, mtime REAL NOT NULL,
                    generation TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(files)")}
            if "generation" not in columns:
                con.execute("ALTER TABLE files ADD COLUMN generation TEXT NOT NULL DEFAULT ''")
            con.execute("CREATE INDEX IF NOT EXISTS idx_files_name_nocase ON files(name COLLATE NOCASE)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime)")
            con.execute("CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            existing = con.execute("SELECT name FROM sqlite_master WHERE name='files_fts'").fetchone()
            try:
                con.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
                    USING fts5(name, content='files', content_rowid='rowid', tokenize='trigram')
                """)
                con.executescript("""
                    CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                        INSERT INTO files_fts(rowid, name) VALUES(new.rowid, new.name);
                    END;
                    CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                        INSERT INTO files_fts(files_fts, rowid, name)
                        VALUES('delete', old.rowid, old.name);
                    END;
                    CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF name ON files
                    WHEN old.name <> new.name BEGIN
                        INSERT INTO files_fts(files_fts, rowid, name)
                        VALUES('delete', old.rowid, old.name);
                        INSERT INTO files_fts(rowid, name) VALUES(new.rowid, new.name);
                    END;
                """)
                if not existing:
                    con.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
                self.fts_enabled = True
            except sqlite3.OperationalError as exc:
                log.warning("FTS5 trigram indexing unavailable; falling back to SQLite LIKE: %s", exc)
                self.fts_enabled = False
            self.indexed_count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            row = con.execute("SELECT value FROM index_meta WHERE key='scan_signature'").fetchone()
            valid = bool(row and row["value"] == self._signature())
            con.commit()
        self.ready.set()
        return self.indexed_count, valid

    def _excluded(self, path: str) -> bool:
        if os.path.normcase(os.path.abspath(path)) in self._internal_paths:
            return True
        normalized = path.replace("\\", "/").casefold()
        basename = normalized.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatchcase(normalized, pattern) or
                   (pattern.endswith("/*") and fnmatch.fnmatchcase(normalized, pattern[:-2])) or
                   ("/" not in pattern and fnmatch.fnmatchcase(basename, pattern))
                   for pattern in self.excludes)

    @staticmethod
    def _record(path: str, is_dir: bool, stat) -> tuple:
        return path, os.path.basename(path), int(is_dir), 0 if is_dir else stat.st_size, stat.st_mtime

    @staticmethod
    def _upsert(con: sqlite3.Connection, batch: list[tuple], generation: str) -> None:
        con.executemany("""
            INSERT INTO files(path, name, is_dir, size, mtime, generation)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name=excluded.name, is_dir=excluded.is_dir, size=excluded.size,
                mtime=excluded.mtime, generation=excluded.generation
        """, [(path, name, kind, size, mtime, generation)
              for path, name, kind, size, mtime in batch])
        con.commit()

    @staticmethod
    def _preserve_subtree(con: sqlite3.Connection, path: str, generation: str) -> None:
        prefix = _escape_like(_subtree_prefix(path)) + "%"
        con.execute("""
            UPDATE files SET generation=?
            WHERE path=? OR path LIKE ? ESCAPE '!'
        """, (generation, path, prefix))

    def scan(self, progress_callback=None) -> tuple[int, bool, str]:
        if not self._scan_lock.acquire(blocking=False):
            return self.indexed_count, False, "Another index operation is running"
        self.is_scanning = True
        self.last_error = ""
        self.last_scan_complete = False
        seen = 0
        complete = True
        errors = []
        generation = uuid.uuid4().hex
        con = None
        try:
            self.ready.wait()
            con = self._connect()
            batch = []
            for root in self.roots:
                if self.stop_event.is_set():
                    complete = False
                    break
                if not os.path.isdir(root):
                    complete = False
                    errors.append("Scan root unavailable: " + root)
                    continue
                stack = [root]
                while stack and not self.stop_event.is_set():
                    directory = stack.pop()
                    if self._excluded(directory):
                        continue
                    try:
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                if self.stop_event.is_set():
                                    break
                                if self._excluded(entry.path):
                                    continue
                                try:
                                    is_dir = entry.is_dir(follow_symlinks=False)
                                    stat = entry.stat(follow_symlinks=False)
                                except OSError:
                                    self._preserve_subtree(con, entry.path, generation)
                                    continue
                                batch.append(self._record(entry.path, is_dir, stat))
                                seen += 1
                                if is_dir:
                                    stack.append(entry.path)
                                if len(batch) >= BATCH_SIZE:
                                    self._upsert(con, batch, generation)
                                    batch.clear()
                                    if progress_callback:
                                        progress_callback(seen)
                    except OSError as exc:
                        # Preserve old results under inaccessible subdirectories.
                        self._preserve_subtree(con, directory, generation)
                        log.debug("Unable to scan %s: %s", directory, exc)
                if self.stop_event.is_set():
                    complete = False
            if not self.stop_event.is_set() and batch:
                self._upsert(con, batch, generation)
                if progress_callback:
                    progress_callback(seen)
            if self.stop_event.is_set():
                complete = False
            if complete:
                # No destructive sweep occurs unless every configured root was reachable.
                con.execute("DELETE FROM files WHERE generation <> ?", (generation,))
                con.execute("""
                    INSERT INTO index_meta(key, value) VALUES('scan_signature', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """, (self._signature(),))
                con.commit()
            else:
                con.commit()
            self.indexed_count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            self.last_scan_complete = complete
            self.last_error = "; ".join(errors)
            return self.indexed_count, complete, self.last_error
        except Exception as exc:
            log.exception("Index scan failed")
            self.last_error = str(exc)
            return self.indexed_count, False, self.last_error
        finally:
            if con is not None:
                con.close()
            self.is_scanning = False
            self._scan_lock.release()

    def start_background_scan(self, progress_callback=None, on_complete=None) -> threading.Thread:
        def worker():
            count, success, error = self.scan(progress_callback)
            if on_complete:
                on_complete(count, success, error)
        thread = threading.Thread(target=worker, daemon=True, name="IndexerScan")
        thread.start()
        return thread

    def start_periodic_rescan(self, interval_seconds: int, progress_callback=None, on_complete=None) -> threading.Thread:
        def worker():
            while not self.stop_event.wait(max(30, interval_seconds)):
                result = self.scan(progress_callback)
                if on_complete:
                    on_complete(*result)
        thread = threading.Thread(target=worker, daemon=True, name="PeriodicReconcile")
        thread.start()
        return thread

    @staticmethod
    def _remove_subtree(con: sqlite3.Connection, path: str) -> None:
        prefix = _escape_like(_subtree_prefix(path)) + "%"
        con.execute("DELETE FROM files WHERE path=? OR path LIKE ? ESCAPE '!'",
                    (path, prefix))

    def apply_changes(self, paths: list[str]) -> bool:
        """Update changed files, including descendants of newly created/renamed folders.

        Return False when a full scan holds the writer lock, so the caller
        can retry instead of silently dropping notifications.
        """
        if not self.ready.is_set() or not self._scan_lock.acquire(blocking=False):
            return False
        try:
            with closing(self._connect()) as con:
                batch = []
                for raw in set(paths):
                    path = os.path.normpath(raw)
                    if not any(os.path.normcase(path) == os.path.normcase(root) or
                               os.path.normcase(path).startswith(os.path.normcase(_subtree_prefix(root)))
                               for root in self.roots):
                        continue
                    if self._excluded(path):
                        self._remove_subtree(con, path)
                        continue
                    if not os.path.exists(path):
                        self._remove_subtree(con, path)
                        continue
                    if os.path.isdir(path):
                        # Reconcile the whole subtree, including removed children.
                        self._remove_subtree(con, path)
                        stack = [path]
                        while stack:
                            current = stack.pop()
                            if self._excluded(current):
                                continue
                            try:
                                stat = os.stat(current, follow_symlinks=False)
                                batch.append(self._record(current, True, stat))
                                with os.scandir(current) as entries:
                                    for entry in entries:
                                        if self._excluded(entry.path):
                                            continue
                                        try:
                                            is_dir = entry.is_dir(follow_symlinks=False)
                                            stat = entry.stat(follow_symlinks=False)
                                        except OSError:
                                            continue
                                        if is_dir:
                                            stack.append(entry.path)
                                        else:
                                            batch.append(self._record(entry.path, False, stat))
                            except OSError:
                                continue
                    else:
                        # A directory may have been replaced by a plain file.
                        con.execute("DELETE FROM files WHERE path LIKE ? ESCAPE '!'",
                                    (_escape_like(_subtree_prefix(path)) + "%",))
                        try:
                            stat = os.stat(path, follow_symlinks=False)
                            batch.append(self._record(path, False, stat))
                        except OSError:
                            continue
                if batch:
                    # Existing rows retain their generation during incremental updates.
                    # A subsequent full scan assigns a new generation to every seen row.
                    con.executemany("""
                        INSERT INTO files(path, name, is_dir, size, mtime, generation)
                        VALUES (?, ?, ?, ?, ?, '')
                        ON CONFLICT(path) DO UPDATE SET
                            name=excluded.name, is_dir=excluded.is_dir,
                            size=excluded.size, mtime=excluded.mtime
                    """, batch)
                con.commit()
                self.indexed_count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return True
        finally:
            self._scan_lock.release()

    def search(
        self, query: str, sort_column: str = "name", descending: bool = False,
        limit: int = 500, offset: int = 0,
    ) -> SearchResult:
        if not query.strip():
            return SearchResult()
        terms = []
        conditions, params = [], []
        for item in TERM_PATTERN.findall(query.strip()):
            token = item.strip('"')
            if not token:
                continue
            low = token.casefold()
            if low.startswith("ext:"):
                ext = low[4:].lstrip(".")
                if ext:
                    conditions.append("name LIKE ? ESCAPE '!'")
                    params.append("%." + _escape_like(ext))
            elif low.startswith("*."):
                conditions.append("name LIKE ? ESCAPE '!'")
                params.append("%" + _escape_like(low[1:]))
            elif low.startswith(("path:", "folder:")):
                fragment = token.split(":", 1)[1]
                if fragment:
                    conditions.append("path LIKE ? ESCAPE '!'")
                    params.append("%" + _escape_like(fragment) + "%")
            else:
                terms.append(token)
                conditions.append("name LIKE ? ESCAPE '!'")
                params.append("%" + _escape_like(token) + "%")
        if not conditions:
            return SearchResult()
        if self.fts_enabled:
            candidates = [t for t in terms if len(t) >= 3]
            if candidates:
                longest = max(candidates, key=len)
                # Quote an FTS5 phrase; LIKE still checks exact substring semantics.
                fts_phrase = '"' + longest.replace('"', '""') + '"'
                conditions.append("rowid IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
                params.append(fts_phrase)
        sort_keys = {
            "name": "name COLLATE NOCASE, path COLLATE NOCASE",
            "path": "path COLLATE NOCASE",
            "size": "size, name COLLATE NOCASE",
            "modified": "mtime, name COLLATE NOCASE",
        }
        direction = "DESC" if descending else "ASC"
        order = ", ".join(part + " " + direction
                          for part in sort_keys.get(sort_column, sort_keys["name"]).split(", "))
        clause = " AND ".join(conditions)
        limit = min(PAGE_LIMIT, max(1, int(limit)))
        offset = max(0, int(offset))
        with closing(self._connect()) as con:
            rows = con.execute(
                f"SELECT name, path, is_dir, size, mtime FROM files WHERE {clause} "
                f"ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset)
            ).fetchall()
            total = con.execute("SELECT COUNT(*) FROM files WHERE " + clause, params).fetchone()[0]
        records = [FileRecord(row["name"], row["path"], bool(row["is_dir"]), row["size"], row["mtime"])
                   for row in rows]
        return SearchResult(records=records, total=total, offset=offset, limit=limit)

    def stop(self) -> None:
        self.stop_event.set()

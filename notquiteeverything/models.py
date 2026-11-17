"""Shared data structures."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class FileRecord:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float


@dataclass(slots=True)
class SearchResult:
    records: list[FileRecord] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 500
    error: str = ""

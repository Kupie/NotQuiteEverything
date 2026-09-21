"""Data model for a single indexed filesystem entry."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileRecord:
	name: str
	path: str
	is_dir: bool
	size: int
	mtime: float

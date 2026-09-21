"""Settings load/save.

Settings (scan drive + rescan interval) live in a small JSON file under the
user's per-app data directory, so they persist across launches and can be
edited by hand without touching source. The SQLite index database lives
next to it.

NOTE (future expansion): today ``scan_drive`` is a single path and the GUI
only ever indexes that one location. To support scanning multiple drives,
change ``scan_drive`` to ``scan_drives: list[str]`` here, update
``Settings.load``/``save`` to (de)serialize a list, and have
``Indexer.scan`` iterate over each root instead of the single
``self.scan_drive``. Everything else (persistence, search, GUI) is already
drive-agnostic since it only ever deals with absolute paths.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "NotQuiteEverything"

DEFAULT_SCAN_DRIVE = "C:\\"
DEFAULT_RESCAN_INTERVAL_SECONDS = 300  # 5 minutes


def get_app_data_dir() -> Path:
	"""Return (and create) the per-user directory for settings + index db."""
	if os.name == "nt":
		base = os.environ.get("APPDATA") or str(Path.home())
	else:
		# Non-Windows fallback, useful for development/testing this codebase.
		base = str(Path.home())
	app_dir = Path(base) / APP_NAME
	app_dir.mkdir(parents=True, exist_ok=True)
	return app_dir


@dataclass
class Settings:
	scan_drive: str = DEFAULT_SCAN_DRIVE
	rescan_interval_seconds: int = DEFAULT_RESCAN_INTERVAL_SECONDS

	# Not persisted in the JSON file itself, just carried alongside it.
	config_path: Path = None
	db_path: Path = None

	@classmethod
	def load(cls, app_dir: Path | None = None) -> "Settings":
		app_dir = app_dir or get_app_data_dir()
		app_dir.mkdir(parents=True, exist_ok=True)
		config_path = app_dir / "settings.json"
		db_path = app_dir / "index.db"

		data = {
			"scan_drive": DEFAULT_SCAN_DRIVE,
			"rescan_interval_seconds": DEFAULT_RESCAN_INTERVAL_SECONDS,
		}
		if config_path.exists():
			try:
				with open(config_path, "r", encoding="utf-8") as f:
					loaded = json.load(f)
				data.update({k: v for k, v in loaded.items() if k in data})
			except (json.JSONDecodeError, OSError):
				pass  # fall back to defaults rather than crash on a bad file

		settings = cls(
			scan_drive=data["scan_drive"],
			rescan_interval_seconds=int(data["rescan_interval_seconds"]),
			config_path=config_path,
			db_path=db_path,
		)
		if not config_path.exists():
			settings.save()  # write out defaults so the file exists to edit
		return settings

	def save(self) -> None:
		payload = {
			"scan_drive": self.scan_drive,
			"rescan_interval_seconds": self.rescan_interval_seconds,
		}
		with open(self.config_path, "w", encoding="utf-8") as f:
			json.dump(payload, f, indent="\t")

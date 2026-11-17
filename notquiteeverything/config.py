"""Configuration for the standard-library-only Windows file indexer."""

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "NotQuiteEverything"
DEFAULT_RESCAN_INTERVAL_SECONDS = 1800


def get_app_data_dir() -> Path:
    """Use non-roaming app data; migrate files from older roaming installs."""
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
    app_dir = base / APP_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    old_base = os.environ.get("APPDATA")
    if old_base and Path(old_base).resolve() != base.resolve():
        legacy = Path(old_base) / APP_NAME
        for filename in ("settings.json", "index.db"):
            source, target = legacy / filename, app_dir / filename
            if source.is_file() and not target.exists():
                try:
                    shutil.copy2(source, target)
                except OSError:
                    pass
    return app_dir


@dataclass
class Settings:
    scan_roots: list[str] = field(default_factory=lambda: ["C:\\"])
    exclude_patterns: list[str] = field(default_factory=list)
    rescan_interval_seconds: int = DEFAULT_RESCAN_INTERVAL_SECONDS
    watch_changes: bool = True
    config_path: Path | None = None
    db_path: Path | None = None

    @classmethod
    def load(cls, app_dir: Path | None = None) -> "Settings":
        app_dir = app_dir if app_dir is not None else get_app_data_dir()
        app_dir.mkdir(parents=True, exist_ok=True)
        config_path = app_dir / "settings.json"
        db_path = app_dir / "index.db"
        data = {}
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                pass
        roots = data.get("scan_roots", [data.get("scan_drive", "C:\\")])
        if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r.strip() for r in roots):
            roots = ["C:\\"]
        excludes = data.get("exclude_patterns", [])
        if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
            excludes = []
        try:
            interval = max(30, int(data.get("rescan_interval_seconds", DEFAULT_RESCAN_INTERVAL_SECONDS)))
        except (ValueError, TypeError, OverflowError):
            interval = DEFAULT_RESCAN_INTERVAL_SECONDS
        watch_changes = data.get("watch_changes", True)
        settings = cls(
            scan_roots=list(dict.fromkeys(r.strip() for r in roots)),
            exclude_patterns=[x.strip() for x in excludes if x.strip()],
            rescan_interval_seconds=interval,
            watch_changes=watch_changes if isinstance(watch_changes, bool) else True,
            config_path=config_path,
            db_path=db_path,
        )
        if not config_path.exists():
            settings.save()
        return settings

    def save(self) -> None:
        if not self.config_path:
            raise ValueError("config_path is not set")
        if not self.scan_roots or self.rescan_interval_seconds < 30:
            raise ValueError("Specify scan roots and an interval of at least 30 seconds")
        payload = {
            "scan_roots": self.scan_roots,
            "exclude_patterns": self.exclude_patterns,
            "rescan_interval_seconds": self.rescan_interval_seconds,
            "watch_changes": self.watch_changes,
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.config_path)

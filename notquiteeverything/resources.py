"""Shared application icon path for Tkinter and the native Windows tray."""

from pathlib import Path

APP_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "notquiteeverything.ico"

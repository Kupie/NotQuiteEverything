"""Entry point: wires up config, indexer, search engine and GUI."""

import logging
import tkinter as tk

from .config import Settings
from .gui import App
from .indexer import Indexer
from .search_engine import SearchEngine


def main() -> None:
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

	settings = Settings.load()
	indexer = Indexer(scan_drive=settings.scan_drive, db_path=settings.db_path)
	search_engine = SearchEngine(indexer)
	search_engine.start()

	root = tk.Tk()
	app = App(root, settings, indexer, search_engine)
	root.protocol("WM_DELETE_WINDOW", app.on_close)

	# Load the persisted index instantly if we have one, otherwise kick off
	# the first full scan in the background so the GUI is usable right away.
	app.start_initial_index()

	root.mainloop()


if __name__ == "__main__":
	main()

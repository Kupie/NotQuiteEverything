"""Application entry point."""

import logging
import tkinter as tk

from .config import Settings
from .gui import App
from .indexer import Indexer
from .search_engine import SearchEngine


def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    indexer = Indexer(settings)
    engine = SearchEngine(indexer)
    engine.start()
    root = tk.Tk()
    app = App(root, settings, indexer, engine)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    app.start_initial_index()
    root.mainloop()


if __name__ == "__main__":
    main()

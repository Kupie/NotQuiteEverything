"""Application entry point."""

import logging
import tkinter as tk

from .config import Settings
from .gui import App
from .indexer import Indexer
from .search_engine import SearchEngine
from .single_instance import SingleInstance


def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    instance = SingleInstance()
    if not instance.claim_or_activate():
        # An existing window has been signaled, so do not start another GUI
        # or open a second connection to the index database.
        return

    try:
        settings = Settings.load()
        indexer = Indexer(settings)
        engine = SearchEngine(indexer)
        engine.start()
        root = tk.Tk()
        app = App(root, settings, indexer, engine)
        root.protocol("WM_DELETE_WINDOW", app.on_close_request)

        def poll_activation() -> None:
            if instance.activation_requested():
                app.restore_window()
            root.after(100, poll_activation)

        root.after(100, poll_activation)
        app.start_initial_index()
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()

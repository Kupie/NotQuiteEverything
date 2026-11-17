"""Windows console-free launcher. Double-click or run with pythonw run.pyw."""

if __name__ == "__main__":
    try:
        from notquiteeverything.main import main
        main()
    except Exception:
        # pythonw.exe has no visible stderr, so show startup errors in a dialog.
        import sys
        import traceback

        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "NotQuiteEverything failed:\n\n" + traceback.format_exc()[-3500:],
                "NotQuiteEverything",
                0x10,
            )
        else:
            raise

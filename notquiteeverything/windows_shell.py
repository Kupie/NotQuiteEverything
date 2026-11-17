"""Windows Explorer integration without command-line quoting mistakes."""

import subprocess


def reveal_in_explorer(path: str) -> None:
    """Open the parent folder and select the requested file or directory.

    Explorer expects /select, and the target path as separate command-line
    arguments. Combining them causes Explorer to open its default directory
    when the target contains spaces.
    """
    subprocess.Popen(["explorer.exe", "/select,", path])

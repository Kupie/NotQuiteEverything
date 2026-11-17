"""Regression tests for Explorer's /select, command-line parsing."""

import subprocess
import unittest
from unittest import mock

from notquiteeverything.windows_shell import reveal_in_explorer


class ExplorerTests(unittest.TestCase):
    def test_reveal_file_with_spaces(self):
        path = r"C:\Users\Example User\Documents\Annual Report 2026.xlsx"
        with mock.patch("notquiteeverything.windows_shell.subprocess.Popen") as popen:
            reveal_in_explorer(path)
        popen.assert_called_once_with(["explorer.exe", "/select,", path])
        # subprocess.list2cmdline models CreateProcess quoting on Windows.
        self.assertEqual(
            subprocess.list2cmdline(popen.call_args.args[0]),
            'explorer.exe /select, "C:\\Users\\Example User\\Documents\\Annual Report 2026.xlsx"',
        )

    def test_reveal_unc_directory_with_spaces(self):
        path = r"\\server\shared data\Project Folder"
        with mock.patch("notquiteeverything.windows_shell.subprocess.Popen") as popen:
            reveal_in_explorer(path)
        popen.assert_called_once_with(["explorer.exe", "/select,", path])


if __name__ == "__main__":
    unittest.main()

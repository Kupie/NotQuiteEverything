"""Regression tests for Windows named mutex and activation event semantics."""

import sys
import unittest
import uuid

from notquiteeverything.single_instance import SingleInstance


class SingleInstanceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows kernel objects only")
    def test_second_process_signals_primary_instead_of_claiming(self):
        name = r"Local\NotQuiteEverythingTest-" + uuid.uuid4().hex
        first = SingleInstance(name)
        second = SingleInstance(name)
        third = SingleInstance(name)
        try:
            self.assertTrue(first.claim_or_activate())
            self.assertFalse(first.activation_requested())

            self.assertFalse(second.claim_or_activate())
            self.assertTrue(first.activation_requested())
            self.assertFalse(first.activation_requested())

            self.assertFalse(third.claim_or_activate())
            self.assertTrue(first.activation_requested())
        finally:
            first.close()
            second.close()
            third.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows kernel objects only")
    def test_relaunch_after_primary_exits(self):
        name = r"Local\NotQuiteEverythingTest-" + uuid.uuid4().hex
        first = SingleInstance(name)
        second = SingleInstance(name)
        try:
            self.assertTrue(first.claim_or_activate())
            first.close()
            self.assertTrue(second.claim_or_activate())
            self.assertFalse(second.activation_requested())
        finally:
            first.close()
            second.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows kernel objects only")
    def test_idempotent_acquisition_and_close(self):
        name = r"Local\NotQuiteEverythingTest-" + uuid.uuid4().hex
        primary = SingleInstance(name)
        try:
            self.assertTrue(primary.claim_or_activate())
            self.assertTrue(primary.claim_or_activate())
            primary.close()
            primary.close()
            self.assertTrue(primary.claim_or_activate())
        finally:
            primary.close()

    @unittest.skipIf(sys.platform == "win32", "Non-Windows behavior only")
    def test_non_windows_launch_without_mutex(self):
        primary = SingleInstance()
        try:
            self.assertTrue(primary.claim_or_activate())
            self.assertFalse(primary.activation_requested())
        finally:
            primary.close()


if __name__ == "__main__":
    unittest.main()

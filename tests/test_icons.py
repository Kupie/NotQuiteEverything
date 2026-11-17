"""Validate the bundled Windows icon without third-party dependencies."""

import struct
import unittest

from notquiteeverything.resources import APP_ICON_PATH


class IconTests(unittest.TestCase):
    def test_icon_is_bundled_and_contains_multiple_sizes(self):
        self.assertTrue(APP_ICON_PATH.is_file(), str(APP_ICON_PATH))
        data = APP_ICON_PATH.read_bytes()
        reserved, image_type, count = struct.unpack_from("<HHH", data, 0)
        self.assertEqual((reserved, image_type), (0, 1))
        self.assertGreaterEqual(count, 3)
        sizes = set()
        for i in range(count):
            width, height, _, _, _, _, length, offset = struct.unpack_from(
                "<BBBBHHII", data, 6 + 16 * i
            )
            self.assertLessEqual(offset + length, len(data))
            sizes.add((width or 256, height or 256))
        self.assertTrue({(16, 16), (32, 32), (256, 256)}.issubset(sizes))


if __name__ == "__main__":
    unittest.main()

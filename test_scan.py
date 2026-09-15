"""Regression checks for recursive folder scanning; no GUI is launched."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app import build_photo_groups, scan_photo_tree


class ScanTests(unittest.TestCase):
    def test_scan_finds_supported_images_at_every_depth(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "婚礼" / "晚宴"
            nested.mkdir(parents=True)
            (root / "root.JPG").touch()
            (root / "notes.txt").touch()
            (root / "婚礼" / "portrait.jpeg").touch()
            (nested / "scene.DNG").touch()
            (nested / "scene.TIFF").touch()
            (nested / "preview.PNG").touch()

            result = scan_photo_tree(root)

            self.assertFalse(result.cancelled)
            self.assertEqual(
                {path.relative_to(root).as_posix() for path in result.paths},
                {
                    "root.JPG",
                    "婚礼/portrait.jpeg",
                    "婚礼/晚宴/scene.DNG",
                    "婚礼/晚宴/scene.TIFF",
                    "婚礼/晚宴/preview.PNG",
                },
            )
            self.assertEqual(result.directories_scanned, 3)

    def test_scan_can_be_cancelled_without_returning_a_partial_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "photo.jpg").touch()
            result = scan_photo_tree(root, should_cancel=lambda: True)

            self.assertTrue(result.cancelled)
            self.assertEqual(result.paths, ())

    def test_symlinked_directory_is_not_followed(self) -> None:
        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside_temporary:
            root = Path(temporary)
            outside = Path(outside_temporary)
            (outside / "outside.jpg").touch()
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前 Windows 环境不允许创建目录符号链接")

            result = scan_photo_tree(root)

            self.assertEqual(result.paths, ())
            self.assertGreaterEqual(result.skipped_links, 1)

    def test_pairing_is_limited_to_the_same_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            paths = [
                first / "IMG_0001.DNG",
                first / "IMG_0001.JPG",
                second / "IMG_0001.DNG",
                second / "IMG_0001.JPG",
            ]

            groups = build_photo_groups(paths, root=root)

            self.assertEqual(len(groups), 2)
            self.assertTrue(all(group.paired_raw_jpeg for group in groups))
            self.assertNotEqual(groups[0].key, groups[1].key)
            self.assertEqual(
                {str(group.primary.parent.relative_to(root)) for group in groups},
                {"first", "second"},
            )


if __name__ == "__main__":
    unittest.main()

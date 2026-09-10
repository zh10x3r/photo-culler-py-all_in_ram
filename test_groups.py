"""Small regression checks for RAW+JPEG pairing; no GUI is launched."""

from pathlib import Path
from types import SimpleNamespace
import unittest

from app import PhotoCuller, build_photo_groups


class GroupingTests(unittest.TestCase):
    def test_dng_and_jpeg_are_one_culling_item(self) -> None:
        groups = build_photo_groups([
            Path(r"C:\shoot\DSC_0001.DNG"),
            Path(r"C:\shoot\DSC_0001.JPG"),
            Path(r"C:\shoot\DSC_0002.jpg"),
        ])
        self.assertEqual(len(groups), 2)
        paired = groups[0]
        self.assertTrue(paired.paired_raw_jpeg)
        self.assertEqual(paired.primary.suffix.lower(), ".jpg")
        self.assertEqual({path.suffix.lower() for path in paired.members}, {".dng", ".jpg"})

    def test_same_stem_png_is_not_accidentally_bound_to_raw_pair(self) -> None:
        groups = build_photo_groups([
            Path(r"C:\shoot\DSC_0001.DNG"),
            Path(r"C:\shoot\DSC_0001.JPEG"),
            Path(r"C:\shoot\DSC_0001.PNG"),
        ])
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0].members), 2)
        self.assertEqual(groups[1].primary.suffix.lower(), ".png")

    def test_pair_export_mode_selects_only_requested_original(self) -> None:
        pair = build_photo_groups([
            Path(r"C:\shoot\DSC_0001.DNG"),
            Path(r"C:\shoot\DSC_0001.JPG"),
        ])[0]
        app = object.__new__(PhotoCuller)
        app.pair_modes = {pair.key: "raw"}
        self.assertEqual([path.suffix.lower() for path in PhotoCuller._selected_members(app, pair)], [".dng"])
        app.pair_modes[pair.key] = "jpg"
        self.assertEqual([path.suffix.lower() for path in PhotoCuller._selected_members(app, pair)], [".jpg"])
        app.pair_modes[pair.key] = "both"
        self.assertEqual(len(PhotoCuller._selected_members(app, pair)), 2)

    def test_mode_and_keep_state_are_independent(self) -> None:
        pair = build_photo_groups([
            Path(r"C:\shoot\DSC_0001.DNG"),
            Path(r"C:\shoot\DSC_0001.JPG"),
        ])[0]
        class Harness:
            visible_items = PhotoCuller.visible_items
            current_item = PhotoCuller.current_item

            def _save_selection(self) -> None:
                pass

            def _show_current(self, **_kwargs) -> None:
                pass

        app = Harness()
        app.all_items = [pair]
        app.index = 0
        app.kept = set()
        app.pair_modes = {pair.key: "raw"}
        app.show_kept_only = SimpleNamespace(get=lambda: False)
        app._save_selection = lambda: None
        app._show_current = lambda **_kwargs: None
        PhotoCuller.toggle_keep(app)
        self.assertIn(pair.key, app.kept)
        self.assertEqual(app.pair_modes[pair.key], "raw")
        PhotoCuller.toggle_keep(app)
        self.assertNotIn(pair.key, app.kept)
        self.assertEqual(app.pair_modes[pair.key], "raw")

    def test_mode_cycle_is_both_then_jpg_then_raw(self) -> None:
        pair = build_photo_groups([
            Path(r"C:\shoot\DSC_0001.DNG"),
            Path(r"C:\shoot\DSC_0001.JPG"),
        ])[0]

        class Harness:
            visible_items = PhotoCuller.visible_items
            current_item = PhotoCuller.current_item
            _pair_mode = PhotoCuller._pair_mode

        app = Harness()
        app.all_items = [pair]
        app.index = 0
        app.kept = set()
        app.pair_modes = {}
        app.show_kept_only = SimpleNamespace(get=lambda: False)
        app._save_selection = lambda: None
        app._update_keep_mode_ui = lambda: None
        app._set_status = lambda _text: None
        app._status_text = lambda _item: ""
        app._render_thumbnails = lambda **_kwargs: None

        PhotoCuller.cycle_keep_mode(app)
        self.assertEqual(app.pair_modes[pair.key], "jpg")
        PhotoCuller.cycle_keep_mode(app)
        self.assertEqual(app.pair_modes[pair.key], "raw")
        PhotoCuller.cycle_keep_mode(app)
        self.assertEqual(app.pair_modes[pair.key], "both")


if __name__ == "__main__":
    unittest.main()

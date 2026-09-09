"""Testy vymazání zvolené oblasti ze všech stránek."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from helpers import make_text_page  # noqa: E402
from PIL import Image  # noqa: E402

import automation  # noqa: E402
import mask  # noqa: E402
from config import AppConfig  # noqa: E402


def color_at(path: str, x: int, y: int):
    with Image.open(path) as image:
        return image.convert("RGB").getpixel((x, y))


class TestMaskRect(unittest.TestCase):
    def test_box_and_list(self):
        rect = mask.MaskRect(10, 20, 30, 40)
        self.assertEqual(rect.box, (10, 20, 40, 60))
        self.assertEqual(rect.as_list(), [10, 20, 30, 40])

    def test_clipping_to_image(self):
        rect = mask.MaskRect(90, 90, 50, 50)
        clipped = rect.clipped(100, 100)
        self.assertEqual(clipped.box, (90, 90, 100, 100))

    def test_rect_outside_image_is_dropped(self):
        self.assertIsNone(mask.MaskRect(200, 200, 10, 10).clipped(100, 100))

    def test_negative_origin_is_clipped(self):
        clipped = mask.MaskRect(-20, -10, 50, 40).clipped(100, 100)
        self.assertEqual(clipped.box, (0, 0, 30, 30))


class TestNormalizeRects(unittest.TestCase):
    def test_accepts_lists_tuples_dicts_and_objects(self):
        rects = mask.normalize_rects([
            [1, 2, 3, 4],
            (5, 6, 7, 8),
            {"x": 9, "y": 10, "width": 11, "height": 12},
            mask.MaskRect(13, 14, 15, 16),
        ])
        self.assertEqual(
            [r.as_list() for r in rects],
            [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14, 15, 16]],
        )

    def test_rubbish_is_dropped(self):
        rects = mask.normalize_rects([
            [1, 2, 0, 5],        # nulová šířka
            [1, 2, 5, -3],       # záporná výška
            "nesmysl",
            None,
            [1, 2],              # chybí rozměry
            [10, 20, 30, 40],
        ])
        self.assertEqual([r.as_list() for r in rects], [[10, 20, 30, 40]])

    def test_none_is_empty(self):
        self.assertEqual(mask.normalize_rects(None), [])

    def test_round_trip_through_config(self):
        original = [mask.MaskRect(3, 4, 5, 6)]
        restored = mask.normalize_rects(mask.rects_to_config(original))
        self.assertEqual(restored, original)


class TestApplyMasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.out = os.path.join(self.dir, "masked")

    def tearDown(self):
        self.tmp.cleanup()

    def pages(self, count=3, size=(300, 200)) -> list[str]:
        paths = []
        for index in range(count):
            path = os.path.join(self.dir, f"page_{index + 1:04d}.png")
            page = make_text_page(index * 20 + 1, size)
            page.paste((0, 0, 0), (10, 10, 90, 50))  # černý blok, který budeme mazat
            page.save(path, "PNG")
            paths.append(path)
        return paths

    def test_area_becomes_white_on_every_page(self):
        paths = self.pages()
        rects = [mask.MaskRect(10, 10, 80, 40)]
        result = mask.apply_masks(paths, rects, self.out)
        self.assertEqual(len(result), len(paths))
        for path in result:
            self.assertEqual(color_at(path, 15, 15), (255, 255, 255))
            self.assertEqual(color_at(path, 85, 45), (255, 255, 255))

    def test_originals_are_untouched(self):
        paths = self.pages()
        mask.apply_masks(paths, [mask.MaskRect(10, 10, 80, 40)], self.out)
        for path in paths:
            self.assertEqual(color_at(path, 15, 15), (0, 0, 0), "originál se nesmí měnit")

    def test_pixels_outside_the_mask_survive(self):
        paths = self.pages()
        with Image.open(paths[0]) as before:
            reference = before.convert("RGB").getpixel((200, 150))
        result = mask.apply_masks(paths, [mask.MaskRect(10, 10, 80, 40)], self.out)
        self.assertEqual(color_at(result[0], 200, 150), reference)

    def test_masked_copies_keep_their_names(self):
        paths = self.pages()
        result = mask.apply_masks(paths, [mask.MaskRect(0, 0, 10, 10)], self.out)
        self.assertEqual(
            [os.path.basename(p) for p in result],
            [os.path.basename(p) for p in paths],
        )
        for path in result:
            self.assertTrue(path.startswith(self.out))

    def test_no_rects_returns_input_without_copying(self):
        paths = self.pages()
        self.assertEqual(mask.apply_masks(paths, [], self.out), paths)
        self.assertFalse(os.path.isdir(self.out))

    def test_several_rects(self):
        paths = self.pages(count=1)
        rects = [mask.MaskRect(10, 10, 40, 20), mask.MaskRect(150, 100, 60, 30)]
        result = mask.apply_masks(paths, rects, self.out)
        self.assertEqual(color_at(result[0], 20, 15), (255, 255, 255))
        self.assertEqual(color_at(result[0], 160, 110), (255, 255, 255))

    def test_rect_larger_than_image_is_clipped(self):
        paths = self.pages(count=1, size=(120, 90))
        result = mask.apply_masks(paths, [mask.MaskRect(0, 0, 9999, 9999)], self.out)
        with Image.open(result[0]) as image:
            self.assertEqual(image.size, (120, 90))
            self.assertEqual(image.convert("RGB").getpixel((119, 89)), (255, 255, 255))

    def test_empty_input_raises(self):
        with self.assertRaises(mask.MaskError):
            mask.apply_masks([], [mask.MaskRect(0, 0, 5, 5)], self.out)

    def test_draw_masks_does_not_modify_source(self):
        image = make_text_page(1, size=(120, 90))
        image.paste((0, 0, 0), (5, 5, 40, 30))
        masked = mask.draw_masks(image, [mask.MaskRect(5, 5, 35, 25)])
        self.assertEqual(masked.getpixel((10, 10)), (255, 255, 255))
        self.assertEqual(image.convert("RGB").getpixel((10, 10)), (0, 0, 0))


class TestMaskCaptures(unittest.TestCase):
    """Obal v automation.py – nikdy nesmí zabránit vzniku PDF."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.config = AppConfig()
        self.logs: list[str] = []
        self.paths = []
        for index in range(3):
            path = os.path.join(self.dir, f"page_{index + 1:04d}.png")
            page = make_text_page(index + 1, (200, 150))
            page.paste((0, 0, 0), (5, 5, 60, 40))
            page.save(path, "PNG")
            self.paths.append(path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_mask_returns_input(self):
        self.assertEqual(
            automation.mask_captures(self.config, self.paths, self.dir), self.paths
        )

    def test_mask_is_applied_into_subdirectory(self):
        self.config.mask_rects = [[5, 5, 55, 35]]
        result = automation.mask_captures(
            self.config, self.paths, self.dir, log=self.logs.append
        )
        self.assertNotEqual(result, self.paths)
        for path in result:
            self.assertIn(os.sep + "masked" + os.sep, path)
            self.assertEqual(color_at(path, 10, 10), (255, 255, 255))
        for path in self.paths:
            self.assertEqual(color_at(path, 10, 10), (0, 0, 0))
        self.assertTrue(any("Vymazáno" in m for m in self.logs))

    def test_failure_falls_back_to_originals(self):
        self.config.mask_rects = [[5, 5, 55, 35]]
        with mock.patch.object(
            automation.mask_mod, "apply_masks", side_effect=mask.MaskError("bum")
        ):
            result = automation.mask_captures(
                self.config, self.paths, self.dir, log=self.logs.append
            )
        self.assertEqual(result, self.paths)
        self.assertTrue(any("bum" in m for m in self.logs))

    def test_status_reports_masking(self):
        self.config.mask_rects = [[1, 1, 10, 10]]
        statuses: list[str] = []
        automation.mask_captures(
            self.config, self.paths, self.dir, status=statuses.append
        )
        self.assertIn(automation.Status.MASKING.value, statuses)

    def test_invalid_rects_are_ignored(self):
        self.config.mask_rects = [[0, 0, 0, 0], "nesmysl"]
        self.assertEqual(
            automation.mask_captures(self.config, self.paths, self.dir), self.paths
        )


def _tk_available() -> bool:
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


TK_AVAILABLE = _tk_available()


class FakeEvent:
    """Náhrada události Tk – obsluha tažení čte jen souřadnice."""

    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


@unittest.skipUnless(TK_AVAILABLE, "není k dispozici displej")
class TestMaskDialog(unittest.TestCase):
    """Přepočet mezi souřadnicemi náhledu a snímku."""

    def setUp(self):
        import tkinter as tk

        import gui

        self.tk = tk
        self.gui = gui
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def dialog(self, size=(1291, 1747), rects=()):
        from PIL import Image

        image = Image.new("RGB", size, "white")
        dlg = self.gui.MaskDialog(self.root, image, list(rects))
        self.root.update()
        return dlg

    def drag(self, dlg, x0, y0, x1, y1):
        dlg._on_press(FakeEvent(x0, y0))
        dlg._on_drag(FakeEvent(x1, y1))
        dlg._on_release(FakeEvent(x1, y1))

    def test_drag_maps_to_image_coordinates(self):
        dlg = self.dialog()
        self.drag(dlg, 40, 20, 380, 60)
        self.assertEqual(len(dlg._rects), 1)
        rect = dlg._rects[0]
        scale = dlg._scale
        self.assertAlmostEqual(rect.x, round(40 / scale), delta=2)
        self.assertAlmostEqual(rect.y, round(20 / scale), delta=2)
        self.assertAlmostEqual(rect.width, round(340 / scale), delta=2)
        self.assertAlmostEqual(rect.height, round(40 / scale), delta=2)
        dlg._cancel()

    def test_drag_in_any_direction_is_the_same(self):
        dlg = self.dialog()
        self.drag(dlg, 40, 20, 380, 60)
        first = dlg._rects[0]
        dlg._clear()
        self.drag(dlg, 380, 60, 40, 20)
        self.assertEqual(dlg._rects[0], first)
        dlg._cancel()

    def test_tiny_drag_is_ignored(self):
        dlg = self.dialog()
        self.drag(dlg, 10, 10, 11, 11)
        self.assertEqual(dlg._rects, [])
        dlg._cancel()

    def test_small_image_is_not_upscaled(self):
        dlg = self.dialog(size=(300, 200))
        self.assertEqual(dlg._scale, 1.0)
        self.drag(dlg, 10, 20, 60, 70)
        self.assertEqual(dlg._rects[0].as_list(), [10, 20, 50, 50])
        dlg._cancel()

    def test_large_image_is_scaled_down_to_fit(self):
        dlg = self.dialog(size=(4000, 3000))
        self.assertLess(dlg._scale, 1.0)
        self.assertLessEqual(dlg._photo.width(), dlg.MAX_WIDTH)
        self.assertLessEqual(dlg._photo.height(), dlg.MAX_HEIGHT)
        dlg._cancel()

    def test_undo_and_clear(self):
        dlg = self.dialog()
        self.drag(dlg, 10, 10, 100, 60)
        self.drag(dlg, 10, 200, 100, 250)
        self.assertEqual(len(dlg._rects), 2)
        dlg._undo()
        self.assertEqual(len(dlg._rects), 1)
        dlg._clear()
        self.assertEqual(dlg._rects, [])
        dlg._cancel()

    def test_existing_rects_are_offered_for_editing(self):
        existing = [mask.MaskRect(10, 20, 30, 40)]
        dlg = self.dialog(rects=existing)
        self.assertEqual(dlg._rects, existing)
        dlg._ok()
        self.assertEqual(dlg.result, existing)

    def test_cancel_returns_none(self):
        dlg = self.dialog()
        self.drag(dlg, 10, 10, 100, 60)
        dlg._cancel()
        self.assertIsNone(dlg.result)

    def test_rect_is_clipped_to_the_image(self):
        dlg = self.dialog(size=(300, 200))
        self.drag(dlg, 250, 150, 900, 900)
        rect = dlg._rects[0]
        self.assertLessEqual(rect.x + rect.width, 300)
        self.assertLessEqual(rect.y + rect.height, 200)
        dlg._cancel()


class TestConfigPersistence(unittest.TestCase):
    def test_mask_rects_survive_save_and_load(self):
        import config as cfg_mod

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cfg_mod, "get_working_dir", lambda: tmp):
                cfg = AppConfig(mask_rects=[[7, 8, 9, 10]])
                cfg.save()
                self.assertEqual(AppConfig.load().mask_rects, [[7, 8, 9, 10]])

    def test_clamp_drops_invalid_rects(self):
        cfg = AppConfig(mask_rects=[[1, 2, 3, 4], [0, 0, -1, 5], "x"])
        cfg.clamp()
        self.assertEqual(cfg.mask_rects, [[1, 2, 3, 4]])

    def test_default_is_empty(self):
        self.assertEqual(AppConfig().mask_rects, [])


if __name__ == "__main__":
    unittest.main()

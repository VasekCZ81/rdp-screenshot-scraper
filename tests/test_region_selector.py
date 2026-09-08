"""Testy překryvu pro výběr oblasti.

Simulují tah myší přes `event_generate`, takže ověří i přepočet na souřadnice
obrazovky včetně záporného počátku virtuální plochy (monitor vlevo od primárního).
Bez dostupného displeje se testy přeskočí.
"""

from __future__ import annotations

import tkinter as tk
import unittest
from unittest import mock

import helpers  # noqa: F401  (nastaví sys.path na ./src)

import region_selector  # noqa: E402
from window_manager import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()


def _tk_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_tk_available(), "není k dispozici displej")
class TestRegionSelector(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.result: list = []

    def tearDown(self):
        self.root.destroy()

    def _drag(self, virtual, press, release, motions=()):
        with mock.patch.object(region_selector, "virtual_screen_rect", lambda: virtual):
            selector = region_selector.RegionSelector(self.root, self.result.append)
            self.root.update()
            canvas = selector.canvas
            canvas.event_generate("<ButtonPress-1>", x=press[0], y=press[1])
            self.root.update()
            for point in motions:
                canvas.event_generate("<B1-Motion>", x=point[0], y=point[1])
                self.root.update()
            canvas.event_generate("<ButtonRelease-1>", x=release[0], y=release[1])
            self.root.update()
            if not self.result:
                selector._cancel()
                self.root.update()
        return self.result[-1] if self.result else None

    # ------------------------------------------------------------------
    def test_drag_maps_to_screen_coordinates(self):
        region = self._drag((0, 0, 1920, 1080), (100, 80), (700, 560), [(400, 300)])
        self.assertEqual((region.x, region.y, region.width, region.height), (100, 80, 600, 480))

    def test_negative_virtual_origin(self):
        """Monitor vlevo/nad primárním – overlay začíná na záporných souřadnicích."""
        region = self._drag((-1920, -200, 3840, 1280), (50, 40), (450, 340))
        self.assertEqual(
            (region.x, region.y, region.width, region.height), (-1870, -160, 400, 300)
        )

    def test_drag_in_any_direction_gives_same_rectangle(self):
        forward = self._drag((0, 0, 1920, 1080), (200, 150), (800, 650))
        self.result.clear()
        backward = self._drag((0, 0, 1920, 1080), (800, 650), (200, 150))
        self.assertEqual(
            (forward.x, forward.y, forward.width, forward.height),
            (backward.x, backward.y, backward.width, backward.height),
        )

    def test_tiny_drag_is_ignored(self):
        region = self._drag((0, 0, 1920, 1080), (100, 100), (101, 101))
        self.assertIsNone(region, "omylem kliknutí nesmí vytvořit oblast")

    def test_escape_cancels_selection(self):
        with mock.patch.object(
            region_selector, "virtual_screen_rect", lambda: (0, 0, 1920, 1080)
        ):
            selector = region_selector.RegionSelector(self.root, self.result.append)
            self.root.update()
            # Klávesový fokus drží plátno – tam ESC ve skutečnosti dorazí.
            # when="now" doručí událost okamžitě, nezávisle na frontě Tk.
            selector.canvas.focus_set()
            self.root.update()
            selector.canvas.event_generate("<Escape>", when="now")
            self.root.update()
        self.assertEqual(self.result, [None])

    def test_overlay_covers_whole_virtual_screen(self):
        with mock.patch.object(
            region_selector, "virtual_screen_rect", lambda: (-1920, 0, 3840, 1080)
        ):
            selector = region_selector.RegionSelector(self.root, self.result.append)
            self.root.update()
            self.assertEqual(selector.top.geometry().split("+")[0], "3840x1080")
            self.assertEqual(selector._origin, (-1920, 0))
            selector._cancel()
            self.root.update()


if __name__ == "__main__":
    unittest.main()

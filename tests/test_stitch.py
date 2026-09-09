"""Testy skládání překrývajících se snímků.

Dlouhý dokument se nakrájí na snímky se známými posuny a ty se skládají zpět.
Posuny musí sedět na pixel a rekonstrukce musí být bitově shodná s předlohou.
"""

from __future__ import annotations

import os
import random
import tempfile
import unittest
from unittest import mock

from helpers import make_text_page  # noqa: E402
from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

import automation  # noqa: E402
import stitch  # noqa: E402
from config import AppConfig  # noqa: E402

FONT_PATH = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")


def tall_document(
    width: int = 700, lines: int = 150, line_height: int = 24, gap_every: int = 0
) -> Image.Image:
    """Dlouhý dokument s nepravidelným textem, volitelně s mezerami mezi stránkami."""
    font = ImageFont.truetype(FONT_PATH, 15) if os.path.isfile(FONT_PATH) else None
    image = Image.new("RGB", (width, lines * line_height + 40), "white")
    draw = ImageDraw.Draw(image)
    rnd = random.Random(11)
    words = ["norma", "ustanoveni", "zarizeni", "bezpecnost", "obsluha",
             "napeti", "ochrana", "kontrola", "pracoviste", "revize"]
    y = 20
    for index in range(lines):
        if gap_every and index and index % gap_every == 0:
            y += 3 * line_height
        text = f"{index:04d} " + " ".join(
            rnd.choice(words) for _ in range(rnd.randint(5, 10))
        )
        draw.text((25, y), text, fill="black", font=font)
        y += line_height
    return image


def slice_document(document: Image.Image, view_height: int, shifts: list[int]):
    """Nakrájí dokument na překrývající se „obrazovky“."""
    shots, tops, top = [], [], 0
    for shift in [0] + shifts:
        top += shift
        if top + view_height > document.size[1]:
            break
        shots.append(document.crop((0, top, document.size[0], top + view_height)))
        tops.append(top)
    return shots, tops


class StitchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, images) -> list[str]:
        paths = []
        for index, image in enumerate(images, start=1):
            path = os.path.join(self.dir, f"page_{index:04d}.png")
            image.save(path, "PNG")
            paths.append(path)
        return paths

    def shifts_of(self, view_height, shifts, gap_every=0):
        document = tall_document(gap_every=gap_every)
        shots, tops = slice_document(document, view_height, shifts)
        plan = stitch.plan_ribbon(self.save(shots))
        expected = [top - tops[i - 1] for i, top in enumerate(tops) if i]
        return expected, [p.shift for p in plan.placements[1:]], plan


class TestShiftDetection(StitchTestCase):
    def test_small_overlap_is_recovered_exactly(self):
        """Page Down obvykle nechá jen pár řádků překryvu."""
        expected, found, plan = self.shifts_of(800, [770, 765, 772, 768])
        self.assertEqual(expected, found)
        self.assertTrue(all(p.verified for p in plan.placements[1:]))

    def test_large_overlap_is_recovered_exactly(self):
        expected, found, _plan = self.shifts_of(800, [500, 520, 480, 510])
        self.assertEqual(expected, found)

    def test_irregular_shifts(self):
        expected, found, _plan = self.shifts_of(700, [640, 410, 660, 555])
        self.assertEqual(expected, found)

    def test_overlap_below_minimum_is_estimated(self):
        """Překryv 10 px je pod hranicí použitelnosti – nastoupí obvyklý posun."""
        expected, found, plan = self.shifts_of(700, [660, 690, 660, 660])
        self.assertNotEqual(expected, found)
        for want, got in zip(expected, found):
            self.assertLessEqual(abs(want - got), 40, "odhad se má trefit blízko")
        self.assertTrue(any(not p.verified for p in plan.placements[1:]))

    def test_shift_not_multiple_of_stride(self):
        """Regrese: hrubá mřížka rozfázovala posuny, které nejsou násobkem kroku."""
        expected, found, _plan = self.shifts_of(760, [701, 702, 703, 705])
        self.assertEqual(expected, found)
        self.assertTrue(any(s % stitch.SCORE_STRIDE for s in found))

    def test_document_with_page_gaps(self):
        """Padne-li překryv do mezery mezi stránkami, použije se obvyklý posun."""
        expected, found, plan = self.shifts_of(700, [660, 655, 662], gap_every=12)
        for want, got in zip(expected, found):
            self.assertLessEqual(abs(want - got), 10, f"{want} vs {got}")
        for placement, want in zip(plan.placements[1:], expected):
            if placement.verified:
                self.assertEqual(placement.shift, want, "ověřený spoj musí sedět přesně")

    def test_reconstruction_is_bit_exact(self):
        document = tall_document()
        shots, tops = slice_document(document, 800, [700, 720, 690, 710])
        plan = stitch.plan_ribbon(self.save(shots))
        out = stitch.render_pages(plan, [(0, plan.height)], os.path.join(self.dir, "out"))
        with Image.open(out[0]) as ribbon:
            original = document.crop(
                (0, tops[0], document.size[0], tops[0] + plan.height)
            )
            self.assertEqual(ribbon.size, original.size)
            diff = ImageChops.difference(ribbon.convert("RGB"), original)
            self.assertEqual(max(diff.getextrema(), key=lambda t: t[1])[1], 0)

    def test_blinking_cursor_does_not_break_alignment(self):
        document = tall_document()
        shots, tops = slice_document(document, 800, [720, 725, 718])
        for index, shot in enumerate(shots):
            ImageDraw.Draw(shot).rectangle([3, 3 + (index % 2) * 5, 7, 18], fill="black")
        plan = stitch.plan_ribbon(self.save(shots))
        expected = [top - tops[i - 1] for i, top in enumerate(tops) if i]
        self.assertEqual(expected, [p.shift for p in plan.placements[1:]])

    def test_blank_overlap_uses_median_shift(self):
        """V prázdném místě posun určit nelze – vezme se obvyklý posun."""
        document = tall_document()
        shots, tops = slice_document(document, 800, [740, 740, 740])
        blank = Image.new("RGB", (document.size[0], 800), "white")
        shots.insert(2, blank)  # snímek bez jediného pixelu textu
        logged: list[str] = []
        plan = stitch.plan_ribbon(self.save(shots), log=logged.append)
        self.assertEqual(len(plan.placements), len(shots))
        self.assertTrue(any("nelze překryv ověřit" in m for m in logged))
        estimated = [p for p in plan.placements[1:] if not p.verified]
        self.assertTrue(estimated)
        for placement in estimated:
            self.assertEqual(placement.shift, 740, "má se použít medián posunů")

    def test_different_sizes_are_rejected(self):
        a = Image.new("RGB", (400, 300), "white")
        b = Image.new("RGB", (400, 250), "white")
        with self.assertRaises(stitch.StitchError):
            stitch.plan_ribbon(self.save([a, b]))

    def test_empty_input_raises(self):
        with self.assertRaises(stitch.StitchError):
            stitch.plan_ribbon([])


class TestRowProfile(StitchTestCase):
    def test_profile_matches_manual_row_means(self):
        image = make_text_page(3, size=(300, 120))
        profile = stitch.row_profile(image, margin_ratio=0.0)
        self.assertEqual(len(profile), 120)
        pixels = image.convert("L").load()
        for y in (0, 60, 119):
            manual = sum(pixels[x, y] for x in range(300)) / 300
            self.assertAlmostEqual(manual, profile[y], delta=0.6)

    def test_margin_is_ignored(self):
        image = Image.new("RGB", (400, 50), "white")
        ImageDraw.Draw(image).rectangle([392, 0, 399, 49], fill="black")
        self.assertAlmostEqual(max(stitch.row_profile(image)), 255.0, places=3)


class TestCutting(StitchTestCase):
    def test_cuts_land_in_whitespace(self):
        document = tall_document(lines=120, gap_every=20)
        shots, _tops = slice_document(document, 700, [640] * 6)
        plan = stitch.plan_ribbon(self.save(shots))
        cuts = stitch.find_cuts(plan, 700)
        self.assertGreater(len(cuts), 1)
        for _top, bottom in cuts[:-1]:
            band = plan.profile[max(0, bottom - 3): bottom + 3]
            ink = sum(255.0 - value for value in band) / max(1, len(band))
            self.assertLess(ink, 15.0, f"řez na řádku {bottom} vede textem")

    def test_cuts_cover_the_whole_ribbon(self):
        document = tall_document(lines=100)
        shots, _tops = slice_document(document, 600, [560] * 5)
        plan = stitch.plan_ribbon(self.save(shots))
        cuts = stitch.find_cuts(plan, 800)
        self.assertEqual(cuts[0][0], 0)
        self.assertEqual(cuts[-1][1], plan.height)
        for first, second in zip(cuts, cuts[1:]):
            self.assertEqual(first[1], second[0], "stránky na sebe musí navazovat")

    def test_short_ribbon_is_one_page(self):
        image = make_text_page(1, size=(400, 300))
        plan = stitch.plan_ribbon(self.save([image]))
        self.assertEqual(stitch.find_cuts(plan, 5000), [(0, 300)])

    def test_default_page_height_uses_a4_ratio(self):
        document = tall_document(width=600, lines=90)
        shots, _tops = slice_document(document, 500, [460] * 5)
        pages = stitch.stitch_pages(self.save(shots), os.path.join(self.dir, "out"))
        self.assertTrue(pages)
        with Image.open(pages[0]) as page:
            self.assertEqual(page.size[0], 600)
            self.assertAlmostEqual(page.size[1] / 600, stitch.A4_RATIO, delta=0.25)


class TestStitchCaptures(StitchTestCase):
    """Obal v automation.py – nikdy nesmí zabránit vzniku PDF."""

    def setUp(self):
        super().setUp()
        self.config = AppConfig()
        self.config.stitch_enabled = True
        self.logs: list[str] = []

    def _pages(self, count=4):
        document = tall_document(lines=90)
        shots, _tops = slice_document(document, 600, [560] * (count - 1))
        return self.save(shots)

    def test_disabled_returns_input(self):
        self.config.stitch_enabled = False
        pages = self._pages()
        self.assertEqual(automation.stitch_captures(self.config, pages, self.dir), pages)

    def test_single_page_returns_input(self):
        pages = self._pages(count=1)
        self.assertEqual(automation.stitch_captures(self.config, pages, self.dir), pages)

    def test_stitched_pages_land_in_subdirectory(self):
        pages = self._pages()
        result = automation.stitch_captures(
            self.config, pages, self.dir, log=self.logs.append
        )
        self.assertNotEqual(result, pages)
        for path in result:
            self.assertIn("stitched", path)
            self.assertTrue(os.path.isfile(path))
        for path in pages:
            self.assertTrue(os.path.isfile(path), "původní snímky musí zůstat")

    def test_failure_falls_back_to_original_pages(self):
        pages = self._pages()
        with mock.patch.object(
            automation.stitch, "stitch_pages", side_effect=stitch.StitchError("bum")
        ):
            result = automation.stitch_captures(
                self.config, pages, self.dir, log=self.logs.append
            )
        self.assertEqual(result, pages)
        self.assertTrue(any("bum" in m for m in self.logs))

    def test_empty_result_falls_back(self):
        pages = self._pages()
        with mock.patch.object(automation.stitch, "stitch_pages", return_value=[]):
            result = automation.stitch_captures(
                self.config, pages, self.dir, log=self.logs.append
            )
        self.assertEqual(result, pages)

    def test_status_reports_stitching(self):
        statuses: list[str] = []
        automation.stitch_captures(
            self.config, self._pages(), self.dir, status=statuses.append
        )
        self.assertIn(automation.Status.STITCHING.value, statuses)


if __name__ == "__main__":
    unittest.main()

"""Testy detekce změny obrazu."""

from __future__ import annotations

import unittest

from helpers import add_clock, add_noise, make_page, make_text_page  # noqa: E402

from image_compare import (  # noqa: E402
    compare,
    dhash,
    difference_metrics,
    hamming_distance,
)


class TestImageCompare(unittest.TestCase):
    def test_identical_images(self):
        page = make_page(1)
        result = compare(page, page.copy())
        self.assertTrue(result.identical)
        self.assertEqual(result.hamming, 0)

    def test_different_pages_are_not_identical(self):
        for seed in range(1, 8):
            with self.subTest(seed=seed):
                result = compare(make_page(seed), make_page(seed + 1))
                self.assertFalse(
                    result.identical,
                    f"stránky {seed} a {seed + 1} vyhodnoceny jako shodné ({result.describe()})",
                )

    def test_blinking_cursor_is_tolerated(self):
        page = make_page(3)
        result = compare(page, add_noise(page))
        self.assertTrue(
            result.identical, f"drobná změna ukončila snímání ({result.describe()})"
        )

    def test_different_size_is_not_identical(self):
        result = compare(make_page(1), make_page(1, size=(100, 100)))
        self.assertFalse(result.identical)

    def test_scrolled_text_pages_are_recognised_as_different(self):
        """Regrese: dvě stránky hustého textu se po zmenšení jeví shodně.

        Právě tenhle případ aplikace snímá – dokument posunutý o stránku.
        """
        page_a = make_text_page(1)
        page_b = make_text_page(50)  # posun o stránku
        result = compare(page_a, page_b)
        self.assertFalse(
            result.identical,
            f"posun dokumentu o stránku nebyl rozpoznán ({result.describe()})",
        )
        self.assertGreater(result.changed_fraction, 0.01)

    def test_text_page_scrolled_by_single_line(self):
        """I posun o jediný řádek je skutečná změna pozice dokumentu."""
        result = compare(make_text_page(1), make_text_page(2))
        self.assertFalse(result.identical, result.describe())

    def test_same_text_page_is_identical(self):
        page = make_text_page(7)
        result = compare(page, page.copy())
        self.assertTrue(result.identical)
        self.assertEqual(result.changed_fraction, 0.0)

    def test_clock_does_not_end_the_document(self):
        page = make_text_page(3)
        result = compare(add_clock(page, "12:34:56"), add_clock(page, "12:34:57"))
        self.assertTrue(
            result.identical, f"změna hodin ukončila snímání ({result.describe()})"
        )

    def test_metrics_have_a_wide_margin(self):
        """Mezi 'drobná animace' a 'nová stránka' musí být řádový odstup."""
        page = make_text_page(1)
        _small_mean, small_changed = difference_metrics(page, add_clock(page))
        _page_mean, page_changed = difference_metrics(page, make_text_page(50))
        self.assertGreater(page_changed, small_changed * 10)

    def test_hamming(self):
        self.assertEqual(hamming_distance(0b1011, 0b1001), 1)
        self.assertEqual(hamming_distance(0, 0), 0)

    def test_dhash_is_stable(self):
        page = make_page(5)
        self.assertEqual(dhash(page), dhash(page.copy()))


if __name__ == "__main__":
    unittest.main()

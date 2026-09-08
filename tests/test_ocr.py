"""Testy OCR vrstvy.

Zpracování odpovědi se testuje bez volání PowerShellu. Navíc je tu jeden
živý test proti skutečnému enginu Windows – přeskočí se, pokud na počítači
není nainstalován žádný jazyk pro OCR.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (nastaví sys.path na ./src)

import automation  # noqa: E402
import ocr  # noqa: E402
from config import AppConfig  # noqa: E402


def _ocr_ready() -> bool:
    try:
        return bool(ocr.available_languages())
    except ocr.OcrError:
        return False


OCR_AVAILABLE = _ocr_ready()


class TestResultParsing(unittest.TestCase):
    """Zpracování odpovědi z PowerShellu."""

    def _recognize(self, response, paths=("a.png", "b.png")):
        with mock.patch.object(ocr, "_run", return_value=response):
            return ocr.recognize(list(paths), "cs")

    def test_words_are_parsed(self):
        response = {
            "pages": [
                {
                    "width": 100,
                    "height": 50,
                    "words": [
                        {"t": "Příliš", "x": 1.5, "y": 2, "w": 30, "h": 12},
                        {"t": "žluťoučký", "x": 35, "y": 2, "w": 50, "h": 12},
                    ],
                },
                {"width": 100, "height": 50, "words": []},
            ]
        }
        pages = self._recognize(response)
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].text, "Příliš žluťoučký")
        self.assertEqual(pages[0].words[0].x, 1.5)
        self.assertEqual(pages[0].words[0].height, 12.0)
        self.assertEqual(pages[1].words, ())

    def test_page_error_does_not_break_the_rest(self):
        response = {
            "pages": [
                {"error": "soubor se nepodařilo otevřít"},
                {"width": 10, "height": 10, "words": [{"t": "ok", "x": 0, "y": 0, "w": 5, "h": 5}]},
            ]
        }
        logged: list[str] = []
        with mock.patch.object(ocr, "_run", return_value=response):
            pages = ocr.recognize(["a.png", "b.png"], "cs", log=logged.append)
        self.assertIsNone(pages[0])
        self.assertEqual(pages[1].text, "ok")
        self.assertTrue(any("selhalo" in m for m in logged))

    def test_missing_pages_become_none(self):
        pages = self._recognize({"pages": []})
        self.assertEqual(pages, [None, None])

    def test_empty_words_are_dropped(self):
        response = {
            "pages": [
                {
                    "width": 10,
                    "height": 10,
                    "words": [
                        {"t": "", "x": 0, "y": 0, "w": 1, "h": 1},
                        {"t": "ano", "x": 0, "y": 0, "w": 5, "h": 5},
                    ],
                }
            ]
        }
        pages = self._recognize(response, paths=("a.png",))
        self.assertEqual([w.text for w in pages[0].words], ["ano"])

    def test_no_images_needs_no_subprocess(self):
        with mock.patch.object(ocr, "_run", side_effect=AssertionError("nemá se volat")):
            self.assertEqual(ocr.recognize([], "cs"), [])

    def test_top_level_error_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ocr, "_powershell_exe", return_value="powershell.exe"):
                with mock.patch("subprocess.Popen", side_effect=OSError("není")):
                    with self.assertRaises(ocr.OcrError):
                        ocr._run("languages", tmp, None, 5.0)


class TestAvailability(unittest.TestCase):
    def test_is_available_matches_language_tags(self):
        with mock.patch.object(ocr, "available_languages", return_value=["cs", "en-GB"]):
            self.assertTrue(ocr.is_available())
            self.assertTrue(ocr.is_available("cs"))
            self.assertTrue(ocr.is_available("CS"))
            self.assertTrue(ocr.is_available("en"))  # en-GB vyhoví prefixu
            self.assertFalse(ocr.is_available("de"))

    def test_no_languages_means_unavailable(self):
        with mock.patch.object(ocr, "available_languages", return_value=[]):
            self.assertFalse(ocr.is_available())
            self.assertFalse(ocr.is_available("cs"))

    def test_error_means_unavailable(self):
        with mock.patch.object(ocr, "available_languages", side_effect=ocr.OcrError("x")):
            self.assertFalse(ocr.is_available("cs"))


class TestOcrPagesHelper(unittest.TestCase):
    """`automation.ocr_pages` nesmí nikdy zabránit vytvoření PDF."""

    def setUp(self):
        self.config = AppConfig()
        self.logs: list[str] = []

    def _call(self, pages=("a.png",)):
        return automation.ocr_pages(self.config, list(pages), log=self.logs.append)

    def test_disabled_returns_none(self):
        self.config.ocr_enabled = False
        with mock.patch.object(automation.ocr, "is_available", side_effect=AssertionError):
            self.assertIsNone(self._call())

    def test_no_pages_returns_none(self):
        self.assertIsNone(automation.ocr_pages(self.config, []))

    def test_language_unavailable_returns_none_and_explains(self):
        with mock.patch.object(automation.ocr, "is_available", return_value=False), \
             mock.patch.object(automation.ocr, "available_languages", return_value=["en-GB"]):
            self.assertIsNone(self._call())
        self.assertTrue(any("není ve Windows k dispozici" in m for m in self.logs))
        self.assertTrue(any("en-GB" in m for m in self.logs))

    def test_ocr_failure_returns_none_and_logs(self):
        with mock.patch.object(automation.ocr, "is_available", return_value=True), \
             mock.patch.object(automation.ocr, "recognize", side_effect=ocr.OcrError("bum")):
            self.assertIsNone(self._call())
        self.assertTrue(any("bum" in m for m in self.logs))

    def test_success_reports_progress_and_counts(self):
        page = ocr.PageText(10, 10, (ocr.Word("ahoj", 0, 0, 5, 5),))
        statuses: list[str] = []
        with mock.patch.object(automation.ocr, "is_available", return_value=True), \
             mock.patch.object(automation.ocr, "recognize", return_value=[page, None]):
            layers = automation.ocr_pages(
                self.config, ["a.png", "b.png"], status=statuses.append, log=self.logs.append
            )
        self.assertEqual(layers, [page, None])
        self.assertTrue(any("Provádím OCR" in s for s in statuses))
        self.assertTrue(any("1 slov na 1 z 2" in m or "slov na 1 z 2" in m for m in self.logs))


@unittest.skipUnless(OCR_AVAILABLE, "ve Windows není nainstalován jazyk pro OCR")
class TestLiveEngine(unittest.TestCase):
    """Živý test proti enginu vestavěnému ve Windows."""

    def test_languages_are_reported(self):
        tags = ocr.available_languages()
        self.assertTrue(tags)
        self.assertTrue(all(isinstance(tag, str) and tag for tag in tags))

    def test_recognizes_rendered_text(self):
        from PIL import Image, ImageDraw, ImageFont

        font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")
        if not os.path.isfile(font_path):
            self.skipTest("není k dispozici písmo Arial")
        font = ImageFont.truetype(font_path, 34)

        expected = ["Testovaci", "stranka", "dokumentu"]
        image = Image.new("RGB", (700, 220), "white")
        draw = ImageDraw.Draw(image)
        draw.text((30, 40), " ".join(expected), fill="black", font=font)
        draw.text((30, 120), "druhy radek textu", fill="black", font=font)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page_0001.png")
            image.save(path, "PNG")
            language = ocr.available_languages()[0]
            pages = ocr.recognize([path], language)

        self.assertEqual(len(pages), 1)
        self.assertIsNotNone(pages[0], "OCR nevrátilo výsledek")
        text = pages[0].text
        for word in expected:
            self.assertIn(word, text, f"v rozpoznaném textu chybí {word!r}: {text!r}")
        for word in pages[0].words:
            self.assertGreater(word.width, 0)
            self.assertGreater(word.height, 0)
            self.assertLessEqual(word.x + word.width, pages[0].width + 5)


if __name__ == "__main__":
    unittest.main()

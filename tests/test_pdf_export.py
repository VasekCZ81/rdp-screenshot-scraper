"""Testy exportu do PDF."""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from helpers import make_page, read_bytes as _read  # noqa: E402

from pdf_export import PdfExportError, images_to_pdf  # noqa: E402


class TestPdfExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _pages(self, count: int, size=(200, 150)) -> list[str]:
        paths = []
        for index in range(count):
            path = os.path.join(self.dir, f"page_{index + 1:04d}.png")
            make_page(index, size).save(path, "PNG")
            paths.append(path)
        return paths

    def test_creates_pdf_with_one_page_per_image(self):
        paths = self._pages(3)
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, dpi=96)

        self.assertTrue(os.path.isfile(pdf))
        data = _read(pdf)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertEqual(data.count(b"/Type /Page\n") + data.count(b"/Type /Page "), 3)
        self.assertIn(b"/Count 3", data)

    def test_pages_keep_aspect_ratio(self):
        paths = self._pages(1, size=(820, 900))
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, dpi=96)
        data = _read(pdf)
        box = re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", data)
        self.assertIsNotNone(box)
        width, height = float(box.group(1)), float(box.group(2))
        self.assertAlmostEqual(width / height, 820 / 900, places=4)
        self.assertAlmostEqual(width, 820 * 72 / 96, places=3)

    def test_lossless_flate_encoding(self):
        paths = self._pages(1)
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, dpi=96)
        data = _read(pdf)
        self.assertIn(b"/Filter /FlateDecode", data)
        self.assertNotIn(b"DCTDecode", data)

    def test_xref_table_points_at_real_objects(self):
        """Ruční zápis PDF – ověříme, že tabulka xref sedí bajt po bajtu."""
        paths = self._pages(3)
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf)
        data = _read(pdf)

        start = int(re.search(rb"startxref\s+(\d+)", data).group(1))
        self.assertEqual(data[start : start + 4], b"xref")
        header = re.match(rb"xref\s+0 (\d+)\s+", data[start:])
        size = int(header.group(1))
        table = data[start + header.end() :]

        # 1 volná položka + objekty; každá položka má přesně 20 bajtů
        self.assertEqual(table[:20], b"0000000000 65535 f \n")
        for obj_id in range(1, size):
            entry = table[obj_id * 20 : obj_id * 20 + 20]
            self.assertRegex(entry, rb"^\d{10} 00000 n \n$")
            offset = int(entry[:10])
            self.assertTrue(
                data[offset:].startswith(f"{obj_id} 0 obj".encode()),
                f"xref položka {obj_id} ukazuje na {data[offset:offset + 20]!r}",
            )
        self.assertIn(f"/Size {size}".encode(), data)

    def test_image_stream_decompresses_to_original_pixels(self):
        """Bezeztrátovost: data v PDF se musí rovnat pixelům PNG."""
        import zlib

        from PIL import Image

        paths = self._pages(1, size=(64, 48))
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf)
        data = _read(pdf)

        match = re.search(rb"/Filter /FlateDecode /Length (\d+) >>\nstream\n", data)
        self.assertIsNotNone(match)
        length = int(match.group(1))
        stream = data[match.end() : match.end() + length]

        with Image.open(paths[0]) as source:
            self.assertEqual(zlib.decompress(stream), source.convert("RGB").tobytes())

    def test_empty_list_raises(self):
        with self.assertRaises(PdfExportError):
            images_to_pdf([], os.path.join(self.dir, "x.pdf"))

    def test_missing_file_raises_and_leaves_no_tmp(self):
        pdf = os.path.join(self.dir, "result.pdf")
        with self.assertRaises(PdfExportError):
            images_to_pdf([os.path.join(self.dir, "nope.png")], pdf)
        self.assertFalse(os.path.exists(pdf + ".tmp"))

    def test_existing_pdf_is_replaced(self):
        paths = self._pages(2)
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf)
        first = os.path.getsize(pdf)
        images_to_pdf(paths[:1], pdf)
        self.assertNotEqual(first, os.path.getsize(pdf))
        self.assertIn(b"/Count 1", _read(pdf))


if __name__ == "__main__":
    unittest.main()

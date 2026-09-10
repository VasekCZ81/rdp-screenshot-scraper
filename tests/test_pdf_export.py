"""Testy exportu do PDF."""

from __future__ import annotations

import os
import re
import tempfile
import unittest

import zlib  # noqa: E402

from PIL import Image  # noqa: E402

from helpers import make_page, read_bytes as _read  # noqa: E402

from pdf_export import (  # noqa: E402
    COMPRESSION_BILEVEL,
    COMPRESSION_LOSSLESS,
    COMPRESSION_NONE,
    BILEVEL_THRESHOLD,
    MAX_DPI,
    MAX_PALETTE,
    PdfExportError,
    dpi_for_width,
    encode_page_image,
    images_to_pdf,
    prepare_image,
)


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
        images_to_pdf(paths, pdf, compression=COMPRESSION_NONE)
        data = _read(pdf)

        match = re.search(rb"/Filter /FlateDecode /Length (\d+) >>\nstream\n", data)
        self.assertIsNotNone(match)
        length = int(match.group(1))
        stream = data[match.end() : match.end() + length]

        with Image.open(paths[0]) as source:
            self.assertEqual(zlib.decompress(stream), source.convert("RGB").tobytes())

    # --- automatický odhad DPI ze šířky předlohy ---------------------------
    def test_dpi_for_width_matches_physical_size(self):
        # A4 (210 mm) nasnímaná v 1600 px => 1600 / 8.2677" ≈ 193.5 DPI
        self.assertAlmostEqual(dpi_for_width(1600, 210.0), 193.52, places=1)
        self.assertAlmostEqual(dpi_for_width(96, 25.4), 96.0, places=6)

    def test_dpi_for_width_is_clamped_and_validated(self):
        self.assertEqual(dpi_for_width(10000, 1.0), float(MAX_DPI))
        with self.assertRaises(ValueError):
            dpi_for_width(0, 210.0)
        with self.assertRaises(ValueError):
            dpi_for_width(800, 0.0)

    def test_page_width_mm_sets_real_page_size(self):
        paths = self._pages(1, size=(1600, 2263))
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, dpi=96, page_width_mm=210.0)
        data = _read(pdf)
        box = re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", data)
        width_pt = float(box.group(1))
        # 210 mm = 595.28 bodu – stránka má skutečnou šířku A4, ne 1600/96 palce.
        self.assertAlmostEqual(width_pt, 210.0 / 25.4 * 72.0, places=2)
        self.assertAlmostEqual(width_pt / float(box.group(2)), 1600 / 2263, places=4)

    def test_page_width_mm_overrides_dpi(self):
        paths = self._pages(1, size=(800, 600))
        fixed = os.path.join(self.dir, "fixed.pdf")
        auto = os.path.join(self.dir, "auto.pdf")
        images_to_pdf(paths, fixed, dpi=96)
        images_to_pdf(paths, auto, dpi=96, page_width_mm=210.0)
        self.assertNotEqual(
            re.search(rb"/MediaBox \[0 0 ([\d.]+) ", _read(fixed)).group(1),
            re.search(rb"/MediaBox \[0 0 ([\d.]+) ", _read(auto)).group(1),
        )

    def test_page_width_mm_zero_keeps_fixed_dpi(self):
        paths = self._pages(1, size=(820, 900))
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, dpi=96, page_width_mm=0)
        data = _read(pdf)
        width = float(re.search(rb"/MediaBox \[0 0 ([\d.]+) ", data).group(1))
        self.assertAlmostEqual(width, 820 * 72 / 96, places=3)

    def test_negative_page_width_raises(self):
        paths = self._pages(1)
        pdf = os.path.join(self.dir, "result.pdf")
        with self.assertRaises(PdfExportError):
            images_to_pdf(paths, pdf, page_width_mm=-5)
        self.assertFalse(os.path.exists(pdf + ".tmp"))

    # --- zvětšení snímku před vložením do PDF ------------------------------
    def test_upscale_adds_samples_but_keeps_page_size(self):
        paths = self._pages(1, size=(400, 300))
        plain = os.path.join(self.dir, "plain.pdf")
        big = os.path.join(self.dir, "big.pdf")
        images_to_pdf(paths, plain, dpi=96)
        images_to_pdf(paths, big, dpi=96, upscale=2.0)

        box = rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]"
        self.assertEqual(
            re.search(box, _read(plain)).groups(), re.search(box, _read(big)).groups()
        )
        self.assertIn(b"/Width 400 /Height 300", _read(plain))
        self.assertIn(b"/Width 800 /Height 600", _read(big))

    def test_upscale_combines_with_page_width_mm(self):
        paths = self._pages(1, size=(400, 300))
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, page_width_mm=210.0, upscale=2.0)
        data = _read(pdf)
        width_pt = float(re.search(rb"/MediaBox \[0 0 ([\d.]+) ", data).group(1))
        self.assertAlmostEqual(width_pt, 210.0 / 25.4 * 72.0, places=2)
        self.assertIn(b"/Width 800 /Height 600", data)

    def test_upscale_is_capped_and_never_shrinks(self):
        paths = self._pages(1, size=(100, 80))
        for factor, expected in ((99.0, b"/Width 400 /Height 320"),
                                 (0.5, b"/Width 100 /Height 80"),
                                 (0, b"/Width 100 /Height 80")):
            pdf = os.path.join(self.dir, f"u{factor}.pdf")
            images_to_pdf(paths, pdf, upscale=factor)
            self.assertIn(expected, _read(pdf))

    # --- doostření --------------------------------------------------------
    def _blurred_text_edge(self):
        """Tmavý pruh s rozmazanou hranou – náhrada za rozmazané písmo."""
        from PIL import Image, ImageFilter

        img = Image.new("RGB", (60, 40), "white")
        for x in range(20, 40):
            for y in range(40):
                img.putpixel((x, y), (20, 20, 20))
        return img.filter(ImageFilter.GaussianBlur(1.5))

    def test_sharpen_zero_is_a_no_op(self):
        source = self._blurred_text_edge()
        self.assertEqual(prepare_image(source, 1.0, 0).tobytes(), source.tobytes())

    def test_sharpen_raises_edge_contrast(self):
        source = self._blurred_text_edge()
        sharp = prepare_image(source, 1.0, 120)
        self.assertEqual(sharp.size, source.size)

        def edge_step(img):
            # rozdíl jasu přes hranu: čím strmější, tím ostřejší
            return abs(img.getpixel((22, 20))[0] - img.getpixel((17, 20))[0])

        self.assertGreater(edge_step(sharp), edge_step(source))

    def test_sharpen_leaves_flat_areas_alone(self):
        """Práh drží prázdný papír beze změny – nezvýrazní artefakty kodeku."""
        sharp = prepare_image(self._blurred_text_edge(), 1.0, 150)
        self.assertEqual(sharp.getpixel((2, 20)), (255, 255, 255))

    def test_sharpen_is_capped(self):
        source = self._blurred_text_edge()
        self.assertEqual(
            prepare_image(source, 1.0, 9999).tobytes(),
            prepare_image(source, 1.0, 300).tobytes(),
        )

    def test_sharpen_combines_with_upscale(self):
        source = self._blurred_text_edge()
        result = prepare_image(source, 2.0, 120)
        self.assertEqual(result.size, (120, 80))

    def test_sharpen_changes_pdf_pixels_but_not_geometry(self):
        import zlib

        # Ostré přechody 0/255 by se po doostření jen ořízly zpátky – proto
        # stránka s rozmazanou hranou, tedy stejná situace jako v RDP.
        path = os.path.join(self.dir, "page_0001.png")
        self._blurred_text_edge().save(path, "PNG")
        plain = os.path.join(self.dir, "plain.pdf")
        sharp = os.path.join(self.dir, "sharp.pdf")
        images_to_pdf([path], plain, dpi=96)
        images_to_pdf([path], sharp, dpi=96, sharpen=120)

        box = rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]"
        self.assertEqual(
            re.search(box, _read(plain)).groups(), re.search(box, _read(sharp)).groups()
        )
        self.assertIn(b"/Width 60 /Height 40", _read(sharp))

        stream = re.compile(rb"/Filter /FlateDecode /Length (\d+) >>\nstream\n")

        def pixels(pdf_path):
            data = _read(pdf_path)
            match = stream.search(data)
            return zlib.decompress(data[match.end() : match.end() + int(match.group(1))])

        self.assertNotEqual(pixels(plain), pixels(sharp))

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


class TestLosslessOptimisation(unittest.TestCase):
    """Indexovaná paleta smí soubor zmenšit, ale ne změnit jediný pixel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _rebuild(data: bytes, colorspace: str, size) -> Image.Image:
        """Složí obrázek zpět z PDF streamu a palety v colorspace."""
        table = bytes.fromhex(colorspace[colorspace.index("<") + 1 : colorspace.index(">")])
        indexed = Image.frombytes("P", size, zlib.decompress(data))
        indexed.putpalette(table)
        return indexed.convert("RGB")

    def test_palette_roundtrip_is_bit_exact(self):
        image = make_page(3, (200, 150))
        data, colorspace, how = encode_page_image(image, COMPRESSION_LOSSLESS)
        self.assertTrue(how.startswith("paleta"), how)
        self.assertIn("/Indexed /DeviceRGB", colorspace)
        rebuilt = self._rebuild(data, colorspace, image.size)
        self.assertEqual(rebuilt.tobytes(), image.tobytes())

    def test_palette_is_smaller_than_devicergb(self):
        image = make_page(5, (400, 300))
        small, _cs, _how = encode_page_image(image, COMPRESSION_LOSSLESS)
        plain, _cs2, _how2 = encode_page_image(image, COMPRESSION_NONE)
        self.assertLess(len(small), len(plain))

    def test_colourful_page_falls_back_to_devicergb(self):
        """Nad 256 barev paleta nestačí – stránka se uloží jako dosud."""
        image = Image.new("RGB", (64, 64))
        pixels = image.load()
        for y in range(64):
            for x in range(64):
                index = y * 64 + x
                pixels[x, y] = (index % 256, (index // 256) % 256, (index * 7) % 256)
        self.assertIsNone(image.getcolors(maxcolors=MAX_PALETTE))
        _data, entries, how = encode_page_image(image, COMPRESSION_LOSSLESS)
        self.assertIn("/ColorSpace /DeviceRGB", entries)
        self.assertIn("/BitsPerComponent 8", entries)
        self.assertEqual(how, "DeviceRGB")

    def test_none_keeps_the_original_encoding(self):
        image = make_page(1, (120, 90))
        data, entries, how = encode_page_image(image, COMPRESSION_NONE)
        self.assertIn("/ColorSpace /DeviceRGB", entries)
        self.assertIn("/Filter /FlateDecode", entries)
        self.assertEqual(how, "DeviceRGB")
        self.assertEqual(zlib.decompress(data), image.tobytes())

    def test_pdf_with_palette_keeps_pixels(self):
        """Celé PDF: stream stránky se musí složit zpět na původní pixely."""
        source = make_page(2, (128, 96))
        path = os.path.join(self.dir, "page_0001.png")
        source.save(path, "PNG")
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf([path], pdf, compression=COMPRESSION_LOSSLESS)
        data = _read(pdf)

        self.assertIn(b"/Indexed /DeviceRGB", data)
        self.assertNotIn(b"DCTDecode", data)
        match = re.search(
            rb"/ColorSpace \[/Indexed /DeviceRGB (\d+) <([0-9A-F]+)>\] "
            rb"/BitsPerComponent 8 /Filter /FlateDecode /Length (\d+) >>\nstream\n",
            data,
        )
        self.assertIsNotNone(match, "XObject s paletou v PDF nenalezen")
        hival, table_hex, length = int(match.group(1)), match.group(2), int(match.group(3))
        stream = data[match.end() : match.end() + length]
        self.assertEqual(len(table_hex) // 2, (hival + 1) * 3)

        indexed = Image.frombytes("P", source.size, zlib.decompress(stream))
        indexed.putpalette(bytes.fromhex(table_hex.decode("ascii")))
        self.assertEqual(indexed.convert("RGB").tobytes(), source.tobytes())

    def test_pdf_is_smaller_with_optimisation(self):
        paths = []
        for index in range(3):
            path = os.path.join(self.dir, f"page_{index + 1:04d}.png")
            make_page(index, (600, 800)).save(path, "PNG")
            paths.append(path)
        plain = os.path.join(self.dir, "plain.pdf")
        small = os.path.join(self.dir, "small.pdf")
        images_to_pdf(paths, plain, compression=COMPRESSION_NONE)
        images_to_pdf(paths, small, compression=COMPRESSION_LOSSLESS)
        self.assertLess(os.path.getsize(small), os.path.getsize(plain))


class TestBilevelG4(unittest.TestCase):
    """CCITT G4 – jediný ztrátový režim, o to víc musí sedět detaily."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_dictionary_entries_match_the_ccitt_filter(self):
        image = make_page(1, (320, 240))
        data, entries, how = encode_page_image(image, COMPRESSION_BILEVEL)
        self.assertEqual(how, "CCITT G4")
        self.assertTrue(data)
        self.assertIn("/ColorSpace /DeviceGray", entries)
        self.assertIn("/BitsPerComponent 1", entries)
        self.assertIn("/Filter /CCITTFaxDecode", entries)
        self.assertIn("/K -1", entries)          # čistá dvourozměrná G4
        self.assertIn("/Columns 320", entries)
        self.assertIn("/Rows 240", entries)

    def test_black_is_1_must_stay_set(self):
        """Bez /BlackIs1 true vyjde bílý text na černé stránce.

        Pillow zapisuje TIFF s photometric=1 (BlackIsZero), CCITTFaxDecode ale
        ve výchozím stavu čeká opačnou faxovou konvenci. Ověřeno vykreslením
        hotového PDF v prohlížeči – bez tohoto klíče je stránka negativ.
        """
        _data, entries, _how = encode_page_image(make_page(2), COMPRESSION_BILEVEL)
        self.assertIn("/BlackIs1 true", entries)

    def test_stream_is_a_single_g4_strip(self):
        """Víc TIFF stripů nelze slepit – každý se kóduje zvlášť."""
        import io

        image = make_page(3, (400, 500))
        data, _entries, _how = encode_page_image(image, COMPRESSION_BILEVEL)

        mono = image.convert("L").point(
            lambda value: 255 if value >= BILEVEL_THRESHOLD else 0, mode="1"
        )
        buffer = io.BytesIO()
        mono.save(buffer, format="TIFF", compression="group4", tiffinfo={278: 500})
        with Image.open(io.BytesIO(buffer.getvalue())) as tiff:
            offsets, counts = tiff.tag_v2[273], tiff.tag_v2[279]
        self.assertEqual(len(offsets), 1, "vynucený rowsperstrip nedal jediný strip")
        expected = buffer.getvalue()[offsets[0] : offsets[0] + counts[0]]
        self.assertEqual(data, expected)

    def test_threshold_does_not_dither(self):
        """Rozptyl odstínů by udělal ze šedé plochy šum, který se nekomprimuje."""
        light = Image.new("RGB", (64, 64), (200, 200, 200))
        dark = Image.new("RGB", (64, 64), (100, 100, 100))
        for image, expected in ((light, 255), (dark, 0)):
            mono = image.convert("L").point(
                lambda value: 255 if value >= BILEVEL_THRESHOLD else 0, mode="1"
            )
            self.assertEqual(
                mono.convert("L").getextrema(),
                (expected, expected),
                "jednolitá šeď se musí převést na jednolitou plochu",
            )

    def test_pdf_uses_ccitt_and_is_the_smallest(self):
        paths = []
        for index in range(3):
            path = os.path.join(self.dir, f"page_{index + 1:04d}.png")
            make_page(index, (600, 800)).save(path, "PNG")
            paths.append(path)

        outputs = {}
        for mode in (COMPRESSION_NONE, COMPRESSION_LOSSLESS, COMPRESSION_BILEVEL):
            pdf = os.path.join(self.dir, f"{mode}.pdf")
            images_to_pdf(paths, pdf, compression=mode)
            outputs[mode] = pdf

        data = _read(outputs[COMPRESSION_BILEVEL])
        self.assertIn(b"/Filter /CCITTFaxDecode", data)
        self.assertNotIn(b"/Indexed", data)
        self.assertNotIn(b"DCTDecode", data)

        sizes = {mode: os.path.getsize(path) for mode, path in outputs.items()}
        self.assertLess(sizes[COMPRESSION_BILEVEL], sizes[COMPRESSION_LOSSLESS])
        self.assertLess(sizes[COMPRESSION_LOSSLESS], sizes[COMPRESSION_NONE])

    def test_unknown_mode_falls_back_to_lossless(self):
        paths = [os.path.join(self.dir, "page_0001.png")]
        make_page(0, (120, 90)).save(paths[0], "PNG")
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, compression="nesmysl")
        self.assertIn(b"/Indexed", _read(pdf))


if __name__ == "__main__":
    unittest.main()

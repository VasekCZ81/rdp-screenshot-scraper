"""Testy neviditelné textové vrstvy (OCR) ve výsledném PDF."""

from __future__ import annotations

import os
import tempfile
import unittest

from helpers import make_text_page, read_bytes as _read  # noqa: E402
from pdf_reader import PdfDocument  # noqa: E402

from ocr import PageText, Word  # noqa: E402
from pdf_export import images_to_pdf  # noqa: E402


def page_text(words: list[tuple[str, float, float, float, float]], size=(900, 700)) -> PageText:
    return PageText(
        width=size[0],
        height=size[1],
        words=tuple(Word(t, x, y, w, h) for t, x, y, w, h in words),
    )


CZECH = [
    ("Příliš", 10.0, 20.0, 80.0, 18.0),
    ("žluťoučký", 100.0, 20.0, 120.0, 18.0),
    ("kůň", 230.0, 20.0, 45.0, 18.0),
    ("úpěl", 285.0, 20.0, 55.0, 18.0),
    ("ďábelské", 350.0, 20.0, 105.0, 18.0),
    ("ódy", 465.0, 20.0, 42.0, 18.0),
    ("ČSN", 10.0, 50.0, 55.0, 18.0),
    ("EN", 75.0, 50.0, 32.0, 18.0),
    ("50110-1", 115.0, 50.0, 90.0, 18.0),
]


class TextLayerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make_pdf(self, layers, count=None, size=(900, 700), compression="lossless") -> str:
        count = count if count is not None else len(layers)
        paths = []
        for index in range(count):
            path = os.path.join(self.dir, f"page_{index + 1:04d}.png")
            make_text_page(index * 40 + 1, size).save(path, "PNG")
            paths.append(path)
        pdf = os.path.join(self.dir, "result.pdf")
        images_to_pdf(paths, pdf, dpi=96, text_layers=layers, compression=compression)
        return pdf


class TestTextExtraction(TextLayerTestCase):
    def test_words_round_trip_exactly(self):
        layer = page_text(CZECH)
        doc = PdfDocument.from_file(self.make_pdf([layer]))
        self.assertEqual(doc.page_words(0), [w[0] for w in CZECH])

    def test_differences_and_tounicode_agree(self):
        """Extrakce funguje i v prohlížeči, který /ToUnicode ignoruje."""
        layer = page_text(CZECH)
        doc = PdfDocument.from_file(self.make_pdf([layer]))
        expected = [w[0] for w in CZECH]
        self.assertEqual(doc.page_words(0, doc.to_unicode_map()), expected)
        self.assertEqual(doc.page_words(0, doc.differences_map()), expected)

    def test_control_code_characters_survive(self):
        """Regrese: znaky s kódy 10 a 13 se v literálním řetězci rozbíjely.

        Kódy se přidělují podle četnosti od 1 výš, takže na 0x0A a 0x0D vždy
        padne nějaké běžné písmeno. PDF v literálním řetězci `( )` normalizuje
        konce řádků – 0x0D by se změnilo na 0x0A a dvojice 0x0D 0x0A by splynula
        v jediný bajt. Text se proto zapisuje hexadecimálně.
        """
        # 15 různých znaků, aby kódy 10 i 13 určitě padly na písmena
        words = [(ch * 3, 10.0 + i * 30, 20.0, 25.0, 18.0)
                 for i, ch in enumerate("abcdefghijklmno")]
        # slova, která by v literálním zápisu vytvořila dvojici CR LF i osamocené CR
        words.append(("jkjk", 10.0, 60.0, 40.0, 18.0))
        words.append(("mjm", 60.0, 60.0, 30.0, 18.0))
        layer = page_text(words)
        doc = PdfDocument.from_file(self.make_pdf([layer]))

        mapping = doc.to_unicode_map()
        self.assertIn(10, mapping, "kód 10 se musí použít, jinak test nic netestuje")
        self.assertIn(13, mapping, "kód 13 se musí použít, jinak test nic netestuje")
        self.assertEqual(doc.page_words(0), [w[0] for w in words])

        # v obsahu stránky nesmí být literální řetězec s textem
        content = doc.page_content(doc.page_ids()[0])
        self.assertIn(b" Tj", content)
        self.assertNotIn(b"(", content.split(b"BT")[-1])

    def test_every_used_character_has_a_mapping(self):
        layer = page_text(CZECH)
        doc = PdfDocument.from_file(self.make_pdf([layer]))
        mapping = doc.to_unicode_map()
        used = {ch for word in CZECH for ch in word[0]}
        self.assertEqual(used, set(mapping.values()))

    def test_multiple_pages_keep_their_own_text(self):
        layers = [
            page_text([("první", 10.0, 20.0, 60.0, 18.0)]),
            page_text([("druhá", 10.0, 20.0, 60.0, 18.0)]),
            page_text([("třetí", 10.0, 20.0, 60.0, 18.0)]),
        ]
        doc = PdfDocument.from_file(self.make_pdf(layers))
        self.assertEqual(doc.page_words(0), ["první"])
        self.assertEqual(doc.page_words(1), ["druhá"])
        self.assertEqual(doc.page_words(2), ["třetí"])

    def test_page_without_ocr_has_no_text(self):
        layers = [page_text([("text", 10.0, 20.0, 40.0, 18.0)]), None]
        doc = PdfDocument.from_file(self.make_pdf(layers, count=2))
        self.assertEqual(doc.page_words(0), ["text"])
        self.assertEqual(doc.page_words(1), [])


class TestTextGeometry(TextLayerTestCase):
    def test_text_is_invisible_and_positioned(self):
        layer = page_text([("Národní", 94.0, 98.0, 99.0, 20.0)])
        doc = PdfDocument.from_file(self.make_pdf([layer]))
        content = doc.page_content(doc.page_ids()[0])
        self.assertIn(b"3 Tr", content, "text musí být v neviditelném režimu")

        (size, tz, x, y) = doc.page_text_positions(0)[0]
        scale = 72.0 / 96.0
        self.assertAlmostEqual(x, 94.0 * scale, places=1)
        self.assertAlmostEqual(size, 20.0 * scale, places=1)
        # účaří leží pod horní hranou rámečku, uvnitř stránky
        self.assertGreater(y, 0)
        self.assertLess(y, 700 * scale)

    def test_horizontal_scaling_matches_word_width(self):
        """Šířka vysázeného slova musí odpovídat rámečku z OCR."""
        for text, width in (("Národní", 99.0), ("a", 12.0), ("dlouhé slovo", 210.0)):
            with self.subTest(text=text):
                layer = page_text([(text, 10.0, 20.0, width, 20.0)])
                doc = PdfDocument.from_file(self.make_pdf([layer]))
                size, tz, _x, _y = doc.page_text_positions(0)[0]
                # Courier: každý glyf je široký přesně 600/1000 em
                drawn = 0.600 * size * len(text) * (tz / 100.0)
                self.assertAlmostEqual(drawn, width * 72.0 / 96.0, places=1)

    def test_ocr_resolution_difference_is_rescaled(self):
        """OCR nad jinak velkým obrázkem se přepočítá na rozměr stránky."""
        layer = PageText(
            width=450, height=350,  # poloviční rozlišení oproti stránce 900x700
            words=(Word("test", 50.0, 25.0, 40.0, 10.0),),
        )
        doc = PdfDocument.from_file(self.make_pdf([layer]))
        _size, _tz, x, _y = doc.page_text_positions(0)[0]
        self.assertAlmostEqual(x, 100.0 * 72.0 / 96.0, places=1)

    def test_upscaled_ocr_lands_on_the_same_place(self):
        """OCR nad 2× zvětšeným snímkem musí dát stejnou pozici jako bez zvětšení."""
        plain = page_text([("test", 50.0, 25.0, 40.0, 10.0)])
        upscaled = PageText(
            width=1800, height=1400,  # OCR běželo nad 2× zvětšenou stránkou 900x700
            words=(Word("test", 100.0, 50.0, 80.0, 20.0),),
        )
        first = PdfDocument.from_file(self.make_pdf([plain])).page_text_positions(0)[0]
        second = PdfDocument.from_file(self.make_pdf([upscaled])).page_text_positions(0)[0]
        for value, other in zip(first, second):
            self.assertAlmostEqual(value, other, places=1)

    def test_upscaled_image_does_not_move_the_text(self):
        """Zvětšení obrázku pro PDF mění jen počet vzorků, ne geometrii."""
        layer = page_text([("test", 50.0, 25.0, 40.0, 10.0)])
        paths = [os.path.join(self.dir, "page_0001.png")]
        make_text_page(1, (900, 700)).save(paths[0], "PNG")

        positions = []
        for factor in (1.0, 2.0):
            pdf = os.path.join(self.dir, f"u{factor}.pdf")
            images_to_pdf(paths, pdf, dpi=96, text_layers=[layer], upscale=factor)
            positions.append(PdfDocument.from_file(pdf).page_text_positions(0)[0])
        for value, other in zip(*positions):
            self.assertAlmostEqual(value, other, places=3)


class TestPlainPdfUnaffected(TextLayerTestCase):
    def test_pdf_without_text_layers_has_no_font(self):
        pdf = self.make_pdf(None, count=2)
        data = _read(pdf)
        self.assertNotIn(b"/BaseFont", data)
        self.assertNotIn(b"/ToUnicode", data)
        self.assertIn(b"/Filter /FlateDecode", data)
        self.assertIn(b"/Count 2", data)

    def test_image_stays_lossless_with_text_layer(self):
        from PIL import Image

        layer = page_text(CZECH)
        # Bez komprese: stream je přímo RGB, takže jde porovnat s předlohou.
        pdf = self.make_pdf([layer], size=(64, 48), compression="none")
        doc = PdfDocument.from_file(pdf)
        image_id, _body = doc.find_object(b"/Subtype /Image")
        # PdfDocument streamy s /FlateDecode rozbaluje sám
        with Image.open(os.path.join(self.dir, "page_0001.png")) as source:
            self.assertEqual(doc.streams[image_id], source.convert("RGB").tobytes())


class TestXrefStillValid(TextLayerTestCase):
    def test_xref_offsets_are_correct_with_text_layer(self):
        import re

        layers = [page_text(CZECH), page_text(CZECH), None]
        pdf = self.make_pdf(layers, count=3)
        data = _read(pdf)
        start = int(re.search(rb"startxref\s+(\d+)", data).group(1))
        header = re.match(rb"xref\s+0 (\d+)\s+", data[start:])
        size = int(header.group(1))
        table = data[start + header.end():]
        self.assertEqual(table[:20], b"0000000000 65535 f \n")
        for obj_id in range(1, size):
            entry = table[obj_id * 20: obj_id * 20 + 20]
            offset = int(entry[:10])
            self.assertTrue(
                data[offset:].startswith(f"{obj_id} 0 obj".encode()),
                f"xref položka {obj_id} ukazuje jinam",
            )


if __name__ == "__main__":
    unittest.main()

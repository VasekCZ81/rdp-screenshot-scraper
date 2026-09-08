"""Sestavení výsledného PDF z pořízených PNG snímků.

Pillow ukládá RGB stránky do PDF jako JPEG (DCTDecode), což znamená ztrátovou
rekompresi – u screenshotů textu je to viditelné. Modul proto zapisuje PDF sám
a vkládá obrazová data bezeztrátově jako FlateDecode (zlib).

Vlastnosti výstupu:
  * jeden snímek = jedna stránka,
  * stránka má rozměr přesně odpovídající pixelům při zadaném DPI, takže
    nedochází k roztažení ani ke změně poměru stran,
  * žádná rotace,
  * pořadí stránek odpovídá pořadí předaných souborů.

Volitelně se nad obrázek vkládá **neviditelná textová vrstva** z OCR
(`Tr 3` = režim vykreslování „neviditelně“). Stránka vypadá úplně stejně,
ale text jde označit myší, kopírovat a hledat v něm.

Textová vrstva používá základní font Courier, který má všechny znaky široké
přesně 600/1000 em – šířku slova proto lze na jeho rámeček napasovat přesným
výpočtem horizontálního škálování (`Tz`), bez tabulek metrik. Znaky se do
fontu mapují vlastním kódováním (`/Differences` se jmény `uniXXXX`) a navíc
se přikládá `/ToUnicode` CMap, takže kopírování vrací správnou diakritiku
nezávisle na prohlížeči.
"""

from __future__ import annotations

import os
import time
import zlib
from collections import Counter
from typing import Callable, Sequence

from PIL import Image

# Courier: všechny glyfy mají šířku 600/1000 em.
COURIER_WIDTH = 0.600
# Účaří odhadneme kousek nad spodní hranou rámečku slova (místo pro dolní dotažnice).
BASELINE_RATIO = 0.82
MAX_ENCODED_CHARS = 255  # kódy 1..255, nula se nepoužívá


class PdfExportError(RuntimeError):
    pass


def _escape(text: str) -> bytes:
    out = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return out.encode("ascii", "replace")


def _hex_string(data: bytes) -> bytes:
    """Kód slova jako hexadecimální řetězec `<...>`.

    Literálním řetězcům `( ... )` se záměrně vyhýbáme: PDF v nich normalizuje
    konce řádků, takže bajt 0x0D se čte jako 0x0A a dvojice 0x0D 0x0A splyne
    v jediný bajt. Kódy znaků přidělujeme od 1 výš, takže na 10 a 13 vždy
    padne nějaké časté písmeno – v literálním řetězci by se tiše rozbilo.
    Hexadecimální zápis žádné escapování nepotřebuje.
    """
    return b"<" + data.hex().upper().encode("ascii") + b">"


# ---------------------------------------------------------------------------
class _TextEncoding:
    """Mapování znaků dokumentu na jednobajtové kódy fontu."""

    def __init__(self, layers: Sequence[object]) -> None:
        counter: Counter[str] = Counter()
        for layer in layers:
            if layer is None:
                continue
            for word in getattr(layer, "words", ()):
                counter.update(word.text)
        # Nejčastější znaky dostanou kód přednostně – kdyby jich bylo přes 255.
        chars = [ch for ch, _ in counter.most_common(MAX_ENCODED_CHARS)]
        self.code_of: dict[str, int] = {ch: index + 1 for index, ch in enumerate(chars)}
        self.chars = chars

    def __bool__(self) -> bool:
        return bool(self.chars)

    def encode(self, text: str) -> bytes:
        codes = bytearray()
        for ch in text:
            code = self.code_of.get(ch)
            if code is not None:
                codes.append(code)
        return bytes(codes)

    def differences(self) -> bytes:
        names = []
        for ch in self.chars:
            point = ord(ch)
            names.append(f"/uni{point:04X}" if point <= 0xFFFF else "/uni003F")
        return ("[1 " + " ".join(names) + "]").encode("ascii")

    def to_unicode_cmap(self) -> bytes:
        header = (
            "/CIDInit /ProcSet findresource begin\n"
            "12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<01> <FF>\nendcodespacerange\n"
        )
        body = []
        items = list(self.code_of.items())
        for start in range(0, len(items), 100):  # bfchar smí mít max 100 položek
            chunk = items[start : start + 100]
            body.append(f"{len(chunk)} beginbfchar\n")
            for ch, code in chunk:
                utf16 = ch.encode("utf-16-be").hex().upper()
                body.append(f"<{code:02X}> <{utf16}>\n")
            body.append("endbfchar\n")
        footer = "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        return (header + "".join(body) + footer).encode("ascii")


def _text_operators(
    layer,
    encoding: _TextEncoding,
    image_width: int,
    image_height: int,
    scale: float,
) -> bytes:
    """Neviditelný textový obsah jedné stránky."""
    words = [w for w in getattr(layer, "words", ()) if w.text.strip()]
    if not words:
        return b""

    # OCR mohlo běžet nad obrázkem s jiným rozměrem – přepočítáme.
    ocr_width = getattr(layer, "width", 0) or image_width
    ocr_height = getattr(layer, "height", 0) or image_height
    kx = image_width / ocr_width if ocr_width else 1.0
    ky = image_height / ocr_height if ocr_height else 1.0

    out = [b"BT\n3 Tr\n"]
    for word in words:
        encoded = encoding.encode(word.text)
        if not encoded:
            continue
        box_w = word.width * kx * scale
        box_h = word.height * ky * scale
        if box_w <= 0 or box_h <= 0:
            continue
        natural = COURIER_WIDTH * box_h * len(encoded)
        if natural <= 0:
            continue
        tz = max(1.0, min(2000.0, box_w / natural * 100.0))
        x = word.x * kx * scale
        baseline = (word.y + word.height * BASELINE_RATIO) * ky
        y = (image_height - baseline) * scale
        out.append(
            f"/F1 {box_h:.2f} Tf {tz:.2f} Tz 1 0 0 1 {x:.2f} {y:.2f} Tm ".encode("ascii")
        )
        out.append(_hex_string(encoded) + b" Tj\n")
    out.append(b"ET\n")
    if len(out) <= 2:
        return b""
    return b"".join(out)


# ---------------------------------------------------------------------------
def images_to_pdf(
    image_paths: Sequence[str],
    pdf_path: str,
    dpi: int = 96,
    text_layers: Sequence[object] | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Vytvoří PDF z uvedených obrázků. Vrací cestu k PDF.

    `text_layers` je volitelný seznam stejné délky jako `image_paths`
    s výsledky OCR (`ocr.PageText`); položka `None` znamená stránku bez textu.
    """
    paths = [p for p in image_paths if p]
    if not paths:
        raise PdfExportError("Nejsou k dispozici žádné snímky pro vytvoření PDF.")
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise PdfExportError(f"Chybí soubor se snímkem: {missing[0]}")

    dpi = max(1, int(dpi))
    scale = 72.0 / dpi
    count = len(paths)

    layers: list[object | None] = list(text_layers or [])
    layers += [None] * (count - len(layers))
    encoding = _TextEncoding(layers)

    # Stránky mají souvislá čísla objektů; ostatní se přidělují průběžně.
    page_ids = [3 + i for i in range(count)]
    next_id = 3 + count

    def alloc() -> int:
        nonlocal next_id
        value = next_id
        next_id += 1
        return value

    font_id = alloc() if encoding else 0
    to_unicode_id = alloc() if encoding else 0

    offsets: dict[int, int] = {}
    tmp_path = pdf_path + ".tmp"

    try:
        with open(tmp_path, "wb") as fh:

            def write_obj(obj_id: int, body: bytes, stream: bytes | None = None) -> None:
                offsets[obj_id] = fh.tell()
                fh.write(f"{obj_id} 0 obj\n".encode("ascii"))
                fh.write(body)
                if stream is not None:
                    fh.write(b"\nstream\n")
                    fh.write(stream)
                    fh.write(b"\nendstream")
                fh.write(b"\nendobj\n")

            fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

            write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")

            kids = " ".join(f"{pid} 0 R" for pid in page_ids)
            write_obj(
                2,
                f"<< /Type /Pages /Count {count} /Kids [{kids}] >>".encode("ascii"),
            )

            if encoding:
                cmap = encoding.to_unicode_cmap()
                compressed_cmap = zlib.compress(cmap, 6)
                write_obj(
                    to_unicode_id,
                    f"<< /Filter /FlateDecode /Length {len(compressed_cmap)} >>".encode("ascii"),
                    compressed_cmap,
                )
                write_obj(
                    font_id,
                    b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
                    b"/Encoding << /Type /Encoding /Differences "
                    + encoding.differences()
                    + b" >> /ToUnicode "
                    + f"{to_unicode_id} 0 R".encode("ascii")
                    + b" >>",
                )

            words_total = 0
            for index, path in enumerate(paths):
                with Image.open(path) as img:
                    img.load()
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    width, height = img.size
                    raw = img.tobytes()

                if width <= 0 or height <= 0:
                    raise PdfExportError(f"Snímek {path} má nulový rozměr.")

                compressed = zlib.compress(raw, 6)
                del raw

                image_id = alloc()
                write_obj(
                    image_id,
                    (
                        "<< /Type /XObject /Subtype /Image "
                        f"/Width {width} /Height {height} "
                        "/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                        f"/Filter /FlateDecode /Length {len(compressed)} >>"
                    ).encode("ascii"),
                    compressed,
                )
                del compressed

                # Rozměr stránky v bodech (1 bod = 1/72"), aby 1 px = 1/dpi palce.
                pw = width * scale
                ph = height * scale
                content = f"q {pw:.4f} 0 0 {ph:.4f} 0 0 cm /Im0 Do Q\n".encode("ascii")

                layer = layers[index]
                if encoding and layer is not None:
                    text_ops = _text_operators(layer, encoding, width, height, scale)
                    if text_ops:
                        content += text_ops
                        words_total += len(getattr(layer, "words", ()))

                content_id = alloc()
                compressed_content = zlib.compress(content, 6)
                write_obj(
                    content_id,
                    f"<< /Filter /FlateDecode /Length {len(compressed_content)} >>".encode("ascii"),
                    compressed_content,
                )

                font_res = (
                    f"/Font << /F1 {font_id} 0 R >> " if (encoding and layer is not None) else ""
                )
                procset = "/PDF /ImageC /Text" if encoding else "/PDF /ImageC"
                write_obj(
                    page_ids[index],
                    (
                        "<< /Type /Page /Parent 2 0 R "
                        f"/MediaBox [0 0 {pw:.4f} {ph:.4f}] "
                        f"/Resources << /ProcSet [{procset}] "
                        f"{font_res}"
                        f"/XObject << /Im0 {image_id} 0 R >> >> "
                        f"/Contents {content_id} 0 R >>"
                    ).encode("ascii"),
                )

                if log:
                    log(f"PDF: přidána stránka {index + 1}/{count} ({os.path.basename(path)})")

            info_id = alloc()
            stamp = time.strftime("D:%Y%m%d%H%M%S")
            write_obj(
                info_id,
                b"<< /Producer ("
                + _escape("RDP Screenshot Scraper")
                + b") /CreationDate ("
                + _escape(stamp)
                + b") >>",
            )

            if log and words_total:
                log(f"PDF: vložena textová vrstva OCR, slov celkem {words_total}")

            max_id = next_id - 1
            xref_offset = fh.tell()
            fh.write(f"xref\n0 {max_id + 1}\n".encode("ascii"))
            fh.write(b"0000000000 65535 f \n")
            for obj_id in range(1, max_id + 1):
                fh.write(f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii"))
            fh.write(
                f"trailer\n<< /Size {max_id + 1} /Root 1 0 R /Info {info_id} 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
            )
    except PdfExportError:
        _cleanup(tmp_path)
        raise
    except (OSError, ValueError, MemoryError) as exc:
        _cleanup(tmp_path)
        raise PdfExportError(f"Vytvoření PDF selhalo: {exc}") from exc

    try:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
        os.replace(tmp_path, pdf_path)
    except OSError as exc:
        _cleanup(tmp_path)
        raise PdfExportError(f"Nelze uložit PDF do {pdf_path}: {exc}") from exc

    return pdf_path


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

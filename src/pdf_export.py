"""Sestavení výsledného PDF z pořízených PNG snímků.

Pillow ukládá RGB stránky do PDF jako JPEG (DCTDecode), což znamená ztrátovou
rekompresi – u screenshotů textu je to viditelné. Modul proto zapisuje PDF sám
a vkládá obrazová data bezeztrátově jako FlateDecode (zlib).

Vlastnosti výstupu:
  * jeden snímek = jedna stránka,
  * stránka má rozměr přesně odpovídající pixelům při zadaném DPI, takže
    nedochází k roztažení ani ke změně poměru stran,
  * místo pevného DPI lze zadat fyzickou šířku předlohy (`page_width_mm`)
    a DPI se pro každou stránku dopočítá z její šířky v pixelech – stránky
    pak mají reálnou velikost dokumentu a prohlížeč je nezmenšuje na zlomek
    (právě to zmenšování dělá z ostrého snímku rozmazaný text),
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

import io
import os
import time
import zlib
from collections import Counter
from typing import Callable, Sequence

from PIL import Image, ImageChops, ImageFilter

# Courier: všechny glyfy mají šířku 600/1000 em.
COURIER_WIDTH = 0.600
MM_PER_INCH = 25.4
MIN_DPI = 1
MAX_DPI = 1200
MAX_UPSCALE = 4.0
MAX_SHARPEN = 300.0
# Práh drží ploché plochy beze změny – doostřuje se jen na hranách písma,
# takže se nezvýrazní artefakty kodeku RDP v prázdném papíru.
SHARPEN_THRESHOLD = 3
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
def dpi_for_width(width_px: int, page_width_mm: float) -> float:
    """DPI, při kterém bude snímek široký `width_px` odpovídat `page_width_mm`.

    Například stránka A4 (210 mm) nasnímaná v šířce 1600 px vyjde na 193 DPI.
    Výsledek je omezen na rozsah, který dává v PDF smysl.
    """
    if width_px <= 0 or page_width_mm <= 0:
        raise ValueError("Šířka snímku i předlohy musí být kladná.")
    dpi = width_px / (page_width_mm / MM_PER_INCH)
    return max(float(MIN_DPI), min(float(MAX_DPI), dpi))


def prepare_image(
    img: Image.Image, upscale: float = 1.0, sharpen: float = 0.0
) -> Image.Image:
    """Zvětšení (LANCZOS) a doostření (unsharp mask) jednoho snímku.

    Doostření se dělá až po zvětšení a poloměr roste s faktorem – tah písma je
    po zvětšení širší, takže na něj musí sáhnout širší maska. Detail to
    nevrátí; zvýší kontrast na hranách, které přeškálování a kodek RDP
    rozmazaly.
    """
    factor = max(1.0, min(MAX_UPSCALE, float(upscale or 1.0)))
    amount = max(0.0, min(MAX_SHARPEN, float(sharpen or 0.0)))

    target = (max(1, round(img.width * factor)), max(1, round(img.height * factor)))
    if target != img.size:
        img = img.resize(target, Image.LANCZOS)
    if amount > 0:
        img = img.filter(
            ImageFilter.UnsharpMask(
                radius=max(0.5, factor), percent=int(round(amount)),
                threshold=SHARPEN_THRESHOLD,
            )
        )
    return img


# Indexovaná paleta se vejde nanejvýš na 256 barev.
MAX_PALETTE = 256

# Režimy komprese obrazu ve výsledném PDF.
COMPRESSION_NONE = "none"          # DeviceRGB, flate-6 – jako v prvních verzích
COMPRESSION_LOSSLESS = "lossless"  # indexovaná paleta, obraz beze změny
COMPRESSION_BILEVEL = "g4"         # CCITT G4, 1 bit na pixel – ZTRÁTOVÉ
COMPRESSIONS = (COMPRESSION_NONE, COMPRESSION_LOSSLESS, COMPRESSION_BILEVEL)

# Práh převodu na černobílou. Vyšší hodnota nechá písmu víc tahu: pixely
# vyhlazení pod prahem zčernají. Naměřeno na skutečných snímcích – 128 písmo
# ztenčuje, 176 drží tah blízko předloze a na velikost nemá vliv.
BILEVEL_THRESHOLD = 176

# Značky TIFF, ze kterých se vytahuje hotový G4 stream.
TIFF_STRIPOFFSETS = 273
TIFF_ROWSPERSTRIP = 278
TIFF_STRIPBYTECOUNTS = 279


def encode_page_image(
    image: Image.Image, compression: str = COMPRESSION_LOSSLESS
) -> tuple[bytes, str, str]:
    """Zakóduje stránku do streamu XObjectu. Vrací (data, klíče slovníku, popis).

    Snímky vzdálené plochy mívají jen několik desítek barev – text je černý,
    papír bílý a mezi tím pár odstínů vyhlazení. Tři bajty na pixel jsou pak
    zbytečné: s indexovanou paletou stačí jeden bajt plus tabulka barev,
    a obraz zůstává **bit po bitu stejný**.

    Naměřeno na devatenáctistránkovém dokumentu 2406×3387 px:

    | varianta            | obrazová data | podíl |
    |---------------------|---------------|-------|
    | DeviceRGB, flate-6  | 7,68 MB       | 100 % |
    | DeviceRGB, flate-9  | 7,31 MB       |  95 % |
    | indexovaná paleta   | 5,52 MB       |  72 % |

    | CCITT G4, 1 bit    | 1,26 MB       |  16 % |

    PNG prediktor se záměrně nepoužívá: na velkých jednolitých plochách je
    **horší** než prostý flate (naměřeno 128 %). Diference rozseká dlouhé
    shodné běhy a na hranách písmen vyrobí vysokou entropii.

    `COMPRESSION_BILEVEL` je jediný **ztrátový** režim: stránka se převede na
    čistě černobílou, takže zmizí vyhlazení písma a šedé výplně ve výkresech.
    Pro čistě textové dokumenty je to nejmenší možný výstup, jinde se nehodí.
    """
    if compression == COMPRESSION_BILEVEL:
        bilevel = _bilevel_stream(image)
        if bilevel is not None:
            data, entries = bilevel
            return data, entries, "CCITT G4"
        # Kodek není k dispozici – radši bezeztrátově než spadnout.
        compression = COMPRESSION_LOSSLESS

    if compression == COMPRESSION_NONE:
        return (
            zlib.compress(image.tobytes(), 6),
            "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode",
            "DeviceRGB",
        )

    indexed = _indexed_image(image)
    if indexed is None:
        return (
            zlib.compress(image.tobytes(), 9),
            "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode",
            "DeviceRGB",
        )

    data, table, hival = indexed
    entries = (
        f"/ColorSpace [/Indexed /DeviceRGB {hival} <{table.hex().upper()}>] "
        "/BitsPerComponent 8 /Filter /FlateDecode"
    )
    return zlib.compress(data, 9), entries, f"paleta {hival + 1} barev"


def _bilevel_stream(image: Image.Image) -> tuple[bytes, str] | None:
    """CCITT G4 přes zapisovač TIFF v Pillow. `None`, když kodek chybí.

    Dvě věci, na kterých se to obvykle rozbije:

    * **Dithering.** `convert("1")` rozptýlí odstíny vyhlazení do šumu, který
      se nedá komprimovat a text vypadá zrnitě. Prahuje se proto natvrdo.
    * **Počet stripů.** TIFF si data dělí po ~8 kB a každý strip kóduje zvlášť,
      takže je nelze prostě slepit. `RowsPerStrip` = výška vynutí jediný strip,
      který jde do PDF beze změny.

    Polarita se **musí** převrátit přes `/BlackIs1 true`. Pillow zapisuje TIFF
    s `photometric=1` (BlackIsZero), ale `CCITTFaxDecode` ve výchozím stavu
    (`/BlackIs1 false`) čeká faxovou konvenci opačnou. Bez toho vyjde bílý text
    na černé stránce – ověřeno vykreslením hotového PDF v prohlížeči.
    """
    width, height = image.size
    mono = image.convert("L").point(
        lambda value: 255 if value >= BILEVEL_THRESHOLD else 0, mode="1"
    )
    buffer = io.BytesIO()
    try:
        mono.save(
            buffer,
            format="TIFF",
            compression="group4",
            tiffinfo={TIFF_ROWSPERSTRIP: height},
        )
    except (OSError, ValueError, KeyError):
        return None

    with Image.open(io.BytesIO(buffer.getvalue())) as tiff:
        offsets = tiff.tag_v2.get(TIFF_STRIPOFFSETS) or ()
        counts = tiff.tag_v2.get(TIFF_STRIPBYTECOUNTS) or ()
    if len(offsets) != 1 or len(counts) != 1:
        return None  # víc stripů slepit nelze

    raw = buffer.getvalue()
    data = raw[offsets[0] : offsets[0] + counts[0]]
    entries = (
        "/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /CCITTFaxDecode "
        f"/DecodeParms << /K -1 /Columns {width} /Rows {height} /BlackIs1 true >>"
    )
    return data, entries


def _indexed_image(image: Image.Image) -> tuple[bytes, bytes, int] | None:
    """Převod na indexovanou paletu. `None`, když by to nebylo bezeztrátové."""
    if image.mode != "RGB":
        return None
    colours = image.getcolors(maxcolors=MAX_PALETTE)
    if not colours:
        return None

    try:
        # MEDIANCUT vrací u obrázků s méně než 256 barvami přesně tytéž barvy
        # a je rychlejší než FASTOCTREE, který přesný není (naměřeno).
        indexed = image.quantize(
            colors=len(colours), method=Image.Quantize.MEDIANCUT
        )
    except (ValueError, OSError):
        return None

    # Kvantizace smí barvy sloučit; bereme ji jen tehdy, když vrátí přesně
    # tytéž pixely. Jinak radši DeviceRGB – kvalita je přednější než velikost.
    if ImageChops.difference(indexed.convert("RGB"), image).getbbox() is not None:
        return None

    hival = indexed.getextrema()[1]
    table = bytes((indexed.getpalette() or [])[: (hival + 1) * 3])
    if len(table) != (hival + 1) * 3:
        return None
    return indexed.tobytes(), table, hival


def images_to_pdf(
    image_paths: Sequence[str],
    pdf_path: str,
    dpi: int = 96,
    text_layers: Sequence[object] | None = None,
    log: Callable[[str], None] | None = None,
    page_width_mm: float | None = None,
    upscale: float = 1.0,
    sharpen: float = 0.0,
    compression: str = COMPRESSION_LOSSLESS,
) -> str:
    """Vytvoří PDF z uvedených obrázků. Vrací cestu k PDF.

    `text_layers` je volitelný seznam stejné délky jako `image_paths`
    s výsledky OCR (`ocr.PageText`); položka `None` znamená stránku bez textu.

    `page_width_mm` (kladné číslo) je fyzická šířka předlohy. Je-li zadaná,
    má přednost před `dpi` a rozlišení stránky se dopočítá z šířky snímku.

    `upscale` zvětší obrázek před vložením (LANCZOS). Fyzická velikost stránky
    se nemění – do stejného rámce se jen vloží víc vzorků, takže prohlížeč ani
    tiskárna nepracuje s tak hrubou předlohou. Detail to nepřidá, soubor
    naroste zhruba s druhou mocninou faktoru.

    `sharpen` je síla doostření v procentech (unsharp mask, 0 = vypnuto).
    Rozumné hodnoty jsou 80–150; vyšší dělá kolem písmen světlé lemy.

    `compression` volí kódování obrazu – viz `encode_page_image()`:
    `COMPRESSION_LOSSLESS` (indexovaná paleta, obraz beze změny),
    `COMPRESSION_NONE` (DeviceRGB jako v prvních verzích) nebo
    `COMPRESSION_BILEVEL` (CCITT G4, nejmenší soubor, ale ztrátové).
    """
    if compression not in COMPRESSIONS:
        compression = COMPRESSION_LOSSLESS
    paths = [p for p in image_paths if p]
    if not paths:
        raise PdfExportError("Nejsou k dispozici žádné snímky pro vytvoření PDF.")
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise PdfExportError(f"Chybí soubor se snímkem: {missing[0]}")

    dpi = max(MIN_DPI, min(MAX_DPI, int(dpi)))
    scale = 72.0 / dpi
    auto_width_mm = float(page_width_mm) if page_width_mm else 0.0
    if auto_width_mm < 0:
        raise PdfExportError("Šířka předlohy nesmí být záporná.")
    factor = max(1.0, min(MAX_UPSCALE, float(upscale or 1.0)))
    amount = max(0.0, min(MAX_SHARPEN, float(sharpen or 0.0)))
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
            palette_pages = 0
            bilevel_pages = 0
            image_bytes = 0
            for index, path in enumerate(paths):
                with Image.open(path) as img:
                    img.load()
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    width, height = img.size
                    if width <= 0 or height <= 0:
                        raise PdfExportError(f"Snímek {path} má nulový rozměr.")
                    # Rámec stránky vychází z původního rozměru; zvětšení mění
                    # jen počet vzorků uvnitř něj.
                    img = prepare_image(img, factor, amount)
                    sample_w, sample_h = img.size
                    # Kopie přežije zavření souboru; kóduje se až níž, kdy je
                    # známo, jestli se má zapnout bezeztrátová optimalizace.
                    sample = img.copy()

                # DPI buď pevné, nebo dopočítané tak, aby stránka byla široká
                # přesně `auto_width_mm` – každá stránka zvlášť, kdyby se
                # rozměry snímků lišily.
                if auto_width_mm:
                    page_dpi = dpi_for_width(width, auto_width_mm)
                    if log and index == 0:
                        log(
                            f"PDF: šířka předlohy {auto_width_mm:.1f} mm při {width} px "
                            f"=> {page_dpi:.0f} DPI"
                        )
                else:
                    page_dpi = float(dpi)
                page_scale = 72.0 / page_dpi
                if log and index == 0:
                    if (sample_w, sample_h) != (width, height):
                        log(
                            f"PDF: snímky zvětšeny {factor:g}× na {sample_w}×{sample_h} px "
                            f"(hustota stránky {page_dpi * factor:.0f} DPI)"
                        )
                    if amount > 0:
                        log(f"PDF: doostření {amount:g} %")

                compressed, entries, how = encode_page_image(sample, compression)
                sample.close()
                del sample
                if how.startswith("paleta"):
                    palette_pages += 1
                elif how == "CCITT G4":
                    bilevel_pages += 1
                image_bytes += len(compressed)

                image_id = alloc()
                write_obj(
                    image_id,
                    (
                        "<< /Type /XObject /Subtype /Image "
                        f"/Width {sample_w} /Height {sample_h} "
                        f"{entries} /Length {len(compressed)} >>"
                    ).encode("ascii"),
                    compressed,
                )
                del compressed

                # Rozměr stránky v bodech (1 bod = 1/72"), aby 1 px = 1/dpi palce.
                pw = width * page_scale
                ph = height * page_scale
                content = f"q {pw:.4f} 0 0 {ph:.4f} 0 0 cm /Im0 Do Q\n".encode("ascii")

                layer = layers[index]
                if encoding and layer is not None:
                    text_ops = _text_operators(layer, encoding, width, height, page_scale)
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
            # Bez /Producer: o použitém nástroji nemá výsledné PDF nic říkat.
            write_obj(
                info_id,
                b"<< /CreationDate (" + _escape(stamp) + b") >>",
            )

            if log and words_total:
                log(f"PDF: vložena textová vrstva OCR, slov celkem {words_total}")
            if log:
                summary = f"PDF: obrazová data {image_bytes / 1024 / 1024:.2f} MB"
                if bilevel_pages:
                    summary += f", CCITT G4 na {bilevel_pages} z {count} stránek"
                elif compression == COMPRESSION_LOSSLESS:
                    summary += (
                        f", bezeztrátová paleta na {palette_pages} z {count} stránek"
                    )
                log(summary)

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

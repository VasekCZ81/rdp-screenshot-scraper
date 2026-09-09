"""Vymazání zvolené oblasti ze všech snímků.

Uživatel označí na první stránce obdélník – například lištu Adobe Readeru,
vodoznak, hlavičku nebo číslo stránky – a ten se na **všech** snímcích
přebarví na bílo. Souřadnice se udávají v pixelech snímané oblasti, takže
platí pro každou stránku stejně.

Maska se uplatní ještě před OCR, takže se vymazaný text nedostane ani do
neviditelné textové vrstvy PDF. Pořízená PNG zůstávají nedotčená – vymazané
kopie vznikají vedle nich v podadresáři `masked/`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageDraw

MASK_COLOR = (255, 255, 255)  # bílá = prázdné místo


class MaskError(RuntimeError):
    """Vymazání se nepovedlo. Nikdy nesmí zahodit původní snímky."""


@dataclass(frozen=True)
class MaskRect:
    """Obdélník k vymazání, v pixelech snímané oblasti."""

    x: int
    y: int
    width: int
    height: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    def clipped(self, image_width: int, image_height: int) -> "MaskRect | None":
        """Ořízne obdélník na rozměr snímku; mimo snímek vrátí None."""
        left = max(0, min(self.x, image_width))
        top = max(0, min(self.y, image_height))
        right = max(0, min(self.x + self.width, image_width))
        bottom = max(0, min(self.y + self.height, image_height))
        if right <= left or bottom <= top:
            return None
        return MaskRect(left, top, right - left, bottom - top)

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]

    def __str__(self) -> str:
        return f"x={self.x} y={self.y} w={self.width} h={self.height}"


def normalize_rects(raw: Iterable) -> list[MaskRect]:
    """Převede hodnoty z config.json na obdélníky; nesmysly zahodí."""
    rects: list[MaskRect] = []
    for item in raw or []:
        try:
            if isinstance(item, MaskRect):
                x, y, width, height = item.x, item.y, item.width, item.height
            elif isinstance(item, dict):
                x, y = int(item["x"]), int(item["y"])
                width, height = int(item["width"]), int(item["height"])
            else:
                x, y, width, height = (int(value) for value in tuple(item)[:4])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if width <= 0 or height <= 0:
            continue
        rects.append(MaskRect(max(0, x), max(0, y), width, height))
    return rects


def rects_to_config(rects: Sequence[MaskRect]) -> list[list[int]]:
    return [rect.as_list() for rect in rects]


def draw_masks(image: Image.Image, rects: Sequence[MaskRect]) -> Image.Image:
    """Vrátí kopii snímku s vybarvenými obdélníky."""
    result = image.convert("RGB") if image.mode != "RGB" else image.copy()
    draw = ImageDraw.Draw(result)
    width, height = result.size
    for rect in rects:
        clipped = rect.clipped(width, height)
        if clipped is None:
            continue
        left, top, right, bottom = clipped.box
        draw.rectangle([left, top, right - 1, bottom - 1], fill=MASK_COLOR)
    return result


def apply_masks(
    image_paths: Sequence[str],
    rects: Sequence[MaskRect],
    out_dir: str,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Uloží vymazané kopie snímků do `out_dir`. Vrací cesty ke kopiím."""
    paths = [p for p in image_paths if p]
    if not paths:
        raise MaskError("Nejsou k dispozici žádné snímky k vymazání oblasti.")
    if not rects:
        return list(paths)

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for path in paths:
        with Image.open(path) as source:
            source.load()
            masked = draw_masks(source, rects)
        target = os.path.join(out_dir, os.path.basename(path))
        masked.save(target, "PNG")
        masked.close()
        written.append(target)

    if log:
        area = ", ".join(str(rect) for rect in rects)
        log(f"Vymazáno {len(rects)} oblastí na {len(written)} snímcích: {area}")
    return written

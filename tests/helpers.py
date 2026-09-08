"""Společné pomůcky pro testy."""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def make_page(seed: int, size: tuple[int, int] = (200, 150)) -> Image.Image:
    """Deterministický "text-like" obrázek – různý seed = jiná stránka."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    for row in range(10):
        offset = (seed * 17 + row * 29) % 90
        width = 40 + (seed * 13 + row * 7) % 100
        y = 8 + row * 14
        draw.rectangle([10 + offset, y, 10 + offset + width, y + 8], fill="black")
    return img


def make_text_page(first_line: int, size: tuple[int, int] = (900, 700)) -> Image.Image:
    """Stránka hustého textu – přesně to, co aplikace ve skutečnosti snímá.

    Dvě takové stránky vypadají po zmenšení stejně (šedá textura), proto se
    porovnání musí dělat v plném rozlišení.
    """
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    y = 8
    line = first_line
    while y < size[1] - 14:
        text = f"radek {line:04d}  " + "=" * (10 + (line * 7) % 40) + f"  konec {line:04d}"
        draw.text((10, y), text, fill="black")
        y += 14
        line += 1
    return img


def add_clock(image: Image.Image, text: str = "12:34:56") -> Image.Image:
    """Napodobí hodiny v rohu – malá, ale reálná změna obsahu."""
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    draw.rectangle([copy.width - 90, 2, copy.width - 4, 16], fill="white")
    draw.text((copy.width - 88, 3), text, fill="black")
    return copy


def add_noise(image: Image.Image) -> Image.Image:
    """Napodobí blikající kurzor – nepatrná změna, která nesmí ukončit snímání."""
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    draw.rectangle([2, 2, 5, 12], fill="black")
    return copy

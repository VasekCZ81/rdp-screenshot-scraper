"""Porovnání dvou snímků pro detekci konce dokumentu.

Nepoužívá binární shodu souborů. Porovnává se v **plném rozlišení** – zmenšení
snímku před porovnáním by u dokumentů zahodilo právě tu informaci, která
jednotlivé stránky textu odlišuje (dvě různé stránky hustého textu mají po
zmenšení prakticky stejnou šedou texturu).

Použité metriky:

  1. `changed_fraction` – podíl pixelů, které se změnily výrazně
     (o více než `CHANGED_PIXEL_LEVEL` úrovní jasu). Necitlivá na
     antialiasing a na drobné změny jasu.
  2. `mean_diff` – normalizovaný průměrný absolutní rozdíl jasu.
     Zachytí i změnu, která je rozprostřená po celé ploše.
  3. `hamming` – difference hash (dHash) jako doplňková pojistka na
     hrubou strukturu obrazu.

Snímky považujeme za "prakticky nezměněné", jen když se na tom shodnou
**všechny** metriky. Tato konzervativní volba raději pokračuje ve snímání,
než aby předčasně ohlásila konec dokumentu a zkrátila výsledné PDF.

Typické naměřené hodnoty (oblast 2160x1130 px):

  * tentýž snímek ............... changed 0.00000  mean 0.00000
  * blikající kurzor ............ changed ~0.00002 mean ~0.00002
  * hodiny / drobná animace ..... changed ~0.002   mean ~0.001
  * posun dokumentu o stránku ... changed 0.044    mean 0.031
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

HASH_SIZE = 8               # dHash 8x9 -> 64 bitů
CHANGED_PIXEL_LEVEL = 32    # rozdíl jasu, od kterého pixel počítáme jako změněný
MAX_COMPARE_PIXELS = 8_000_000  # strop pro extrémně velké oblasti


@dataclass(frozen=True)
class ComparisonResult:
    hamming: int
    pixel_diff: float
    changed_fraction: float
    identical: bool

    def describe(self) -> str:
        return (
            f"dHash={self.hamming} pixelDiff={self.pixel_diff:.5f} "
            f"změněno={self.changed_fraction:.5f}"
        )


def _grayscale(image: Image.Image) -> Image.Image:
    """Odstín šedi v plném rozlišení; zmenší jen extrémně velké oblasti."""
    gray = image.convert("L")
    pixels = gray.width * gray.height
    if pixels > MAX_COMPARE_PIXELS:
        scale = (MAX_COMPARE_PIXELS / pixels) ** 0.5
        gray = gray.resize(
            (max(1, int(gray.width * scale)), max(1, int(gray.height * scale))),
            Image.Resampling.BILINEAR,
        )
    return gray


def dhash(image: Image.Image, hash_size: int = HASH_SIZE) -> int:
    """Difference hash – porovnává sousední pixely v řádku."""
    small = image.convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.BILINEAR
    )
    pixels = small.tobytes()  # režim "L" – jeden bajt na pixel, po řádcích
    bits = 0
    index = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1 << index
            index += 1
    return bits


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def difference_metrics(a: Image.Image, b: Image.Image) -> tuple[float, float]:
    """Vrátí (mean_diff, changed_fraction) v rozsahu 0.0-1.0."""
    diff = ImageChops.difference(_grayscale(a), _grayscale(b))
    mean_diff = ImageStat.Stat(diff).mean[0] / 255.0
    mask = diff.point(lambda value: 255 if value > CHANGED_PIXEL_LEVEL else 0)
    changed_fraction = ImageStat.Stat(mask).mean[0] / 255.0
    return mean_diff, changed_fraction


def mean_pixel_difference(a: Image.Image, b: Image.Image) -> float:
    return difference_metrics(a, b)[0]


def compare(
    previous: Image.Image,
    current: Image.Image,
    hash_threshold: int = 4,
    pixel_threshold: float = 0.01,
    changed_threshold: float = 0.005,
) -> ComparisonResult:
    """Porovná dva snímky a rozhodne, zda jsou prakticky totožné."""
    if previous.size != current.size:
        return ComparisonResult(
            hamming=64, pixel_diff=1.0, changed_fraction=1.0, identical=False
        )

    mean_diff, changed_fraction = difference_metrics(previous, current)
    hamming = hamming_distance(dhash(previous), dhash(current))
    identical = (
        changed_fraction <= changed_threshold
        and mean_diff <= pixel_threshold
        and hamming <= hash_threshold
    )
    return ComparisonResult(
        hamming=hamming,
        pixel_diff=mean_diff,
        changed_fraction=changed_fraction,
        identical=identical,
    )

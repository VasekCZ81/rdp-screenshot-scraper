"""Pořizování screenshotů zvolené oblasti obrazovky.

Používá `mss` (BitBlt nad desktop DC) – kurzor myši se do snímku nezachytává.
Souřadnice jsou fyzické pixely virtuální plochy, včetně záporných hodnot
monitorů vlevo/nad primárním monitorem.
"""

from __future__ import annotations

from dataclasses import dataclass

import mss
from PIL import Image

from window_manager import virtual_screen_rect

# mss 10 přejmenovalo tovární funkci na MSS; starší verze mají jen mss.mss.
_MSS_FACTORY = getattr(mss, "MSS", None) or mss.mss


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> dict:
        return {
            "left": self.x,
            "top": self.y,
            "width": self.width,
            "height": self.height,
        }

    def __str__(self) -> str:
        return f"x={self.x} y={self.y} w={self.width} h={self.height}"


def validate_region(region: Region | None) -> None:
    """Vyhodí CaptureError, pokud oblast není použitelná."""
    if region is None:
        raise CaptureError("Není vybrána žádná oblast obrazovky.")
    if region.width <= 0 or region.height <= 0:
        raise CaptureError(
            f"Neplatná velikost oblasti ({region.width}x{region.height} px)."
        )
    vx, vy, vw, vh = virtual_screen_rect()
    left = max(region.x, vx)
    top = max(region.y, vy)
    right = min(region.x + region.width, vx + vw)
    bottom = min(region.y + region.height, vy + vh)
    if right - left <= 0 or bottom - top <= 0:
        raise CaptureError("Vybraná oblast leží mimo plochu monitorů.")


class ScreenCapturer:
    """Obal nad mss. Instanci vytvářej ve vlákně, které bude snímat."""

    def __init__(self) -> None:
        self._sct = _MSS_FACTORY()

    def grab(self, region: Region) -> Image.Image:
        validate_region(region)
        try:
            raw = self._sct.grab(region.as_dict())
        except Exception as exc:  # mss vyhazuje vlastní typy chyb
            raise CaptureError(f"Screenshot se nepodařilo pořídit: {exc}") from exc
        image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        if image.size != (region.width, region.height):
            raise CaptureError(
                f"Screenshot má rozměr {image.size}, očekáváno "
                f"({region.width}, {region.height})."
            )
        return image

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    def __enter__(self) -> "ScreenCapturer":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

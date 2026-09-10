"""Snímání obsahu okna RDP relace přes `PrintWindow`.

Snímá se přímo HWND okna `mstsc.exe`, nikoli plocha monitoru. To má dva
důsledky, na kterých stojí celá aplikace:

1. **Okno smí být větší než monitor.** S příznakem `PW_RENDERFULLCONTENT`
   vrací Windows i tu část okna, která leží mimo obrazovku. Relace RDP tedy
   může být vysoká 3600 px na monitoru vysokém 2160 px a celá stránka PDF se
   vejde do jednoho snímku – bez posouvání a bez skládání.
2. **Na okno může cokoli ležet.** Snímek vzniká z obsahu okna, ne z toho, co
   je zrovna vidět na ploše. Total Commander tedy smí okno RDP překrývat.

Souřadnice snímané oblasti jsou proto pixely **client rectu okna RDP**, ne
souřadnice obrazovky. Nezávisí tak na tom, kde okno na ploše leží.

Desktopové snímání (`mss`, `BitBlt` nad plochou) se záměrně nepoužívá ani
jako tichý fallback: vracelo by obsah překrývajícího okna nebo černou plochu
místo stránky dokumentu.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image

from window_manager import (
    client_offset,
    client_rect,
    is_iconic,
    is_window,
    is_zoomed as wm_is_zoomed,
    monitor_rect as wm_monitor_rect,
    restore_window as wm_restore_window,
    set_window_geometry as wm_set_geometry,
    wait_for_stable_size as wm_wait_for_stable_size,
    window_rect as wm_window_rect,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]

PW_RENDERFULLCONTENT = 0x2
BI_RGB = 0
DIB_RGB_COLORS = 0

# Snímek považujeme za neplatný, jen když je jednolitý A tmavý. Prázdná bílá
# stránka dokumentu je legitimní výsledek, černý snímek je vždycky chyba.
BLANK_SPREAD = 8
BLANK_DARK_MEAN = 32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class Region:
    """Obdélník v pixelech client rectu okna RDP (levý horní roh = 0,0)."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.right, self.bottom)

    def __str__(self) -> str:
        return f"x={self.x} y={self.y} w={self.width} h={self.height}"


def validate_region(region: Region | None, client: tuple[int, int] | None = None) -> None:
    """Vyhodí `CaptureError`, pokud oblast není použitelná.

    Když je znám rozměr client rectu, ověří se i to, že oblast leží uvnitř
    okna – jinak by se snímala část, kterou okno vůbec nevykresluje.
    """
    if region is None:
        raise CaptureError("Není vybrána žádná oblast okna RDP.")
    if region.width <= 0 or region.height <= 0:
        raise CaptureError(
            f"Neplatná velikost oblasti ({region.width}x{region.height} px)."
        )
    if region.x < 0 or region.y < 0:
        raise CaptureError(
            f"Oblast začíná mimo okno RDP ({region.x}, {region.y})."
        )
    if client is not None:
        width, height = client
        if region.right > width or region.bottom > height:
            raise CaptureError(
                f"Oblast {region} přesahuje okno RDP ({width}x{height} px). "
                "Vyberte ji znovu – okno nejspíš změnilo velikost."
            )


def capture_window(hwnd: int) -> Image.Image:
    """Vrátí obsah celého okna včetně rámu, i té části mimo obrazovku."""
    if not is_window(hwnd):
        raise CaptureError("Okno RDP relace neexistuje.")
    if is_iconic(hwnd):
        raise CaptureError(
            "Okno RDP relace je minimalizované – z minimalizovaného okna nelze "
            "snímat. Obnovte ho a spusťte snímání znovu."
        )

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError("Nepodařilo se zjistit rozměry okna RDP.")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise CaptureError(f"Okno RDP má neplatný rozměr {width}x{height} px.")

    screen_dc = user32.GetDC(0)
    if not screen_dc:
        raise CaptureError("Nepodařilo se získat kontext obrazovky.")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not mem_dc:
        user32.ReleaseDC(0, screen_dc)
        raise CaptureError("Nepodařilo se vytvořit paměťový kontext.")

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # záporná výška = řádky shora dolů
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB

    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        screen_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
    )
    if not bitmap:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)
        raise CaptureError(
            f"Nepodařilo se alokovat bitmapu {width}x{height} px "
            f"(chyba {ctypes.get_last_error()})."
        )

    previous = gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            raise CaptureError(
                "PrintWindow nedokázal vykreslit okno RDP "
                f"(chyba {ctypes.get_last_error()})."
            )
        gdi32.GdiFlush()
        raw = ctypes.string_at(bits, width * height * 4)
        return Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def capture_client(hwnd: int) -> Image.Image:
    """Vrátí jen client rect okna – bez rámu a titulku."""
    window = capture_window(hwnd)
    offset_x, offset_y = client_offset(hwnd)
    _, _, width, height = client_rect(hwnd)
    box = (offset_x, offset_y, offset_x + width, offset_y + height)
    if box[2] > window.width or box[3] > window.height:
        raise CaptureError(
            f"Client rect {width}x{height} px se nevešel do snímku okna "
            f"{window.size} – okno mění velikost."
        )
    return window.crop(box)


def looks_blank(image: Image.Image) -> bool:
    """True pro jednolitý tmavý snímek – typický projev selhání capture.

    Jednolitě bílý snímek za chybu nepovažujeme; prázdná stránka dokumentu je
    legitimní obsah.
    """
    probe = image.convert("L").resize((64, 64), Image.Resampling.BOX)
    low, high = probe.getextrema()
    if high - low > BLANK_SPREAD:
        return False
    histogram = probe.histogram()
    total = sum(histogram) or 1
    mean = sum(index * count for index, count in enumerate(histogram)) / total
    return mean < BLANK_DARK_MEAN


def session_bounds(image: Image.Image, threshold: int = 6) -> tuple[int, int, int, int] | None:
    """Najde plochu vzdálené relace uvnitř client rectu okna.

    Když je okno větší než relace, `mstsc` relaci vycentruje a kolem dokola
    nechá **přesně černé** pruhy. Vrací `(left, top, right, bottom)` plochy
    relace, nebo `None`, když žádné pruhy nejsou (relace okno vyplňuje).
    """
    gray = image.convert("L")
    mask = gray.point(lambda value: 255 if value > threshold else 0)
    box = mask.getbbox()
    if box is None:
        return None
    if box == (0, 0, image.width, image.height):
        return None
    return box


def fit_to_session(hwnd: int, probe: int = 4096, log=None) -> tuple[int, int]:
    """Roztáhne okno RDP tak, aby v něm byla celá plocha vzdálené relace.

    Myší to udělat nejde – okno je potřeba zvětšit i pod dolní okraj monitoru,
    kam se táhnout nedá. Postup:

    1. Okno se přesune do levého horního rohu monitoru a zvětší se na `probe`.
       Když má relace pevné rozlišení, `mstsc` si velikost sám doladí přesně
       na její rozměr a je hotovo.
    2. Když se okno nezmenšilo, relace je menší než okno. Stáhne se tedy na
       velikost monitoru a podle černých pruhů kolem relace se dopočítá její
       skutečný rozměr.

    Vrací výsledný rozměr client rectu.
    """
    if not is_window(hwnd):
        raise CaptureError("Okno RDP relace neexistuje.")

    # Minimalizované okno Windows nevykreslují a zvětšit se také nedá.
    if is_iconic(hwnd):
        wm_restore_window(hwnd)
        wm_wait_for_stable_size(hwnd)
        if is_iconic(hwnd):
            raise CaptureError(
                "Okno RDP relace je minimalizované a nepodařilo se ho obnovit."
            )
        if log:
            log("Okno RDP bylo minimalizované, obnoveno")

    # Maximalizované okno Windows nezvětší – SetWindowPos na něm nic neudělá.
    if wm_is_zoomed(hwnd):
        wm_restore_window(hwnd)
        wm_wait_for_stable_size(hwnd)
        if log:
            log("Okno RDP bylo maximalizované, obnoveno do normální velikosti")

    monitor_x, monitor_y, monitor_w, monitor_h = wm_monitor_rect(hwnd)
    wm_set_geometry(hwnd, monitor_x, monitor_y, probe, probe)
    width, height = wm_wait_for_stable_size(hwnd)
    if log:
        log(f"Okno RDP zvětšeno na {width}x{height} px")

    # Krok 2 – relace je menší než okno, stáhnout na monitor a změřit pruhy
    if width >= probe or height >= probe:
        wm_set_geometry(
            hwnd,
            monitor_x,
            monitor_y,
            min(width, monitor_w),
            min(height, monitor_h),
        )
        wm_wait_for_stable_size(hwnd)
        bounds = session_bounds(capture_client(hwnd))
        if bounds is not None:
            frame_w, frame_h = client_frame(hwnd)
            left, top, right, bottom = bounds
            wm_set_geometry(
                hwnd,
                monitor_x,
                monitor_y,
                (right - left) + frame_w,
                (bottom - top) + frame_h,
            )
            wm_wait_for_stable_size(hwnd)
            if log:
                log(
                    "Relace je menší než okno, okno staženo na její rozměr "
                    f"{right - left}x{bottom - top} px"
                )

    # `mstsc` si okno při doladění velikosti sám přesune – klidně tak, že horní
    # okraj (a s ním panely prohlížeče) skončí nad obrazovkou. Vrátíme ho do
    # rohu monitoru, ať je vidět aspoň vršek relace.
    _, _, final_w, final_h = wm_window_rect(hwnd)
    wm_set_geometry(hwnd, monitor_x, monitor_y, final_w, final_h)

    _, _, client_w, client_h = client_rect(hwnd)
    if log:
        log(f"Plocha relace RDP: {client_w}x{client_h} px")
    return (client_w, client_h)


def client_frame(hwnd: int) -> tuple[int, int]:
    """O kolik je celé okno širší a vyšší než jeho client rect."""
    _, _, window_w, window_h = wm_window_rect(hwnd)
    _, _, client_w, client_h = client_rect(hwnd)
    return (window_w - client_w, window_h - client_h)


class WindowCapturer:
    """Snímá client rect zvoleného okna. Instanci vytvoř ve vlákně, které snímá."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def client_size(self) -> tuple[int, int]:
        _, _, width, height = client_rect(self.hwnd)
        return width, height

    def grab(self, region: Region | None = None) -> Image.Image:
        image = capture_client(self.hwnd)
        if region is not None:
            validate_region(region, image.size)
            image = image.crop(region.as_box())
        if looks_blank(image):
            raise CaptureError(
                "Snímek okna RDP je prázdný (jednolitá tmavá plocha). "
                "Zkontrolujte, že relace není minimalizovaná a že se vykresluje."
            )
        return image

    def close(self) -> None:
        """Kompatibilita s dřívějším rozhraním – držet není co."""

    def __enter__(self) -> "WindowCapturer":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

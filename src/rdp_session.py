"""Založení RDP relace s pevným rozlišením.

Rozlišení relace je jediná věc, která určuje kvalitu výsledku – viz README,
sekce *Rozlišení – jak dostat ostrý obraz*. Zvětšit ho u už běžící relace
nejde: `mstsc` dynamicky vyjednané rozlišení zastropuje velikostí fyzického
monitoru. Relaci je proto potřeba **založit** s vyšším rozlišením, a to jde
jedině přes `.rdp` soubor.

Modul soubor vygeneruje, spustí `mstsc.exe` a počká, až se okno relace objeví.
Přihlášení obstará uživatel – aplikace se hesla ani bezpečnostních dialogů
nedotýká.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Callable

import window_manager as wm

RDP_FILENAME = "rdp_session.rdp"
CREATE_NO_WINDOW = 0x08000000

# Meze rozlišení relace. Spodní hranice je „ještě použitelné okno“, horní
# odpovídá maximu, které novější RDP zvládne; server může vrátit i méně.
MIN_SIDE = 640
MAX_SIDE = 8192

DEFAULT_WIDTH = 2560
DEFAULT_HEIGHT = 3600


class RdpSessionError(RuntimeError):
    pass


def build_rdp_content(host: str, width: int, height: int) -> str:
    """Vrátí obsah `.rdp` souboru pro relaci s pevným rozlišením.

    Podstatné jsou tři volby:

    * `desktopwidth`/`desktopheight` – rozlišení relace,
    * `smart sizing:i:0` – jinak by `mstsc` obraz zmenšoval do okna a získané
      rozlišení by se zahodilo,
    * `dynamic resolution:i:0` – jinak by relace spadla zpátky na velikost
      monitoru, jakmile se změní velikost okna.

    LAN profil a vypnutá komprese drží kodek RDP co nejblíž bezeztrátovému;
    při vyšším rozlišení je to znát na ostrosti písma.
    """
    host = (host or "").strip()
    if not host:
        raise RdpSessionError("Není vyplněna adresa RDP relace.")
    width, height = clamp_resolution(width, height)

    lines = [
        f"full address:s:{host}",
        "screen mode id:i:1",
        f"desktopwidth:i:{width}",
        f"desktopheight:i:{height}",
        "smart sizing:i:0",
        "dynamic resolution:i:0",
        "session bpp:i:32",
        "compression:i:0",
        "connection type:i:6",
        "networkautodetect:i:0",
        "bandwidthautodetect:i:0",
        "audiomode:i:2",
        "redirectclipboard:i:1",
        "redirectprinters:i:1",
        "autoreconnection enabled:i:1",
        "authentication level:i:2",
        "negotiate security layer:i:1",
    ]
    return "\r\n".join(lines) + "\r\n"


def clamp_resolution(width: int, height: int) -> tuple[int, int]:
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return (DEFAULT_WIDTH, DEFAULT_HEIGHT)
    width = max(MIN_SIDE, min(MAX_SIDE, width))
    height = max(MIN_SIDE, min(MAX_SIDE, height))
    return (width, height)


def write_rdp_file(host: str, width: int, height: int, directory: str) -> str:
    """Zapíše `.rdp` soubor do zvoleného adresáře a vrátí jeho cestu."""
    content = build_rdp_content(host, width, height)
    path = os.path.join(directory, RDP_FILENAME)
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except OSError as exc:
        raise RdpSessionError(f"Soubor {RDP_FILENAME} nelze zapsat: {exc}") from exc
    return path


def mstsc_path() -> str:
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "mstsc.exe"
    )


def launch(rdp_path: str) -> None:
    """Spustí `mstsc.exe` nad připraveným `.rdp`.

    Přihlašovací dialog i případné bezpečnostní upozornění řeší uživatel;
    aplikace do nich nezasahuje.
    """
    exe = mstsc_path()
    if not os.path.isfile(exe):
        raise RdpSessionError(f"Nenalezen klient vzdálené plochy: {exe}")
    try:
        subprocess.Popen([exe, rdp_path], creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        raise RdpSessionError(f"Klienta vzdálené plochy nelze spustit: {exc}") from exc


def find_session(host: str) -> wm.WindowInfo | None:
    """Vrátí okno relace k zadané adrese, pokud už existuje."""
    matching, _all_rdp = wm.find_rdp(host)
    return matching[0] if matching else None


def wait_for_session(
    host: str,
    timeout_s: float = 120.0,
    poll_s: float = 0.5,
    is_cancelled: Callable[[], bool] | None = None,
) -> wm.WindowInfo | None:
    """Počká, až se objeví okno relace k dané adrese.

    Timeout je štědrý – uživatel mezitím zadává heslo a potvrzuje případné
    bezpečnostní upozornění.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_cancelled is not None and is_cancelled():
            return None
        window = find_session(host)
        if window is not None and not wm.is_iconic(window.hwnd):
            return window
        time.sleep(poll_s)
    return None

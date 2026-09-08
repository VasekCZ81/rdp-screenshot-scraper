"""Konfigurace a cesty aplikace RDP Screenshot Scraper.

Pracovní adresář = adresář, ve kterém se nachází spuštěný .exe (frozen build)
nebo kořen projektu (běh ze zdrojových kódů, kdy main.py leží v podadresáři src).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime

APP_NAME = "RDP Screenshot Scraper"
CONFIG_FILENAME = "config.json"
CAPTURES_DIRNAME = "captures"
LOG_FILENAME = "scraper.log"
DUPLICATES_DIRNAME = "duplicates"
PAGE_PREFIX = "page_"
PAGE_DIGITS = 4


def get_working_dir() -> str:
    """Vrátí pracovní adresář aplikace."""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile/onedir – adresář vedle .exe
        return os.path.dirname(os.path.abspath(sys.executable))

    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Při běhu ze zdrojáků leží kód v ./src – výstupy patří do kořene projektu.
    if os.path.basename(script_dir).lower() == "src":
        return os.path.dirname(script_dir)
    return script_dir


def get_captures_root() -> str:
    return os.path.join(get_working_dir(), CAPTURES_DIRNAME)


def new_session_dir() -> str:
    """Vytvoří a vrátí adresář pro jeden běh: captures/RRRR-MM-DD_HHMMSS."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = os.path.join(get_captures_root(), stamp)
    path = base
    counter = 1
    while os.path.exists(path):
        path = f"{base}_{counter}"
        counter += 1
    os.makedirs(path, exist_ok=True)
    return path


def check_working_dir_writable() -> None:
    """Ověří, že lze zapisovat do pracovního adresáře. Vyhodí OSError."""
    root = get_captures_root()
    os.makedirs(root, exist_ok=True)
    probe = os.path.join(root, ".write_test")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("ok")
    os.remove(probe)


@dataclass
class AppConfig:
    """Uživatelsky nastavitelné parametry automatizace."""

    # --- časování ---
    activation_delay_ms: int = 300      # čekání po aktivaci okna
    page_down_delay_ms: int = 700       # čekání na překreslení po Page Down
    activation_attempts: int = 3        # počet pokusů o aktivaci okna
    activation_retry_ms: int = 400      # pauza mezi pokusy o aktivaci

    # --- detekce konce / limity ---
    end_confirmations: int = 2          # kolik po sobě jdoucích shodných snímků = konec
    max_screenshots: int = 1000         # ochrana proti nekonečné smyčce

    # --- citlivost porovnání obrazu ---
    # Porovnává se v plném rozlišení – viz image_compare.py.
    hash_threshold: int = 4             # max. Hammingova vzdálenost dHash (0-64)
    pixel_threshold: float = 0.01       # max. normalizovaný průměrný rozdíl pixelů
    changed_threshold: float = 0.005    # max. podíl výrazně změněných pixelů

    # --- ostatní ---
    # Adresa RDP relace není předvyplněná – uživatel ji musí zadat v Nastavení
    # (uloží se do config.json). Bez ní se snímání nespustí.
    rdp_host: str = ""
    page_down_method: str = "sendinput"  # "sendinput" | "postmessage"
    pdf_dpi: int = 96                    # DPI použité pro velikost stránky PDF
    save_duplicates: bool = True         # ukládat potvrzovací duplicity do duplicates/

    # --- OCR (vestavěný engine Windows) ---
    ocr_enabled: bool = True             # vložit do PDF neviditelnou textovou vrstvu
    ocr_language: str = "cs"             # jazyková značka, např. "cs" nebo "en-GB"

    # ------------------------------------------------------------------
    def clamp(self) -> None:
        """Ošetří nesmyslné hodnoty zadané uživatelem."""
        self.activation_delay_ms = max(0, min(10000, int(self.activation_delay_ms)))
        self.page_down_delay_ms = max(0, min(30000, int(self.page_down_delay_ms)))
        self.activation_attempts = max(1, min(20, int(self.activation_attempts)))
        self.activation_retry_ms = max(0, min(5000, int(self.activation_retry_ms)))
        self.end_confirmations = max(1, min(10, int(self.end_confirmations)))
        self.max_screenshots = max(1, min(100000, int(self.max_screenshots)))
        self.hash_threshold = max(0, min(64, int(self.hash_threshold)))
        self.pixel_threshold = max(0.0, min(1.0, float(self.pixel_threshold)))
        self.changed_threshold = max(0.0, min(1.0, float(self.changed_threshold)))
        self.pdf_dpi = max(1, min(1200, int(self.pdf_dpi)))
        if self.page_down_method not in ("sendinput", "postmessage"):
            self.page_down_method = "sendinput"
        # Prázdná adresa je platný stav – znamená „uživatel ještě nezadal“.
        self.rdp_host = str(self.rdp_host).strip()
        self.ocr_enabled = bool(self.ocr_enabled)
        self.ocr_language = str(self.ocr_language).strip() or "cs"

    # ------------------------------------------------------------------
    @classmethod
    def config_path(cls) -> str:
        return os.path.join(get_working_dir(), CONFIG_FILENAME)

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        path = cls.config_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cfg
        known = {f.name for f in fields(cls)}
        for key, value in (data or {}).items():
            if key in known:
                setattr(cfg, key, value)
        try:
            cfg.clamp()
        except (TypeError, ValueError):
            cfg = cls()
        return cfg

    def save(self) -> bool:
        """Uloží konfiguraci. Selhání zápisu není fatální."""
        try:
            with open(self.config_path(), "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2, ensure_ascii=False)
            return True
        except OSError:
            return False


def page_filename(index: int) -> str:
    """page_0001.png – pevné doplnění nulami zachová pořadí."""
    return f"{PAGE_PREFIX}{index:0{PAGE_DIGITS}d}.png"

"""Konfigurace a cesty aplikace RDP Screenshot Scraper.

Pracovní adresář = adresář, ve kterém se nachází spuštěný .exe (frozen build)
nebo kořen projektu (běh ze zdrojových kódů, kdy main.py leží v podadresáři src).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

APP_NAME = "RDP Screenshot Scraper"
CONFIG_FILENAME = "config.json"
CAPTURES_DIRNAME = "captures"
LOG_FILENAME = "scraper.log"
DUPLICATES_DIRNAME = "duplicates"
MASKED_DIRNAME = "masked"
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

    # --- rozlišení zakládané RDP relace ---
    # Určuje kvalitu výsledku: strop DPI = výška relace / 11,69" pro A4.
    # U už běžící relace ho zvětšit nelze, proto se relace zakládá z .rdp.
    rdp_width: int = 2560
    rdp_height: int = 3600

    pdf_dpi: int = 96                    # DPI použité pro velikost stránky PDF
    # Fyzická šířka snímané předlohy v mm. Kladná hodnota má přednost před
    # pdf_dpi – rozlišení stránky se dopočítá ze šířky snímku (A4 = 210).
    pdf_page_width_mm: float = 0.0
    # Zvětšení snímku před vložením do PDF (1.0 = beze změny). Fyzická velikost
    # stránky zůstává stejná, jen se do ní vloží víc vzorků.
    pdf_upscale: float = 1.0
    # Doostření (unsharp mask) v procentech; 0 = vypnuto, rozumně 80-150.
    pdf_sharpen: float = 0.0
    save_duplicates: bool = True         # ukládat potvrzovací duplicity do duplicates/

    # --- snímaná oblast ---
    # [x, y, šířka, výška] v pixelech client rectu okna RDP. Souřadnice jsou
    # vázané na okno, ne na plochu, takže platí i po dalším spuštění.
    region: list = field(default_factory=list)

    # --- vymazání oblasti ze všech stránek ---
    # Obdélníky [x, y, šířka, výška] v pixelech snímané oblasti.
    mask_rects: list = field(default_factory=list)

    # --- OCR (vestavěný engine Windows) ---
    ocr_enabled: bool = True             # vložit do PDF neviditelnou textovou vrstvu
    ocr_language: str = "cs"             # jazyková značka, např. "cs" nebo "en-GB"
    ocr_upscale: float = 2.0             # zvětšení snímku před OCR (1.0 = vypnuto)

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
        # 0 = vypnuto; horní mez pokrývá i velké formáty (A0 má 841 mm).
        self.pdf_page_width_mm = max(0.0, min(2000.0, float(self.pdf_page_width_mm)))
        if self.page_down_method not in ("sendinput", "postmessage"):
            self.page_down_method = "sendinput"
        import rdp_session as rdp_mod

        self.rdp_width, self.rdp_height = rdp_mod.clamp_resolution(
            self.rdp_width, self.rdp_height
        )
        # Prázdná adresa je platný stav – znamená „uživatel ještě nezadal“.
        self.rdp_host = str(self.rdp_host).strip()
        import mask as mask_mod

        self.mask_rects = mask_mod.rects_to_config(
            mask_mod.normalize_rects(self.mask_rects)
        )
        self.region = _clean_region(self.region)
        self.ocr_enabled = bool(self.ocr_enabled)
        self.ocr_language = str(self.ocr_language).strip() or "cs"
        # Nad 4× už jen roste čas a velikost, kvalita ne.
        self.ocr_upscale = max(1.0, min(4.0, float(self.ocr_upscale)))
        self.pdf_upscale = max(1.0, min(4.0, float(self.pdf_upscale)))
        self.pdf_sharpen = max(0.0, min(300.0, float(self.pdf_sharpen)))

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


def _clean_region(value) -> list:
    """Ověří uloženou oblast. Cokoli nesmyslného zahodí (= nevybráno)."""
    try:
        x, y, width, height = (int(v) for v in value)
    except (TypeError, ValueError):
        return []
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        return []
    return [x, y, width, height]


def page_filename(index: int) -> str:
    """page_0001.png – pevné doplnění nulami zachová pořadí."""
    return f"{PAGE_PREFIX}{index:0{PAGE_DIGITS}d}.png"

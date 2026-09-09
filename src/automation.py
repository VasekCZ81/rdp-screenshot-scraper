"""Řídicí logika automatického snímání.

Celý cyklus běží v samostatném vlákně, aby GUI zůstalo responzivní.

NEPŘEKROČITELNÉ PRAVIDLO
------------------------
Screenshot vzniká výhradně v metodě `_capture_with_total_commander()`, která
nejprve aktivuje Total Commander a ověří přes GetForegroundWindow(), že je
skutečně aktivním oknem. Pokud ověření selže, screenshot se NEPOŘÍDÍ a
automatizace se zastaví. Nikde jinde v kódu se snímek nepořizuje.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import config as cfg_mod
import mask as mask_mod
import ocr
import stitch
import window_manager as wm
from capture import CaptureError, Region, ScreenCapturer, validate_region
from config import AppConfig
from image_compare import compare
from pdf_export import PdfExportError, images_to_pdf


class Status(str, Enum):
    READY = "Připraveno"
    SELECTING = "Výběr oblasti"
    RUNNING = "Probíhá snímání"
    PAUSED = "Pozastaveno"
    END_DETECTED = "Detekován konec dokumentu"
    MASKING = "Vymazávám oblast"
    STITCHING = "Skládám snímky"
    OCR = "Provádím OCR"
    MAKING_PDF = "Vytvářím PDF"
    DONE = "Dokončeno"
    STOPPED = "Snímání ukončeno"
    ERROR = "Chyba"


@dataclass
class RunTargets:
    """Okna a oblast, se kterými poběží jeden běh automatizace."""

    tc_hwnd: int
    tc_label: str
    rdp_hwnd: int
    rdp_label: str
    region: Region


def capture_verified(
    tc_hwnd: int,
    region: Region,
    capturer: ScreenCapturer,
    config: AppConfig,
    log: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
):
    """JEDINÉ místo v aplikaci, kde vzniká screenshot.

    Aktivuje Total Commander, ověří přes `GetForegroundWindow()`, že je
    skutečně aktivním oknem, a teprve pak snímá. Když ověření neprojde,
    snímek NEVZNIKNE a vyhodí se `AutomationError`.
    """
    if not wm.is_window(tc_hwnd):
        raise AutomationError("Okno Total Commanderu bylo zavřeno.")

    if not wm.ensure_foreground(
        tc_hwnd,
        activation_delay_ms=config.activation_delay_ms,
        attempts=config.activation_attempts,
        retry_ms=config.activation_retry_ms,
        log=warn or log,
    ):
        raise AutomationError(
            "Nepodařilo se aktivovat Total Commander – screenshot nebyl pořízen."
        )
    if log:
        log(f"Total Commander aktivován (HWND {tc_hwnd})")

    # Poslední kontrola těsně před snímkem. Bez ní se nesnímá.
    if not wm.is_foreground(tc_hwnd):
        raise AutomationError(
            "Total Commander přestal být aktivním oknem – screenshot nebyl pořízen."
        )
    return capturer.grab(region)


def capture_single(
    config: AppConfig,
    tc_hwnd: int,
    region: Region,
    log: Callable[[str], None] | None = None,
):
    """Pořídí jeden ověřený snímek – pro náhled a výběr oblasti k vymazání."""
    validate_region(region)
    capturer = ScreenCapturer()
    try:
        return capture_verified(tc_hwnd, region, capturer, config, log=log)
    finally:
        capturer.close()


def mask_captures(
    config: AppConfig,
    page_files: list[str],
    session_dir: str,
    status: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Vymaže zvolené oblasti ze všech snímků. Vrací cesty pro další zpracování.

    Uplatňuje se před skládáním i před OCR: souřadnice masky platí v rámci
    snímané oblasti a vymazaný text se tak nedostane ani do textové vrstvy.
    Při jakémkoli selhání se vrátí původní snímky, aby PDF vzniklo jako dosud.
    """
    rects = mask_mod.normalize_rects(config.mask_rects)
    if not rects or not page_files:
        return page_files

    out_dir = os.path.join(session_dir, cfg_mod.MASKED_DIRNAME)
    try:
        if status:
            status(Status.MASKING.value)
        return mask_mod.apply_masks(page_files, rects, out_dir, log=log)
    except (mask_mod.MaskError, OSError, ValueError) as exc:
        if log:
            log(f"Vymazání oblasti selhalo, snímky zůstávají beze změny: {exc}")
        return page_files


def stitch_captures(
    config: AppConfig,
    page_files: list[str],
    session_dir: str,
    status: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Složí překrývající se snímky do stránek. Vrací cesty, které mají jít do PDF.

    Pořízené snímky zůstávají nedotčené – složené stránky vznikají vedle nich
    v podadresáři `stitched/`. Když skládání selže, vrátí se původní snímky,
    takže se PDF vytvoří tak jako dosud.
    """
    if not config.stitch_enabled or len(page_files) < 2:
        return page_files

    out_dir = os.path.join(session_dir, cfg_mod.STITCHED_DIRNAME)
    try:
        if status:
            status(Status.STITCHING.value)
        pages = stitch.stitch_pages(
            page_files,
            out_dir,
            page_height=config.stitch_page_height_px,
            log=log,
        )
    except (stitch.StitchError, OSError, ValueError) as exc:
        if log:
            log(f"Skládání selhalo, PDF vznikne z původních snímků: {exc}")
        return page_files

    if not pages:
        if log:
            log("Skládání nevrátilo žádnou stránku, používám původní snímky.")
        return page_files
    if log:
        log(f"Skládání: {len(page_files)} snímků složeno do {len(pages)} stránek")
    return pages


def ocr_pages(
    config: AppConfig,
    page_files: list[str],
    status: Callable[[str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> list | None:
    """Rozpozná text ve stránkách pro neviditelnou textovou vrstvu PDF.

    Vrací `None`, pokud je OCR vypnuté nebo nedostupné. Selhání OCR nikdy
    nesmí zabránit vytvoření PDF – v takovém případě vznikne PDF bez textu.
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    if not config.ocr_enabled or not page_files:
        return None

    try:
        if not ocr.is_available(config.ocr_language):
            available = ocr.available_languages()
            _log(
                f"OCR pro jazyk „{config.ocr_language}“ není ve Windows k dispozici "
                f"(dostupné: {', '.join(available) or 'žádné'}). "
                "PDF vznikne bez textové vrstvy."
            )
            return None

        if status:
            status(Status.OCR.value)

        def on_progress(index: int, total: int) -> None:
            if status:
                status(f"{Status.OCR.value} ({index}/{total})")

        layers = ocr.recognize(
            page_files,
            config.ocr_language,
            on_progress=on_progress,
            log=_log,
            scale=config.ocr_upscale,
        )
    except ocr.OcrError as exc:
        _log(f"OCR selhalo, PDF vznikne bez textové vrstvy: {exc}")
        return None

    recognized = sum(1 for layer in layers if layer is not None)
    words = sum(len(layer.words) for layer in layers if layer is not None)
    _log(f"OCR hotovo: {words} slov na {recognized} z {len(page_files)} stránek")
    return layers


class AutomationController:
    """Stavový automat snímacího cyklu."""

    def __init__(
        self,
        config: AppConfig,
        targets: RunTargets,
        session_dir: str,
        emit: Callable[[str, object], None],
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.targets = targets
        self.session_dir = session_dir
        self.emit = emit
        self.log = logger

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()  # nastaveno = běží; zrušeno = pozastaveno

        self.page_files: list[str] = []
        self.duplicate_files: list[str] = []
        self.captured_total = 0
        self.finished_reason = ""

    # ------------------------------------------------------------------
    # Ovládání
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._resume.set()
        self._thread = threading.Thread(target=self._run, name="scraper", daemon=True)
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self) -> None:
        if self.is_running() and not self.is_paused():
            self._resume.clear()
            self.log.info("Uživatel požádal o pozastavení")

    def resume(self) -> None:
        if self.is_running() and self.is_paused():
            self._resume.set()
            self.log.info("Pokračování ve snímání")
            self._status(Status.RUNNING)

    def stop(self, reason: str = "Ukončeno uživatelem") -> None:
        if self._stop.is_set():
            return
        self.finished_reason = self.finished_reason or reason
        self._stop.set()
        self._resume.set()  # odblokuje případnou pauzu, aby vlákno doběhlo
        self.log.info("Požadavek na ukončení: %s", reason)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------------
    # Pomocné
    # ------------------------------------------------------------------
    def _status(self, status: Status, detail: str = "") -> None:
        self.emit("status", status.value + (f" – {detail}" if detail else ""))

    def _emit_counts(self) -> None:
        self.emit(
            "counts",
            {"captured": self.captured_total, "pages": len(self.page_files)},
        )

    def _log(self, message: str, level: int = logging.INFO) -> None:
        self.log.log(level, message)
        self.emit("log", message)

    def _sleep(self, seconds: float) -> None:
        """Čekání přerušitelné požadavkem na ukončení."""
        if seconds <= 0:
            return
        self._stop.wait(seconds)

    def _wait_if_paused(self) -> None:
        if self._resume.is_set():
            return
        self._status(Status.PAUSED)
        self._log("Snímání pozastaveno")
        while not self._resume.wait(0.15):
            if self._stop.is_set():
                return

    def _should_continue(self) -> bool:
        return not self._stop.is_set()

    # ------------------------------------------------------------------
    # Kritická sekce – aktivace + ověření + snímek
    # ------------------------------------------------------------------
    def _activate_rdp(self) -> bool:
        hwnd = self.targets.rdp_hwnd
        if not wm.is_window(hwnd):
            raise AutomationError("Okno RDP relace bylo zavřeno.")
        ok = wm.ensure_foreground(
            hwnd,
            activation_delay_ms=self.config.activation_delay_ms,
            attempts=self.config.activation_attempts,
            retry_ms=self.config.activation_retry_ms,
            log=lambda m: self.log.warning(m),
        )
        if ok:
            self._log(f"RDP aktivováno (HWND {hwnd})")
        return ok

    def _capture_with_total_commander(self, capturer: ScreenCapturer):
        """Snímek přes společnou `capture_verified()` – jediné místo s tímto právem."""
        image = capture_verified(
            self.targets.tc_hwnd,
            self.targets.region,
            capturer,
            self.config,
            log=self._log,
            warn=lambda m: self.log.warning(m),
        )
        self.captured_total += 1
        return image

    # ------------------------------------------------------------------
    # Hlavní smyčka
    # ------------------------------------------------------------------
    def _run(self) -> None:
        capturer: ScreenCapturer | None = None
        try:
            validate_region(self.targets.region)
            self._log(f"Start snímání, oblast: {self.targets.region}")
            self._log(f"Total Commander: {self.targets.tc_label}")
            self._log(f"RDP: {self.targets.rdp_label}")
            self._log(
                "Nastavení: aktivace {a} ms, Page Down {p} ms, potvrzení konce {c}, "
                "max snímků {m}, metoda kláves {k}".format(
                    a=self.config.activation_delay_ms,
                    p=self.config.page_down_delay_ms,
                    c=self.config.end_confirmations,
                    m=self.config.max_screenshots,
                    k=self.config.page_down_method,
                )
            )
            self._status(Status.RUNNING)

            # mss musí vzniknout ve vlákně, které snímá
            capturer = ScreenCapturer()
            self._loop(capturer)

        except AutomationError as exc:
            self._fail(str(exc))
        except CaptureError as exc:
            self._fail(str(exc))
        except Exception as exc:  # poslední záchranná síť – data se nesmí ztratit
            self.log.exception("Neočekávaná chyba")
            self._fail(f"Neočekávaná chyba: {exc}")
        else:
            # Výjimka v else větvi by unikla obsluze výše a vlákno by skončilo
            # bez události 'finished' – GUI by zůstalo viset na „Vytvářím PDF“.
            try:
                self._finish_ok()
            except Exception as exc:  # noqa: BLE001 – poslední záchranná síť
                self.log.exception("Chyba při dokončování")
                self._fail(f"Chyba při dokončování: {exc}")
        finally:
            if capturer is not None:
                capturer.close()

    def _loop(self, capturer: ScreenCapturer) -> None:
        cfg = self.config

        # Krok 0 – výchozí pozice dokumentu (aktivace TC + ověření + snímek)
        previous = self._capture_with_total_commander(capturer)
        self._save_page(previous)
        self._emit_counts()

        same_in_row = 0

        while self._should_continue():
            self._wait_if_paused()
            if not self._should_continue():
                break

            if self.captured_total >= cfg.max_screenshots:
                self.finished_reason = (
                    f"Dosažen maximální počet snímků ({cfg.max_screenshots})."
                )
                self._log(self.finished_reason, logging.WARNING)
                self.emit("limit", self.finished_reason)
                break

            # Krok 1 – RDP do popředí a ověření
            if not self._activate_rdp():
                raise AutomationError(
                    "Nepodařilo se aktivovat okno RDP – Page Down nebyl odeslán."
                )

            # Krok 2 – Page Down (jen s ověřeným foreground RDP)
            if not wm.is_foreground(self.targets.rdp_hwnd):
                raise AutomationError(
                    "RDP přestalo být aktivním oknem – Page Down nebyl odeslán."
                )
            try:
                wm.send_page_down(self.targets.rdp_hwnd, cfg.page_down_method)
            except OSError as exc:
                raise AutomationError(f"Odeslání Page Down selhalo: {exc}") from exc
            self._log("Page Down")

            # Krok 3 – čekání na překreslení obsahu
            self._sleep(cfg.page_down_delay_ms / 1000.0)
            if not self._should_continue():
                break

            # Krok 4 – zpět na Total Commander, ověřit a teprve pak snímat
            current = self._capture_with_total_commander(capturer)

            # Krok 5 – porovnání s předchozím snímkem
            result = compare(
                previous,
                current,
                hash_threshold=cfg.hash_threshold,
                pixel_threshold=cfg.pixel_threshold,
                changed_threshold=cfg.changed_threshold,
            )

            if result.identical:
                same_in_row += 1
                self._log(
                    f"Snímek beze změny ({result.describe()}), "
                    f"potvrzení {same_in_row}/{cfg.end_confirmations}"
                )
                if cfg.save_duplicates:
                    self._save_duplicate(current)
                if same_in_row >= cfg.end_confirmations:
                    self.finished_reason = "Detekován konec dokumentu."
                    self._status(Status.END_DETECTED)
                    self._log(self.finished_reason)
                    break
            else:
                same_in_row = 0
                self._save_page(current)
                self._log(f"Nová pozice dokumentu ({result.describe()})")

            previous = current
            self._emit_counts()

        self._emit_counts()

    # ------------------------------------------------------------------
    # Ukládání
    # ------------------------------------------------------------------
    def _save_page(self, image) -> str:
        index = len(self.page_files) + 1
        path = os.path.join(self.session_dir, cfg_mod.page_filename(index))
        image.save(path, "PNG")
        self.page_files.append(path)
        self._log(f"Screenshot {os.path.basename(path)}")
        return path

    def _save_duplicate(self, image) -> None:
        """Potvrzovací duplicity ukládáme mimo hlavní řadu – do PDF se nedostanou."""
        folder = os.path.join(self.session_dir, cfg_mod.DUPLICATES_DIRNAME)
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(
                folder, f"dup_{len(self.duplicate_files) + 1:0{cfg_mod.PAGE_DIGITS}d}.png"
            )
            image.save(path, "PNG")
            self.duplicate_files.append(path)
        except OSError as exc:
            self.log.warning("Duplicitní snímek se nepodařilo uložit: %s", exc)

    # ------------------------------------------------------------------
    # Dokončení
    # ------------------------------------------------------------------
    def _finish_ok(self) -> None:
        reason = self.finished_reason or "Snímání dokončeno."
        self._log(f"Konec snímání: {reason}")
        pdf_path = None
        error = None
        if self.page_files:
            try:
                self._status(Status.MAKING_PDF)
                pdf_path = self.build_pdf()
            except PdfExportError as exc:
                error = str(exc)
                self.log.error("PDF: %s", exc)
        else:
            error = "Nebyl pořízen žádný snímek."

        if error:
            self._status(Status.ERROR, error)
            self.emit("error", error)
        else:
            self._status(Status.DONE)
        self.emit(
            "finished",
            {
                "reason": reason,
                "pdf": pdf_path,
                "pages": len(self.page_files),
                "captured": self.captured_total,
                "session_dir": self.session_dir,
                "error": error,
            },
        )

    def _fail(self, message: str) -> None:
        self.finished_reason = message
        self.log.error(message)
        self._status(Status.ERROR, message)
        self.emit("log", "CHYBA: " + message)
        self.emit("error", message)
        self.emit(
            "finished",
            {
                "reason": message,
                "pdf": None,
                "pages": len(self.page_files),
                "captured": self.captured_total,
                "session_dir": self.session_dir,
                "error": message,
            },
        )

    def build_pdf(self) -> str:
        """Sestaví PDF z dosud uložených unikátních stránek."""
        if not self.page_files:
            raise PdfExportError("Nejsou k dispozici žádné snímky pro vytvoření PDF.")
        name = os.path.basename(self.session_dir.rstrip("\\/"))
        pdf_path = os.path.join(self.session_dir, f"RDP_capture_{name}.pdf")

        pages = mask_captures(
            self.config,
            self.page_files,
            self.session_dir,
            status=lambda text: self.emit("status", text),
            log=self._log,
        )

        pages = stitch_captures(
            self.config,
            pages,
            self.session_dir,
            status=lambda text: self.emit("status", text),
            log=self._log,
        )

        layers = ocr_pages(
            self.config,
            pages,
            status=lambda text: self.emit("status", text),
            log=self._log,
        )

        self._status(Status.MAKING_PDF)
        self._log(f"Vytvářím PDF z {len(pages)} stránek")
        images_to_pdf(
            pages,
            pdf_path,
            dpi=self.config.pdf_dpi,
            text_layers=layers,
            log=lambda m: self.log.info(m),
            page_width_mm=self.config.pdf_page_width_mm or None,
            upscale=self.config.pdf_upscale,
            sharpen=self.config.pdf_sharpen,
        )
        self._log(f"PDF vytvořeno: {pdf_path}")
        return pdf_path


class AutomationError(RuntimeError):
    """Chyba, po které je nutné automatizaci bezpečně zastavit."""


def setup_session_logger(session_dir: str) -> logging.Logger:
    """Logger zapisující do captures/<relace>/scraper.log."""
    logger = logging.getLogger(f"scraper.{os.path.basename(session_dir)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    path = os.path.join(session_dir, cfg_mod.LOG_FILENAME)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

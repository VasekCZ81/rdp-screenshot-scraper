"""GUI aplikace RDP Screenshot Scraper (tkinter)."""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import config as cfg_mod
import hotkey as hotkey_mod
import mask as mask_mod
import ocr
import rdp_session as rdp_mod
import window_manager as wm
import automation
from automation import (
    AutomationController,
    AutomationError,
    RunTargets,
    Status,
    close_logger,
    ocr_pages,
    mask_captures,
    setup_session_logger,
)
from PIL import Image

from capture import CaptureError, Region, fit_to_session, validate_region
from config import APP_NAME, AppConfig
import pdf_export as pdf_mod
from pdf_export import PdfExportError, dpi_for_width, images_to_pdf

# Popisky režimů komprese – v config.json se ukládá technická hodnota.
COMPRESSION_LABELS = {
    pdf_mod.COMPRESSION_LOSSLESS: "Bezeztrátová paleta (doporučeno)",
    pdf_mod.COMPRESSION_NONE: "Žádná – plné barvy",
    pdf_mod.COMPRESSION_BILEVEL: "Černobílá CCITT G4 (ztrátové)",
}
COMPRESSION_VALUES = {label: key for key, label in COMPRESSION_LABELS.items()}

PAD = 8
MAX_LOG_LINES = 400
TITLE_LIMIT = 44


def _short(text: str, limit: int = TITLE_LIMIT) -> str:
    """Zkrátí titulek okna, aby se vešel do popisku vedle tlačítka."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class WindowPicker(tk.Toplevel):
    """Jednoduchý výběr okna ze seznamu kandidátů."""

    def __init__(self, master: tk.Misc, title: str, windows: list[wm.WindowInfo]) -> None:
        super().__init__(master)
        self.title(title)
        self.resizable(True, False)
        self.result: wm.WindowInfo | None = None
        self._windows = windows

        ttk.Label(self, text="Vyberte správné okno:").pack(
            anchor="w", padx=PAD, pady=(PAD, 2)
        )
        self.listbox = tk.Listbox(self, width=90, height=min(10, max(3, len(windows))))
        for win in windows:
            self.listbox.insert("end", win.label())
        self.listbox.selection_set(0)
        self.listbox.pack(fill="both", expand=True, padx=PAD)

        row = ttk.Frame(self)
        row.pack(fill="x", padx=PAD, pady=PAD)
        ttk.Button(row, text="Zrušit", command=self._cancel).pack(side="right")
        ttk.Button(row, text="Použít", command=self._ok).pack(side="right", padx=(0, 6))

        self.listbox.bind("<Double-Button-1>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.transient(master)
        self.grab_set()
        self.listbox.focus_set()

    def _ok(self) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.result = self._windows[selection[0]]
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class MaskDialog(tk.Toplevel):
    """Vyznačení obdélníků myší nad zmenšeným náhledem snímku.

    Používá se dvakrát:
      * `single=False` – oblasti, které se vymažou ze všech stránek,
      * `single=True`  – snímaná oblast v rámci okna RDP.

    V obou případech jsou výsledné souřadnice pixely předloženého obrázku,
    takže volající ví, k čemu se vztahují. Kreslí se do obrazu okna, ne do
    plochy monitoru – označit tak lze i tu část relace, která leží mimo
    obrazovku.
    """

    MAX_WIDTH = 1100
    MAX_HEIGHT = 760

    DEFAULT_TITLE = "Oblast k vymazání ze všech stránek"
    DEFAULT_PROMPT = (
        "Tažením myši označte oblast, která se má na všech stránkách "
        "nahradit bílou plochou.\nMůžete označit i více oblastí."
    )

    def __init__(
        self,
        master: tk.Misc,
        image,
        rects: list[mask_mod.MaskRect],
        title: str | None = None,
        prompt: str | None = None,
        single: bool = False,
    ) -> None:
        super().__init__(master)
        self.title(title or self.DEFAULT_TITLE)
        self.resizable(False, False)
        self.result: list[mask_mod.MaskRect] | None = None
        self._single = single
        self._prompt = prompt or self.DEFAULT_PROMPT
        self._rects = list(rects)
        self._image_size = image.size
        self._start: tuple[int, int] | None = None
        self._drag_id: int | None = None

        width, height = image.size
        self._scale = min(1.0, self.MAX_WIDTH / width, self.MAX_HEIGHT / height)
        view = image
        if self._scale < 1.0:
            view = image.resize(
                (max(1, int(width * self._scale)), max(1, int(height * self._scale))),
                Image.Resampling.LANCZOS,
            )

        # Tk 8.6 umí PNG načíst přímo, takže není potřeba PIL.ImageTk.
        self._tempdir = tempfile.TemporaryDirectory(prefix="rdpscraper_mask_")
        preview_path = os.path.join(self._tempdir.name, "preview.png")
        view.save(preview_path, "PNG")
        self._photo = tk.PhotoImage(file=preview_path)

        ttk.Label(self, text=self._prompt, justify="left").pack(
            anchor="w", padx=PAD, pady=(PAD, 4)
        )

        self.canvas = tk.Canvas(
            self,
            width=self._photo.width(),
            height=self._photo.height(),
            highlightthickness=1,
            highlightbackground="#808080",
            cursor="crosshair",
        )
        self.canvas.pack(padx=PAD)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda _e: self._cancel())

        self.var_info = tk.StringVar()
        ttk.Label(self, textvariable=self.var_info, foreground="#555555").pack(
            anchor="w", padx=PAD, pady=(4, 0)
        )

        row = ttk.Frame(self)
        row.pack(fill="x", padx=PAD, pady=PAD)
        ttk.Button(row, text="Použít", command=self._ok).pack(side="right")
        ttk.Button(row, text="Zrušit", command=self._cancel).pack(side="right", padx=(0, 6))
        ttk.Button(row, text="Smazat vše", command=self._clear).pack(side="left")
        ttk.Button(row, text="Zpět", command=self._undo).pack(side="left", padx=(6, 0))

        self._redraw()
        self.transient(master)
        self.grab_set()

    # ------------------------------------------------------------------
    def _to_image(self, x: int, y: int) -> tuple[int, int]:
        return int(round(x / self._scale)), int(round(y / self._scale))

    def _to_view(self, x: int, y: int) -> tuple[float, float]:
        return x * self._scale, y * self._scale

    def _redraw(self) -> None:
        self.canvas.delete("mask")
        for rect in self._rects:
            left, top = self._to_view(rect.x, rect.y)
            right, bottom = self._to_view(rect.x + rect.width, rect.y + rect.height)
            self.canvas.create_rectangle(
                left, top, right, bottom,
                fill="white", stipple="gray50", outline="#D00000", width=2,
                tags="mask",
            )
        width, height = self._image_size
        if self._single:
            chosen = str(self._rects[0]) if self._rects else "nevybráno"
            self.var_info.set(
                f"Okno RDP {width}x{height} px, náhled {self._scale * 100:.0f} %"
                f"   |   oblast: {chosen}"
            )
        else:
            self.var_info.set(
                f"Snímek {width}x{height} px, náhled {self._scale * 100:.0f} %   |   "
                f"označených oblastí: {len(self._rects)}"
            )

    def _on_press(self, event: tk.Event) -> None:
        self._start = (event.x, event.y)
        if self._drag_id is not None:
            self.canvas.delete(self._drag_id)
        self._drag_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#D00000", width=2, dash=(4, 3)
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self._start is None or self._drag_id is None:
            return
        self.canvas.coords(self._drag_id, self._start[0], self._start[1], event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if self._start is None:
            return
        if self._drag_id is not None:
            self.canvas.delete(self._drag_id)
            self._drag_id = None
        left, right = sorted((self._start[0], event.x))
        top, bottom = sorted((self._start[1], event.y))
        self._start = None
        if right - left < 3 or bottom - top < 3:
            return
        x0, y0 = self._to_image(left, top)
        x1, y1 = self._to_image(right, bottom)
        rect = mask_mod.MaskRect(x0, y0, x1 - x0, y1 - y0).clipped(*self._image_size)
        if rect is not None:
            if self._single:
                # Snímaná oblast je vždycky jen jedna – nový tah nahradí starou.
                self._rects = [rect]
            else:
                self._rects.append(rect)
        self._redraw()

    def _undo(self) -> None:
        if self._rects:
            self._rects.pop()
            self._redraw()

    def _clear(self) -> None:
        self._rects = []
        self._redraw()

    def _ok(self) -> None:
        self.result = list(self._rects)
        self._close()

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self._tempdir.cleanup()


class SettingsDialog(tk.Toplevel):
    """Nastavení časování, detekce konce a OCR."""

    FIELDS = [
        ("activation_delay_ms", "Delay po aktivaci okna [ms]", int),
        ("page_down_delay_ms", "Delay po Page Down [ms]", int),
        ("end_confirmations", "Počet potvrzení konce", int),
        ("max_screenshots", "Maximální počet screenshotů", int),
        ("activation_attempts", "Počet pokusů o aktivaci okna", int),
        ("activation_retry_ms", "Pauza mezi pokusy o aktivaci [ms]", int),
        ("hash_threshold", "Tolerance dHash (0-64)", int),
        ("pixel_threshold", "Tolerance průměrného rozdílu (0.0-1.0)", float),
        ("changed_threshold", "Tolerance podílu změněných pixelů", float),
        ("pdf_dpi", "DPI stránky PDF", int),
        ("pdf_page_width_mm", "Šířka předlohy [mm] (0 = použít DPI)", float),
        ("pdf_upscale", "Zvětšení snímku pro PDF (1 = vypnuto)", float),
        ("pdf_sharpen", "Doostření pro PDF [%] (0 = vypnuto)", float),
        ("ocr_upscale", "Zvětšení snímku pro OCR (1 = vypnuto)", float),
        ("rdp_host", "Adresa RDP relace (povinné)", str),
        ("rdp_width", "Šířka zakládané RDP relace [px]", int),
        ("rdp_height", "Výška zakládané RDP relace [px]", int),
    ]

    def __init__(
        self,
        master: tk.Misc,
        config: AppConfig,
        ocr_languages: list[str] | None = None,
        region_width: int | None = None,
    ) -> None:
        super().__init__(master)
        self.title("Nastavení")
        self.resizable(False, False)
        self.config_obj = config
        self.saved = False
        self._ocr_languages = ocr_languages or []
        self._region_width = region_width or 0
        self._vars: dict[str, tk.StringVar] = {}

        frame = ttk.Frame(self, padding=PAD)
        frame.pack(fill="both", expand=True)

        for row, (key, label, _kind) in enumerate(self.FIELDS):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(getattr(config, key)))
            self._vars[key] = var
            ttk.Entry(frame, textvariable=var, width=18).grid(
                row=row, column=1, sticky="e", padx=(PAD, 0), pady=2
            )

        row = len(self.FIELDS)
        self._dpi_hint = ttk.Label(frame, foreground="#555555", justify="left")
        self._dpi_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 4))
        for key in ("pdf_dpi", "pdf_page_width_mm", "pdf_upscale"):
            self._vars[key].trace_add("write", lambda *_a: self._update_dpi_hint())
        self._update_dpi_hint()

        row += 1
        self._method = tk.StringVar(value=config.page_down_method)
        ttk.Label(frame, text="Metoda odeslání Page Down").grid(
            row=row, column=0, sticky="w", pady=2
        )
        ttk.Combobox(
            frame,
            textvariable=self._method,
            values=["sendinput", "postmessage"],
            state="readonly",
            width=16,
        ).grid(row=row, column=1, sticky="e", padx=(PAD, 0), pady=2)

        row += 1
        self._save_dups = tk.BooleanVar(value=config.save_duplicates)
        ttk.Checkbutton(
            frame,
            text="Ukládat potvrzovací duplicity do podadresáře duplicates",
            variable=self._save_dups,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 2))

        row += 1
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4)
        )

        row += 1
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6)
        )

        row += 1
        self._pdf_compression = tk.StringVar(
            value=COMPRESSION_LABELS.get(
                config.pdf_compression, COMPRESSION_LABELS[pdf_mod.COMPRESSION_LOSSLESS]
            )
        )
        ttk.Label(frame, text="Komprese obrazu v PDF").grid(
            row=row, column=0, sticky="w", pady=2
        )
        ttk.Combobox(
            frame,
            textvariable=self._pdf_compression,
            values=list(COMPRESSION_LABELS.values()),
            state="readonly",
            width=34,
        ).grid(row=row, column=1, sticky="e", padx=(PAD, 0), pady=2)

        row += 1
        ttk.Label(
            frame,
            text=(
                "Bezeztrátová paleta: stránky s nejvýš 256 barvami dostanou\n"
                "indexovanou paletu. Obraz zůstává bit po bitu stejný a soubor\n"
                "je zhruba o čtvrtinu menší – vhodné pro cokoli.\n"
                "Černobílá CCITT G4: 1 bit na pixel, soubor klesne asi na osminu,\n"
                "ale ZTRÁTOVĚ – zmizí vyhlazení písma i šedé výplně ve výkresech.\n"
                "Jen pro čistě textové dokumenty."
            ),
            foreground="#555555",
            justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))

        row += 1
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(4, 6)
        )

        row += 1
        self._ocr_enabled = tk.BooleanVar(value=config.ocr_enabled)
        ttk.Checkbutton(
            frame,
            text="Provést OCR a vložit do PDF vrstvu s vyhledatelným textem",
            variable=self._ocr_enabled,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 2))

        row += 1
        self._ocr_language = tk.StringVar(value=config.ocr_language)
        ttk.Label(frame, text="Jazyk OCR").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            frame,
            textvariable=self._ocr_language,
            values=self._ocr_languages,
            width=16,
        ).grid(row=row, column=1, sticky="e", padx=(PAD, 0), pady=2)

        row += 1
        if self._ocr_languages:
            ocr_note = (
                "OCR zajišťuje engine vestavěný ve Windows – nic se neinstaluje.\n"
                f"Dostupné jazyky: {', '.join(self._ocr_languages)}"
            )
        else:
            ocr_note = (
                "OCR zajišťuje engine vestavěný ve Windows – nic se neinstaluje.\n"
                "Na tomto počítači nebyl nalezen žádný jazyk pro OCR."
            )
        ttk.Label(frame, text=ocr_note, foreground="#555555", justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )

        row += 1
        ttk.Label(
            frame,
            text=(
                "Adresu RDP relace je nutné vyplnit – podle ní se hledá okno\n"
                "Připojení ke vzdálené ploše (například 192.168.1.100).\n"
                "Metoda sendinput je pro klienta RDP spolehlivá.\n"
                "postmessage je náhradní varianta pro aplikace, které ji přijímají."
            ),
            foreground="#555555",
            justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))

        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(PAD, 0))
        ttk.Button(buttons, text="Zrušit", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Uložit", command=self._save).pack(side="right", padx=(0, 6))

        self.transient(master)
        self.grab_set()

    def _float_var(self, key: str, default: float) -> float | None:
        """Hodnota pole jako float; None znamená nečitelný vstup."""
        raw = self._vars[key].get().strip().replace(",", ".")
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return None

    def _update_dpi_hint(self) -> None:
        """Ukáže, jaká hustota z aktuálních hodnot vyjde pro vybranou oblast."""
        width_mm = self._float_var("pdf_page_width_mm", 0.0)
        factor = self._float_var("pdf_upscale", 1.0)
        if width_mm is None or factor is None:
            self._dpi_hint.configure(text="Šířka předlohy / zvětšení: neplatná hodnota.")
            return
        factor = max(1.0, min(4.0, factor))

        upscale_note = ""
        if factor > 1.0:
            upscale_note = f" Se zvětšením {factor:g}× nese stránka {{dpi}} DPI."

        if width_mm <= 0:
            fixed = self._float_var("pdf_dpi", 0.0)
            base = f"{fixed:.0f}" if fixed else "?"
            note = (
                upscale_note.format(dpi=f"{fixed * factor:.0f}")
                if (upscale_note and fixed)
                else upscale_note.format(dpi="?")
            )
            self._dpi_hint.configure(
                text=(
                    f"Šířka předlohy 0 = použije se pevné DPI stránky ({base}).{note}\n"
                    "Zadáním šířky (A4 = 210 mm) se DPI dopočítá ze snímku."
                )
            )
            return
        if self._region_width <= 0:
            self._dpi_hint.configure(
                text=(
                    f"Šířka předlohy {width_mm:g} mm má přednost před DPI.\n"
                    "Výsledné DPI se dopočítá ze šířky snímku při exportu."
                )
            )
            return
        dpi = dpi_for_width(self._region_width, width_mm)
        note = upscale_note.format(dpi=f"{dpi * factor:.0f}") if upscale_note else ""
        self._dpi_hint.configure(
            text=(
                f"Oblast {self._region_width} px při šířce {width_mm:g} mm "
                f"=> {dpi:.0f} DPI.{note}\n"
                "Šířka předlohy má přednost před polem DPI stránky PDF."
            )
        )

    def _save(self) -> None:
        values: dict[str, object] = {}
        for key, label, kind in self.FIELDS:
            raw = self._vars[key].get().strip().replace(",", ".")
            if kind is str:
                values[key] = raw
                continue
            try:
                values[key] = kind(raw)
            except ValueError:
                messagebox.showerror(
                    "Nastavení", f"Pole „{label}“ obsahuje neplatnou hodnotu.", parent=self
                )
                return
        for key, value in values.items():
            setattr(self.config_obj, key, value)
        self.config_obj.page_down_method = self._method.get()
        self.config_obj.save_duplicates = bool(self._save_dups.get())
        self.config_obj.pdf_compression = COMPRESSION_VALUES.get(
            self._pdf_compression.get(), pdf_mod.COMPRESSION_LOSSLESS
        )
        self.config_obj.ocr_enabled = bool(self._ocr_enabled.get())
        self.config_obj.ocr_language = self._ocr_language.get()
        self.config_obj.clamp()
        if not self.config_obj.save():
            messagebox.showwarning(
                "Nastavení",
                "Nastavení se nepodařilo uložit do config.json.\n"
                "Hodnoty budou platit pouze pro toto spuštění.",
                parent=self,
            )
        self.saved = True
        self.destroy()


class ScraperApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.minsize(720, 620)

        self.config_obj = AppConfig.load()
        self.events: queue.Queue = queue.Queue()

        self.tc_window: wm.WindowInfo | None = None
        self.rdp_window: wm.WindowInfo | None = None
        self.tc_candidates: list[wm.WindowInfo] = []
        self.rdp_candidates: list[wm.WindowInfo] = []
        self.region: Region | None = None
        # Umístění okna RDP před roztažením – aby šlo vrátit zpět.
        self._rdp_placement: dict | None = None

        self.controller: AutomationController | None = None
        self.session_dir: str | None = None
        self.logger = None
        self.last_pdf: str | None = None
        self.ocr_languages: list[str] = []
        self._busy = False  # dlouhá operace mimo automatizaci (např. tvorba PDF)

        self._build_ui()
        self._update_rdp_res_label()
        self.refresh_windows()
        self._restore_region()
        self._update_mask_label()
        self._update_buttons()

        self.hotkey = hotkey_mod.EmergencyHotkey(self._on_hotkey)
        if self.hotkey.start():
            self._append_log(f"Nouzové zastavení: {hotkey_mod.DESCRIPTION} nebo ESC v okně aplikace.")
        else:
            self._append_log(
                f"Globální zkratku {hotkey_mod.DESCRIPTION} se nepodařilo zaregistrovat – "
                "použijte tlačítko Ukončit snímání nebo ESC v okně aplikace."
            )

        self.bind("<Escape>", lambda _e: self._on_hotkey())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_events)
        self.after(2500, self._idle_refresh)
        self._probe_ocr()

    # ------------------------------------------------------------------
    def _probe_ocr(self) -> None:
        """Zjistí na pozadí, jaké jazyky umí OCR engine Windows."""

        def worker() -> None:
            try:
                languages = ocr.available_languages()
            except ocr.OcrError as exc:
                self._emit("ocr_languages", {"error": str(exc)})
            else:
                self._emit("ocr_languages", {"languages": languages})

        threading.Thread(target=worker, name="ocr-probe", daemon=True).start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=PAD)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # --- stav oken ---
        box = ttk.LabelFrame(root, text="Stav oken", padding=PAD)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Total Commander:").grid(row=0, column=0, sticky="w")
        self.var_tc = tk.StringVar(value="nenalezen")
        self.lbl_tc = ttk.Label(box, textvariable=self.var_tc, foreground="#B00000")
        self.lbl_tc.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.btn_pick_tc = ttk.Button(box, text="Vybrat okno…", command=self._pick_tc, width=14)
        self.btn_pick_tc.grid(row=0, column=2, sticky="e")

        self.var_rdp_label = tk.StringVar(value="RDP:")
        ttk.Label(box, textvariable=self.var_rdp_label).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.var_rdp = tk.StringVar(value="nenalezena")
        self.lbl_rdp = ttk.Label(box, textvariable=self.var_rdp, foreground="#B00000")
        self.lbl_rdp.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        self.btn_pick_rdp = ttk.Button(box, text="Vybrat okno…", command=self._pick_rdp, width=14)
        self.btn_pick_rdp.grid(row=1, column=2, sticky="e", pady=(4, 0))

        row_buttons = ttk.Frame(box)
        row_buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(PAD, 0))
        ttk.Button(
            row_buttons, text="Obnovit seznam oken", command=self.refresh_windows
        ).pack(side="left")
        self.btn_connect = ttk.Button(
            row_buttons, text="Založit RDP relaci…", command=self._connect_rdp
        )
        self.btn_connect.pack(side="left", padx=(6, 0))
        self.var_rdp_res = tk.StringVar()
        ttk.Label(row_buttons, textvariable=self.var_rdp_res, foreground="#555555").pack(
            side="left", padx=(10, 0)
        )

        # --- oblast ---
        box2 = ttk.LabelFrame(root, text="Vybraná oblast", padding=PAD)
        box2.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        box2.columnconfigure(4, weight=1)
        self.var_x = tk.StringVar(value="–")
        self.var_y = tk.StringVar(value="–")
        self.var_w = tk.StringVar(value="–")
        self.var_h = tk.StringVar(value="–")
        for col, (caption, var) in enumerate(
            [("X:", self.var_x), ("Y:", self.var_y), ("Šířka:", self.var_w), ("Výška:", self.var_h)]
        ):
            cell = ttk.Frame(box2)
            cell.grid(row=0, column=col, sticky="w", padx=(0, 18))
            ttk.Label(cell, text=caption).pack(side="left")
            ttk.Label(cell, textvariable=var, font=("Segoe UI", 9, "bold")).pack(
                side="left", padx=(4, 0)
            )
        self.btn_fit = ttk.Button(
            box2, text="Roztáhnout okno RDP", command=self._toggle_stretch
        )
        self.btn_fit.grid(row=0, column=4, sticky="e", padx=(0, 6))
        self.btn_region = ttk.Button(box2, text="Vybrat oblast", command=self._select_region)
        self.btn_region.grid(row=0, column=5, sticky="e")
        self.var_mask = tk.StringVar(value="Vymazané oblasti: 0")
        ttk.Label(box2, textvariable=self.var_mask).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        self.btn_mask = ttk.Button(
            box2, text="Vymazat oblast…", command=self._select_mask
        )
        self.btn_mask.grid(row=1, column=5, sticky="e", pady=(6, 0))

        # --- stav automatizace ---
        box3 = ttk.LabelFrame(root, text="Stav automatizace", padding=PAD)
        box3.grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        box3.columnconfigure(1, weight=1)
        self.var_status = tk.StringVar(value=Status.READY.value)
        ttk.Label(box3, text="Stav:").grid(row=0, column=0, sticky="w")
        ttk.Label(
            box3, textvariable=self.var_status, font=("Segoe UI", 10, "bold")
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        self.var_count = tk.StringVar(value="Pořízeno snímků: 0    Stránek do PDF: 0")
        ttk.Label(box3, textvariable=self.var_count).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        self.var_session = tk.StringVar(value=f"Pracovní adresář: {cfg_mod.get_working_dir()}")
        ttk.Label(box3, textvariable=self.var_session, foreground="#555555").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        # --- ovládání ---
        box4 = ttk.Frame(root)
        box4.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))
        self.btn_start = ttk.Button(box4, text="Spustit", command=self._start)
        self.btn_pause = ttk.Button(box4, text="Pozastavit", command=self._pause)
        self.btn_resume = ttk.Button(box4, text="Pokračovat", command=self._resume)
        self.btn_stop = ttk.Button(box4, text="Ukončit snímání", command=self._stop)
        for btn in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop):
            btn.pack(side="left", padx=(0, 6))

        box5 = ttk.Frame(root)
        box5.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.btn_pdf = ttk.Button(box5, text="Vytvořit PDF nyní", command=self._make_pdf_now)
        self.btn_pdf.pack(side="left", padx=(0, 6))
        ttk.Button(box5, text="Otevřít pracovní adresář", command=self._open_workdir).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(box5, text="Nastavení", command=self._open_settings).pack(side="left")

        # --- log ---
        box6 = ttk.LabelFrame(root, text="Průběh", padding=PAD)
        box6.grid(row=5, column=0, sticky="nsew", pady=(PAD, 0))
        root.rowconfigure(5, weight=1)
        box6.columnconfigure(0, weight=1)
        box6.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            box6, height=12, wrap="none", state="disabled", font=("Consolas", 9)
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box6, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    # ------------------------------------------------------------------
    # Okna
    # ------------------------------------------------------------------
    def refresh_windows(self) -> None:
        self.tc_candidates = wm.find_total_commander()
        matching, all_rdp = wm.find_rdp(self.config_obj.rdp_host)
        self.rdp_candidates = matching or all_rdp
        self._rdp_exact = bool(matching)

        if self.tc_window is not None and not wm.is_window(self.tc_window.hwnd):
            self.tc_window = None
        if self.rdp_window is not None and not wm.is_window(self.rdp_window.hwnd):
            self.rdp_window = None

        if self.tc_window is None and self.tc_candidates:
            self.tc_window = self.tc_candidates[0]
        if self.rdp_window is None and self.rdp_candidates:
            self.rdp_window = self.rdp_candidates[0]

        self._update_window_labels()
        self._update_buttons()

    def _update_window_labels(self) -> None:
        host = self.config_obj.rdp_host
        self.var_rdp_label.set(f"RDP {host}:" if host else "RDP:")

        if self.tc_window is not None:
            extra = ""
            if len(self.tc_candidates) > 1:
                extra = f"  (nalezeno oken: {len(self.tc_candidates)})"
            self.var_tc.set(f"nalezen – {_short(self.tc_window.title)}{extra}")
            self.lbl_tc.configure(foreground="#006000")
        else:
            self.var_tc.set("nenalezen")
            self.lbl_tc.configure(foreground="#B00000")

        if not host:
            self.var_rdp.set("adresa není vyplněna – zadejte ji v Nastavení")
            self.lbl_rdp.configure(foreground="#B00000")
        elif self.rdp_window is not None:
            if getattr(self, "_rdp_exact", False):
                self.var_rdp.set(f"nalezena – {_short(self.rdp_window.title)}")
                self.lbl_rdp.configure(foreground="#006000")
            else:
                self.var_rdp.set(
                    f"relace k {host} nenalezena – "
                    f"vybráno: {_short(self.rdp_window.title, 28)}"
                )
                self.lbl_rdp.configure(foreground="#B06000")
        else:
            self.var_rdp.set("nenalezena")
            self.lbl_rdp.configure(foreground="#B00000")

    def _idle_refresh(self) -> None:
        if not self._automation_active():
            self.refresh_windows()
        self.after(2500, self._idle_refresh)

    def _pick_tc(self) -> None:
        candidates = wm.find_total_commander()
        if not candidates:
            messagebox.showerror(APP_NAME, "Nebylo nalezeno žádné okno Total Commanderu.")
            return
        picker = WindowPicker(self, "Okno Total Commanderu", candidates)
        self.wait_window(picker)
        if picker.result is not None:
            self.tc_window = picker.result
            self.tc_candidates = candidates
            self._update_window_labels()
            self._update_buttons()

    def _pick_rdp(self) -> None:
        matching, all_rdp = wm.find_rdp(self.config_obj.rdp_host)
        candidates = all_rdp or matching
        if not candidates:
            messagebox.showerror(APP_NAME, "Nebylo nalezeno žádné okno RDP relace.")
            return
        picker = WindowPicker(self, "Okno RDP relace", candidates)
        self.wait_window(picker)
        if picker.result is not None:
            self.rdp_window = picker.result
            self.rdp_candidates = candidates
            self._rdp_exact = self.config_obj.rdp_host.lower() in picker.result.title.lower()
            self._update_window_labels()
            self._update_buttons()

    # ------------------------------------------------------------------
    # Oblast
    # ------------------------------------------------------------------
    def _update_rdp_res_label(self) -> None:
        self.var_rdp_res.set(
            f"rozlišení relace: {self.config_obj.rdp_width}×{self.config_obj.rdp_height} px"
        )

    def _connect_rdp(self) -> None:
        """Založí RDP relaci s pevným rozlišením z Nastavení.

        U už běžící relace rozlišení zvětšit nelze – `mstsc` ho zastropuje
        velikostí monitoru. Proto se relace zakládá znovu z `.rdp` souboru.
        """
        if self._automation_active() or self._busy:
            return
        host = self.config_obj.rdp_host
        if not host:
            messagebox.showerror(
                APP_NAME,
                "Není vyplněna adresa RDP relace.\n\n"
                "Otevřete Nastavení a zadejte adresu serveru.",
            )
            self._open_settings()
            return

        width, height = self.config_obj.rdp_width, self.config_obj.rdp_height
        existing = rdp_mod.find_session(host)
        question = (
            f"Založit relaci k {host} s rozlišením {width}×{height} px?\n\n"
        )
        if existing is not None:
            question += (
                "Relace k této adrese už běží. Nové připojení ji převezme – "
                "otevřené aplikace na serveru zůstanou, jen se přepočítá plocha.\n\n"
            )
        question += "Přihlašovací údaje zadáte sám v okně vzdálené plochy."
        if not messagebox.askyesno(APP_NAME, question):
            return

        try:
            path = rdp_mod.write_rdp_file(
                host, width, height, cfg_mod.get_working_dir()
            )
            rdp_mod.launch(path)
        except rdp_mod.RdpSessionError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self._append_log(f"Spuštěna vzdálená plocha podle {os.path.basename(path)}")
        self._append_log(f"Čekám na přihlášení k {host} (rozlišení {width}×{height} px)…")
        self._busy = True
        self._update_buttons()

        def worker() -> None:
            window = rdp_mod.wait_for_session(host)
            self._emit("rdp_connected", window)

        threading.Thread(target=worker, daemon=True).start()

    def _on_rdp_connected(self, window) -> None:
        """Doběhlo čekání na okno relace – dokončit nastavení, nebo ohlásit timeout."""
        self._busy = False
        if window is None:
            self._append_log(
                "Okno RDP relace se neobjevilo. Až se přihlásíte, klikněte na "
                "„Obnovit seznam oken“."
            )
            self._update_buttons()
            return

        self.rdp_window = window
        self._rdp_exact = True
        self._rdp_placement = None
        self.refresh_windows()
        self._append_log(f"RDP relace připojena: {_short(window.title, 60)}")

        size = self._stretch_rdp()
        if size is None:
            self._update_buttons()
            return
        wanted = (self.config_obj.rdp_width, self.config_obj.rdp_height)
        if size != wanted:
            messagebox.showwarning(
                APP_NAME,
                f"Server přidělil relaci {size[0]}×{size[1]} px místo "
                f"požadovaných {wanted[0]}×{wanted[1]} px.\n\n"
                "Snímat to nebrání, jen vyjde nižší rozlišení stránky. "
                "Zkuste zadat menší hodnotu – starší servery odmítají "
                "výšku nad 2048 px.",
            )
        self._update_buttons()

    def _stretch_rdp(self, log_only_on_change: bool = False) -> tuple[int, int] | None:
        """Roztáhne okno RDP na celou plochu relace. Vrací rozměr client rectu.

        Myší to udělat nejde – okno musí přesahovat pod dolní okraj monitoru,
        kam se okraj okna táhnout nedá. Původní umístění si zapamatujeme, aby
        šlo tlačítkem vrátit.
        """
        if self.rdp_window is None:
            messagebox.showerror(
                APP_NAME,
                "Není vybrané okno RDP relace. Klikněte na „Obnovit okna“.",
            )
            return None
        hwnd = self.rdp_window.hwnd
        before = wm.client_rect(hwnd)[2:]
        if self._rdp_placement is None:
            self._rdp_placement = wm.get_placement(hwnd)
        try:
            size = fit_to_session(
                hwnd, log=lambda message: self._emit("log", message)
            )
        except CaptureError as exc:
            messagebox.showerror(APP_NAME, f"Okno RDP se nepodařilo roztáhnout.\n\n{exc}")
            return None
        if not log_only_on_change or size != before:
            self._append_log(
                f"Okno RDP roztaženo: plocha relace {size[0]}x{size[1]} px"
            )
        self._update_buttons()
        return size

    def _restore_rdp(self) -> None:
        """Vrátí okno RDP tam, kde bylo před roztažením."""
        if self.rdp_window is None or self._rdp_placement is None:
            return
        if wm.set_placement(self.rdp_window.hwnd, self._rdp_placement):
            self._append_log("Okno RDP vráceno na původní velikost.")
        self._rdp_placement = None
        self._update_buttons()

    def _toggle_stretch(self) -> None:
        if self._automation_active() or self._busy:
            return
        if self._rdp_placement is None:
            self._stretch_rdp()
        else:
            self._restore_rdp()

    def _restore_region(self) -> None:
        """Obnoví snímanou oblast z config.json.

        Souřadnice jsou vázané na okno RDP, ne na plochu, takže po restartu
        aplikace platí dál – pokud okno mezitím nezměnilo velikost. To ověří
        až start snímání, kde je znám aktuální client rect.
        """
        stored = list(self.config_obj.region or [])
        if len(stored) != 4:
            return
        region = Region(*(int(v) for v in stored))
        self._apply_region(region, quiet=True)
        self._append_log(f"Oblast z minulého běhu: {region}")

    def _window_snapshot(self):
        """Snímek celého okna RDP – podklad pro výběr oblasti.

        Jde přes stejné ověření aktivního Total Commanderu jako ostrý běh,
        takže se výběr chová stejně jako to, co se pak bude snímat.
        """
        if self.rdp_window is None:
            messagebox.showerror(
                APP_NAME,
                "Není vybrané okno RDP relace. Klikněte na „Obnovit okna“ "
                "a vyberte relaci.",
            )
            return None
        if self.tc_window is None:
            messagebox.showerror(
                APP_NAME,
                "Není nalezený Total Commander. Snímek vzniká jen s ním "
                "v popředí, takže bez něj nelze pořídit ani podklad pro výběr.",
            )
            return None
        try:
            return automation.capture_single(
                self.config_obj,
                self.tc_window.hwnd,
                self.rdp_window.hwnd,
                log=lambda message: self._emit("log", message),
            )
        except (AutomationError, CaptureError) as exc:
            messagebox.showerror(APP_NAME, f"Snímek okna RDP se nepodařilo pořídit.\n\n{exc}")
            return None

    def _select_region(self) -> None:
        if self._automation_active() or self._busy:
            return
        self.var_status.set(Status.SELECTING.value)
        self._update_buttons()
        try:
            # Bez roztažení by šlo označit jen tu část relace, která se vejde
            # na monitor – zbytek okna je schovaný za posuvníky mstsc.
            if self._stretch_rdp(log_only_on_change=True) is None:
                return
            image = self._window_snapshot()
            if image is None:
                return

            current = []
            if self.region is not None:
                current = [
                    mask_mod.MaskRect(
                        self.region.x, self.region.y,
                        self.region.width, self.region.height,
                    )
                ]
            dialog = MaskDialog(
                self,
                image,
                current,
                title="Snímaná oblast v okně RDP",
                prompt=(
                    "Tažením myši označte oblast, která se má snímat – typicky "
                    "samotnou stránku dokumentu\nbez panelů prohlížeče. "
                    "Souřadnice platí v okně RDP, takže vydrží i po restartu "
                    "aplikace."
                ),
                single=True,
            )
            image.close()
            self.wait_window(dialog)
            if dialog.result is None:
                self._append_log("Výběr oblasti zrušen.")
                return
            if not dialog.result:
                self._append_log("Nebyla označena žádná oblast.")
                return
            rect = dialog.result[0]
            self._apply_region(Region(rect.x, rect.y, rect.width, rect.height))
        finally:
            self.var_status.set(Status.READY.value)
            self._update_buttons()

    def _apply_region(self, region: Region, quiet: bool = False) -> None:
        self.region = region
        self.var_x.set(str(region.x))
        self.var_y.set(str(region.y))
        self.var_w.set(str(region.width))
        self.var_h.set(str(region.height))
        self.config_obj.region = [region.x, region.y, region.width, region.height]
        self.config_obj.save()
        if not quiet:
            self._append_log(f"Oblast v okně RDP: {region}")

    # ------------------------------------------------------------------
    # Oblast k vymazání
    # ------------------------------------------------------------------
    def _mask_rects(self) -> list:
        return mask_mod.normalize_rects(self.config_obj.mask_rects)

    def _update_mask_label(self) -> None:
        count = len(self._mask_rects())
        self.var_mask.set(
            "Vymazané oblasti: žádná" if not count else f"Vymazané oblasti: {count}"
        )

    def _mask_source_image(self):
        """Snímek první stránky – čerstvý, jinak z poslední relace."""
        if (
            self.tc_window is not None
            and self.rdp_window is not None
            and self.region is not None
        ):
            try:
                return automation.capture_single(
                    self.config_obj,
                    self.tc_window.hwnd,
                    self.rdp_window.hwnd,
                    self.region,
                    log=lambda message: self._emit("log", message),
                ), "čerstvý snímek"
            except (AutomationError, CaptureError) as exc:
                self._append_log(f"Náhled se nepodařilo pořídit: {exc}")
        pages, _session = self._collect_pages()
        if pages:
            with Image.open(pages[0]) as stored:
                return stored.convert("RGB"), os.path.basename(pages[0])
        return None, ""

    def _select_mask(self) -> None:
        if self._automation_active() or self._busy:
            return
        if self.region is None:
            messagebox.showerror(
                APP_NAME,
                "Nejprve vyberte snímanou oblast – podle ní se určuje, "
                "co se má na stránkách vymazat.",
            )
            return

        image, source = self._mask_source_image()
        if image is None:
            messagebox.showerror(
                APP_NAME,
                "Nepodařilo se získat snímek první stránky.\n\n"
                "Zkontrolujte, že běží Total Commander, nebo nejdřív pořiďte "
                "aspoň jeden snímek.",
            )
            return

        self._append_log(f"Výběr oblasti k vymazání ({source})")
        dialog = MaskDialog(self, image, self._mask_rects())
        image.close()
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self.config_obj.mask_rects = mask_mod.rects_to_config(dialog.result)
        self.config_obj.save()
        self._update_mask_label()
        if dialog.result:
            self._append_log(
                "K vymazání: " + ", ".join(str(rect) for rect in dialog.result)
            )
        else:
            self._append_log("Vymazávání oblastí zrušeno.")

    # ------------------------------------------------------------------
    # Automatizace
    # ------------------------------------------------------------------
    def _automation_active(self) -> bool:
        return self.controller is not None and self.controller.is_running()

    def _start(self) -> None:
        if self._automation_active() or self._busy:
            return

        if not self.config_obj.rdp_host:
            messagebox.showerror(
                APP_NAME,
                "Není vyplněna adresa RDP relace.\n\n"
                "Otevřete Nastavení a do pole „Adresa RDP relace“ zadejte adresu "
                "serveru, ke kterému jste připojen (například 192.168.1.100).\n"
                "Podle ní aplikace pozná správné okno Připojení ke vzdálené ploše.",
            )
            self._open_settings()
            return

        self.refresh_windows()
        if self.tc_window is None:
            messagebox.showerror(
                APP_NAME,
                "Total Commander není spuštěn.\n\n"
                "Spusťte Total Commander a klikněte na Obnovit seznam oken.",
            )
            return
        if self.rdp_window is None:
            messagebox.showerror(
                APP_NAME,
                f"RDP relace k {self.config_obj.rdp_host} nebyla nalezena.\n\n"
                "Aplikace RDP relaci sama nenavazuje – spusťte a přihlaste ji ručně.",
            )
            return
        if not getattr(self, "_rdp_exact", False):
            if not messagebox.askyesno(
                APP_NAME,
                f"Relace k {self.config_obj.rdp_host} nebyla jednoznačně nalezena.\n\n"
                f"Použít okno „{self.rdp_window.title}“?",
            ):
                return
        if self.region is None:
            messagebox.showerror(
                APP_NAME,
                "Nejprve vyberte snímanou oblast v okně RDP.",
            )
            return

        # Bez roztažení by se snímala jen ta část relace, která se vejde na
        # monitor. Dělá se před kontrolou oblasti, aby souhlasil client rect.
        if self._stretch_rdp(log_only_on_change=True) is None:
            return

        # Oblast je vázaná na okno; když okno mezitím změnilo velikost, nesedí.
        client = wm.client_rect(self.rdp_window.hwnd)
        try:
            validate_region(self.region, (client[2], client[3]))
        except CaptureError as exc:
            messagebox.showerror(
                APP_NAME,
                f"{exc}\n\nOkno RDP má teď {client[2]}x{client[3]} px. "
                "Vyberte snímanou oblast znovu.",
            )
            return

        # RDP nesmí být minimalizované
        if wm.is_iconic(self.rdp_window.hwnd):
            wm.restore_window(self.rdp_window.hwnd)
            self.after(300)
            if wm.is_iconic(self.rdp_window.hwnd):
                messagebox.showerror(
                    APP_NAME,
                    "Okno RDP je minimalizované a nepodařilo se jej obnovit.\n"
                    "Obnovte je prosím ručně a spusťte snímání znovu.",
                )
                return
        if wm.is_iconic(self.tc_window.hwnd):
            wm.restore_window(self.tc_window.hwnd)

        try:
            cfg_mod.check_working_dir_writable()
            session_dir = cfg_mod.new_session_dir()
        except OSError as exc:
            messagebox.showerror(
                APP_NAME,
                f"Do pracovního adresáře nelze zapisovat:\n{exc}",
            )
            return

        if self.logger is not None:
            close_logger(self.logger)
        self.session_dir = session_dir
        self.logger = setup_session_logger(session_dir)
        self.last_pdf = None
        self.var_session.set(f"Relace: {session_dir}")
        self._clear_log()
        self._append_log(f"Adresář relace: {session_dir}")

        targets = RunTargets(
            tc_hwnd=self.tc_window.hwnd,
            tc_label=self.tc_window.label(),
            rdp_hwnd=self.rdp_window.hwnd,
            rdp_label=self.rdp_window.label(),
            region=self.region,
        )
        self.controller = AutomationController(
            config=self.config_obj,
            targets=targets,
            session_dir=session_dir,
            emit=self._emit,
            logger=self.logger,
        )
        self.var_count.set("Pořízeno snímků: 0    Stránek do PDF: 0")
        self.controller.start()
        self._update_buttons()

    def _pause(self) -> None:
        if self.controller is not None:
            self.controller.pause()
            self._update_buttons()

    def _resume(self) -> None:
        if self.controller is not None:
            self.controller.resume()
            self._update_buttons()

    def _stop(self) -> None:
        if self.controller is not None and self.controller.is_running():
            self.controller.stop("Ukončeno uživatelem")
            self.var_status.set(Status.STOPPED.value)
            self._update_buttons()

    def _on_hotkey(self) -> None:
        """Nouzové zastavení – voláno i z vlákna hotkey."""
        if self.controller is not None and self.controller.is_running():
            self.controller.stop("Nouzové zastavení uživatelem")
            self._emit("log", "Nouzové zastavení")
            self._emit("status", Status.STOPPED.value)

    # ------------------------------------------------------------------
    # PDF a adresáře
    # ------------------------------------------------------------------
    def _collect_pages(self) -> tuple[list[str], str | None]:
        if self.controller is not None and self.controller.page_files:
            return list(self.controller.page_files), self.session_dir
        if self.session_dir and os.path.isdir(self.session_dir):
            files = sorted(
                os.path.join(self.session_dir, name)
                for name in os.listdir(self.session_dir)
                if name.startswith(cfg_mod.PAGE_PREFIX) and name.lower().endswith(".png")
            )
            return files, self.session_dir
        return [], None

    def _make_pdf_now(self) -> None:
        if self._automation_active():
            messagebox.showinfo(APP_NAME, "Nejprve snímání pozastavte nebo ukončete.")
            return
        if self._busy:
            return
        pages, session_dir = self._collect_pages()
        if not pages or session_dir is None:
            messagebox.showerror(APP_NAME, "Nejsou k dispozici žádné snímky pro PDF.")
            return

        name = os.path.basename(session_dir.rstrip("\\/"))
        pdf_path = os.path.join(session_dir, f"RDP_capture_{name}.pdf")
        self._busy = True
        self.var_status.set(Status.MAKING_PDF.value)
        self._update_buttons()

        def worker() -> None:
            try:
                sources = mask_captures(
                    self.config_obj,
                    pages,
                    session_dir,
                    status=lambda text: self._emit("status", text),
                    log=lambda message: self._emit("log", message),
                )
                layers = ocr_pages(
                    self.config_obj,
                    sources,
                    status=lambda text: self._emit("status", text),
                    log=lambda message: self._emit("log", message),
                )
                self._emit("status", Status.MAKING_PDF.value)
                images_to_pdf(
                    sources,
                    pdf_path,
                    dpi=self.config_obj.pdf_dpi,
                    text_layers=layers,
                    log=lambda message: self._emit("log", message),
                    page_width_mm=self.config_obj.pdf_page_width_mm or None,
                    upscale=self.config_obj.pdf_upscale,
                    sharpen=self.config_obj.pdf_sharpen,
                    compression=self.config_obj.pdf_compression,
                )
            except PdfExportError as exc:
                self._emit("pdf_failed", str(exc))
            else:
                self._emit("pdf_done", pdf_path)

        threading.Thread(target=worker, name="pdf", daemon=True).start()

    def _open_workdir(self) -> None:
        target = self.session_dir if self.session_dir and os.path.isdir(self.session_dir) else None
        target = target or cfg_mod.get_working_dir()
        try:
            os.startfile(target)  # noqa: S606 – standardní otevření průzkumníka
        except (OSError, AttributeError):
            try:
                subprocess.Popen(["explorer", target])
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Adresář se nepodařilo otevřít:\n{exc}")

    def _open_settings(self) -> None:
        if self._automation_active():
            messagebox.showinfo(APP_NAME, "Nastavení nelze měnit během snímání.")
            return
        dialog = SettingsDialog(
            self,
            self.config_obj,
            self.ocr_languages,
            region_width=self.region.width if self.region else None,
        )
        self.wait_window(dialog)
        if dialog.saved:
            self._append_log("Nastavení uloženo.")
            self._update_rdp_res_label()
            self.refresh_windows()

    # ------------------------------------------------------------------
    # Události z vlákna automatizace
    # ------------------------------------------------------------------
    def _emit(self, kind: str, payload: object) -> None:
        self.events.put((kind, payload))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _handle_event(self, kind: str, payload: object) -> None:
        if kind == "rdp_connected":
            self._on_rdp_connected(payload)
        elif kind == "status":
            self.var_status.set(str(payload))
        elif kind == "log":
            self._append_log(str(payload))
        elif kind == "counts" and isinstance(payload, dict):
            self.var_count.set(
                f"Pořízeno snímků: {payload.get('captured', 0)}"
                f"    Stránek do PDF: {payload.get('pages', 0)}"
            )
        elif kind == "ocr_languages" and isinstance(payload, dict):
            if payload.get("error"):
                self._append_log(
                    f"OCR není k dispozici: {payload['error']} – PDF bude bez textové vrstvy."
                )
            else:
                self.ocr_languages = list(payload.get("languages", []))
                if self.ocr_languages:
                    self._append_log(
                        "OCR (engine Windows) k dispozici, jazyky: "
                        + ", ".join(self.ocr_languages)
                    )
                else:
                    self._append_log(
                        "Ve Windows není nainstalován žádný jazyk pro OCR – "
                        "PDF bude bez textové vrstvy."
                    )
        elif kind == "limit":
            messagebox.showwarning(
                APP_NAME,
                f"{payload}\n\nSnímky zůstávají uloženy. "
                "PDF můžete vytvořit tlačítkem „Vytvořit PDF nyní“.",
            )
        elif kind == "error":
            self._append_log(f"CHYBA: {payload}")
        elif kind == "finished" and isinstance(payload, dict):
            self._on_finished(payload)
        elif kind == "pdf_done":
            self._busy = False
            self.last_pdf = str(payload)
            self.var_status.set(Status.DONE.value)
            self._append_log(f"PDF vytvořeno: {payload}")
            self._update_buttons()
            messagebox.showinfo(APP_NAME, f"PDF vytvořeno:\n{payload}")
        elif kind == "pdf_failed":
            self._busy = False
            self.var_status.set(Status.ERROR.value)
            self._append_log(f"CHYBA PDF: {payload}")
            self._update_buttons()
            messagebox.showerror(APP_NAME, f"Vytvoření PDF selhalo:\n{payload}")
        self._update_buttons()

    def _on_finished(self, payload: dict) -> None:
        pdf = payload.get("pdf")
        self.last_pdf = pdf
        self._update_buttons()
        if payload.get("error"):
            messagebox.showerror(
                APP_NAME,
                f"{payload['error']}\n\n"
                f"Pořízené snímky zůstávají v adresáři:\n{payload.get('session_dir')}",
            )
        elif pdf:
            messagebox.showinfo(
                APP_NAME,
                f"{payload.get('reason')}\n\n"
                f"Stránek v PDF: {payload.get('pages')}\n"
                f"Celkem snímků: {payload.get('captured')}\n\n{pdf}",
            )

    # ------------------------------------------------------------------
    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _update_buttons(self) -> None:
        running = self._automation_active()
        paused = running and self.controller is not None and self.controller.is_paused()
        has_pages = bool(self._collect_pages()[0])
        ready = (
            self.tc_window is not None
            and self.rdp_window is not None
            and self.region is not None
            and bool(self.config_obj.rdp_host)
        )

        self.btn_start.configure(state="normal" if (ready and not running and not self._busy) else "disabled")
        self.btn_pause.configure(state="normal" if (running and not paused) else "disabled")
        self.btn_resume.configure(state="normal" if paused else "disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_region.configure(state="disabled" if (running or self._busy) else "normal")
        self.btn_fit.configure(
            text=("Vrátit okno RDP" if self._rdp_placement is not None
                  else "Roztáhnout okno RDP"),
            state="disabled" if (running or self._busy or self.rdp_window is None)
            else "normal",
        )
        self.btn_pdf.configure(
            state="normal" if (has_pages and not running and not self._busy) else "disabled"
        )
        self.btn_pick_tc.configure(state="disabled" if running else "normal")
        self.btn_pick_rdp.configure(state="disabled" if running else "normal")
        self.btn_connect.configure(
            state="disabled" if (running or self._busy) else "normal"
        )
        self.btn_mask.configure(
            state="disabled" if (running or self._busy) else "normal"
        )

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._automation_active():
            if not messagebox.askyesno(
                APP_NAME, "Snímání stále probíhá. Opravdu ukončit aplikaci?"
            ):
                return
            self.controller.stop("Aplikace ukončena uživatelem")
            self.controller.join(timeout=3.0)
        try:
            self.hotkey.stop()
        except Exception:
            pass
        if self.logger is not None:
            close_logger(self.logger)
        self.destroy()


def run() -> None:
    app = ScraperApp()
    app.mainloop()

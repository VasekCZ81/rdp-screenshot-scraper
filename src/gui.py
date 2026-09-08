"""GUI aplikace RDP Screenshot Scraper (tkinter)."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import config as cfg_mod
import hotkey as hotkey_mod
import ocr
import window_manager as wm
from automation import (
    AutomationController,
    RunTargets,
    Status,
    close_logger,
    ocr_pages,
    setup_session_logger,
)
from capture import Region
from config import APP_NAME, AppConfig
from pdf_export import PdfExportError, images_to_pdf
from region_selector import select_region

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
        ("rdp_host", "Adresa RDP relace (povinné)", str),
    ]

    def __init__(
        self, master: tk.Misc, config: AppConfig, ocr_languages: list[str] | None = None
    ) -> None:
        super().__init__(master)
        self.title("Nastavení")
        self.resizable(False, False)
        self.config_obj = config
        self.saved = False
        self._ocr_languages = ocr_languages or []
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

        self.controller: AutomationController | None = None
        self.session_dir: str | None = None
        self.logger = None
        self.last_pdf: str | None = None
        self.ocr_languages: list[str] = []
        self._busy = False  # dlouhá operace mimo automatizaci (např. tvorba PDF)

        self._build_ui()
        self.refresh_windows()
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

        ttk.Button(box, text="Obnovit seznam oken", command=self.refresh_windows).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(PAD, 0)
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
        self.btn_region = ttk.Button(box2, text="Vybrat oblast", command=self._select_region)
        self.btn_region.grid(row=0, column=5, sticky="e")

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
    def _select_region(self) -> None:
        if self._automation_active():
            return
        self.var_status.set(Status.SELECTING.value)
        self.withdraw()
        self.after(150, self._show_overlay)

    def _show_overlay(self) -> None:
        select_region(self, self._region_selected)

    def _region_selected(self, region: Region | None) -> None:
        self.deiconify()
        self.lift()
        if region is None:
            self.var_status.set(Status.READY.value)
            self._append_log("Výběr oblasti zrušen.")
        else:
            self.region = region
            self.var_x.set(str(region.x))
            self.var_y.set(str(region.y))
            self.var_w.set(str(region.width))
            self.var_h.set(str(region.height))
            self.var_status.set(Status.READY.value)
            self._append_log(f"Oblast: {region}")
        self._update_buttons()

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
            messagebox.showerror(APP_NAME, "Nejprve vyberte oblast obrazovky.")
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
                layers = ocr_pages(
                    self.config_obj,
                    pages,
                    status=lambda text: self._emit("status", text),
                    log=lambda message: self._emit("log", message),
                )
                self._emit("status", Status.MAKING_PDF.value)
                images_to_pdf(
                    pages, pdf_path, dpi=self.config_obj.pdf_dpi, text_layers=layers
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
        dialog = SettingsDialog(self, self.config_obj, self.ocr_languages)
        self.wait_window(dialog)
        if dialog.saved:
            self._append_log("Nastavení uloženo.")
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
        if kind == "status":
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
        self.btn_pdf.configure(
            state="normal" if (has_pages and not running and not self._busy) else "disabled"
        )
        self.btn_pick_tc.configure(state="disabled" if running else "normal")
        self.btn_pick_rdp.configure(state="disabled" if running else "normal")

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

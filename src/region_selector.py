"""Výběr obdélníkové oblasti myší přes průhledný fullscreen overlay.

Overlay pokrývá celou virtuální plochu (všechny monitory) včetně záporných
souřadnic. Aplikace je per-monitor DPI aware, takže souřadnice Tk odpovídají
fyzickým pixelům – vybraná oblast se shoduje s tím, co uživatel označil.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from capture import Region
from window_manager import ensure_foreground, primary_screen_size, virtual_screen_rect

MIN_SIZE = 4  # menší tah bereme jako omyl / kliknutí


class RegionSelector:
    def __init__(self, master: tk.Misc, on_done: Callable[[Region | None], None]) -> None:
        self.master = master
        self.on_done = on_done
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None
        self._result: Region | None = None
        self._closed = False

        vx, vy, vw, vh = virtual_screen_rect()
        self._origin = (vx, vy)

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.30)
        except tk.TclError:
            pass
        self.top.configure(bg="black")
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

        self.canvas = tk.Canvas(
            self.top, bg="black", highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)
        # Nápovědu umístíme doprostřed primárního monitoru, ne doprostřed
        # virtuální plochy – ta může vycházet na hranici dvou monitorů.
        primary_w, _primary_h = primary_screen_size()
        self.canvas.create_text(
            primary_w // 2 - vx,
            40 - vy,
            text="Táhnutím myší označte oblast pro snímání.  ESC = zrušit",
            fill="white",
            font=("Segoe UI", 16, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # ESC vážeme na overlay i na plátno – klávesový fokus drží plátno.
        self.top.bind("<Escape>", lambda _e: self._cancel())
        self.canvas.bind("<Escape>", lambda _e: self._cancel())

        self.top.update_idletasks()
        self._claim_focus()
        self.top.grab_set()

    def _claim_focus(self) -> None:
        """Zajistí klávesový fokus, jinak by overlay nereagoval na ESC.

        Samotné `focus_force()` uspěje jen tehdy, když aplikace už drží
        foreground; overlay proto aktivujeme i přes Win32 API.
        """
        self.top.focus_force()
        try:
            ensure_foreground(self.top.winfo_id(), activation_delay_ms=60, attempts=2,
                              retry_ms=100)
        except OSError:
            pass
        self.canvas.focus_set()

    # ------------------------------------------------------------------
    def _to_screen(self, x: int, y: int) -> tuple[int, int]:
        return x + self._origin[0], y + self._origin[1]

    def _on_press(self, event: tk.Event) -> None:
        self._start = (event.x, event.y)
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00FF00", width=2
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self._start is None or self._rect_id is None:
            return
        self.canvas.coords(self._rect_id, self._start[0], self._start[1], event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if self._start is None:
            return
        x0, y0 = self._start
        x1, y1 = event.x, event.y
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        width, height = right - left, bottom - top
        if width < MIN_SIZE or height < MIN_SIZE:
            self._start = None
            return
        sx, sy = self._to_screen(left, top)
        self._result = Region(x=sx, y=sy, width=width, height=height)
        self._close()

    def _cancel(self) -> None:
        self._result = None
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()
        self.on_done(self._result)


def select_region(master: tk.Misc, on_done: Callable[[Region | None], None]) -> RegionSelector:
    return RegionSelector(master, on_done)

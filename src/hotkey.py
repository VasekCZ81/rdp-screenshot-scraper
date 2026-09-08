"""Globální nouzová klávesová zkratka Ctrl+Shift+F12.

RegisterHotKey zachytí kombinaci na úrovni systému dříve, než ji dostane
aplikace v popředí – do RDP relace se tedy neodešle jako běžný vstup.

Hotkey je vázán na vlákno, které jej registruje, proto zde běží vlastní
smyčka zpráv. Pokud registrace selže (např. zkratku obsadila jiná aplikace),
třída to tiše ohlásí a aplikace se spolehne na tlačítko v GUI.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_F12 = 0x7B
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 0xA17E

DESCRIPTION = "Ctrl+Shift+F12"


class EmergencyHotkey:
    """Registruje nouzovou zkratku a volá `callback` v odděleném vlákně."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._registered = threading.Event()
        self._failed = threading.Event()
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return self._registered.is_set() and not self._failed.is_set()

    def start(self, timeout: float = 2.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._registered.wait(timeout)
        return self.active

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())

        ok = user32.RegisterHotKey(
            None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_F12
        )
        if not ok:
            self._failed.set()
        self._registered.set()
        if not ok:
            return

        try:
            msg = wt.MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self._callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

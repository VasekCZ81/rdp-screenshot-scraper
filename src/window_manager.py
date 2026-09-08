"""Práce s okny přes Win32 API.

Modul zajišťuje:
  * DPI awareness (per-monitor v2),
  * vyhledání okna Total Commanderu a RDP relace,
  * robustní aktivaci okna,
  * OVĚŘENÍ foreground window pomocí GetForegroundWindow(),
  * odeslání klávesy Page Down.

Klíčové pravidlo celé aplikace: screenshot smí vzniknout pouze tehdy, když
`is_foreground(hwnd_total_commander)` vrátí True. Tento modul poskytuje
`ensure_foreground()`, které aktivaci provede a následně DVAKRÁT ověří.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass
from typing import Iterable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- konstanty -----------------------------------------------------------
SW_RESTORE = 9
SW_SHOW = 5
GA_ROOT = 2

VK_NEXT = 0x22  # Page Down
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

TOTALCMD_EXES = ("totalcmd64.exe", "totalcmd.exe")
TOTALCMD_CLASS = "TTOTAL_CMD"
TOTALCMD_TITLE_HINT = "total commander"

RDP_EXES = ("mstsc.exe", "msrdc.exe")
RDP_CLASSES = ("TscShellContainerClass",)

# --- prototypy -----------------------------------------------------------
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wt.HWND
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint
kernel32.GetCurrentThreadId.restype = wt.DWORD


# SendInput je citlivý na přesnou velikost struktury INPUT (x64 = 40 B,
# x86 = 28 B). Union proto obsahuje všechny tři varianty včetně největší
# MOUSEINPUT – jinak SendInput selže s chybou 87 (ERROR_INVALID_PARAMETER).
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT


# ------------------------------------------------------------------------
def enable_dpi_awareness() -> str:
    """Zapne per-monitor DPI awareness. Musí se volat před vytvořením GUI."""
    try:
        # Windows 10 1703+
        ctx = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return "none"


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """(x, y, width, height) celé virtuální plochy – včetně záporných souřadnic."""
    gsm = user32.GetSystemMetrics
    return (
        gsm(SM_XVIRTUALSCREEN),
        gsm(SM_YVIRTUALSCREEN),
        gsm(SM_CXVIRTUALSCREEN),
        gsm(SM_CYVIRTUALSCREEN),
    )


def primary_screen_size() -> tuple[int, int]:
    """Rozměr primárního monitoru v pixelech (počátek je vždy 0,0)."""
    gsm = user32.GetSystemMetrics
    return gsm(SM_CXSCREEN), gsm(SM_CYSCREEN)


# ------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe: str

    def label(self) -> str:
        exe = self.exe or "?"
        title = self.title or "(bez názvu)"
        return f"{title}  [{exe}, HWND {self.hwnd}]"


def _process_image_name(pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            path = buf.value
            return path.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


_ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows() -> list[WindowInfo]:
    """Všechna viditelná top-level okna s neprázdným titulkem."""
    result: list[WindowInfo] = []
    pid_cache: dict[int, str] = {}

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if not title:
            return True
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_value = pid.value
        if pid_value not in pid_cache:
            pid_cache[pid_value] = _process_image_name(pid_value)
        result.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title,
                class_name=_class_name(hwnd),
                pid=pid_value,
                exe=pid_cache[pid_value],
            )
        )
        return True

    user32.EnumWindows(_ENUMPROC(_cb), 0)
    return result


def _sorted_candidates(windows: Iterable[WindowInfo], preferred_class: str) -> list[WindowInfo]:
    """Hlavní okno (očekávaná třída) dáme na první místo."""
    return sorted(
        windows,
        key=lambda w: (0 if w.class_name == preferred_class else 1, w.hwnd),
    )


def find_total_commander() -> list[WindowInfo]:
    """Kandidáti na hlavní okno Total Commanderu, nejpravděpodobnější první."""
    windows = list_windows()
    by_exe = [w for w in windows if w.exe.lower() in TOTALCMD_EXES]
    if by_exe:
        main = [w for w in by_exe if w.class_name == TOTALCMD_CLASS]
        return _sorted_candidates(main or by_exe, TOTALCMD_CLASS)
    # Sekundární metoda – class name / titulek
    fallback = [
        w
        for w in windows
        if w.class_name == TOTALCMD_CLASS or TOTALCMD_TITLE_HINT in w.title.lower()
    ]
    return _sorted_candidates(fallback, TOTALCMD_CLASS)


def find_rdp(host: str) -> tuple[list[WindowInfo], list[WindowInfo]]:
    """Vrátí (okna odpovídající hostiteli, všechna RDP okna).

    Preferuje relaci, jejíž titulek obsahuje zadanou adresu.
    """
    windows = list_windows()
    rdp = [
        w
        for w in windows
        if w.exe.lower() in RDP_EXES or w.class_name in RDP_CLASSES
    ]
    host_lower = (host or "").strip().lower()
    matching = [w for w in rdp if host_lower and host_lower in w.title.lower()]
    return (
        _sorted_candidates(matching, RDP_CLASSES[0]),
        _sorted_candidates(rdp, RDP_CLASSES[0]),
    )


# ------------------------------------------------------------------------
def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def is_iconic(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def restore_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)


def get_foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def is_foreground(hwnd: int) -> bool:
    """True pouze pokud je `hwnd` skutečně aktivním oknem systému."""
    if not is_window(hwnd):
        return False
    fg = get_foreground_hwnd()
    if not fg:
        return False
    if fg == hwnd:
        return True
    # Foreground může být potomek/vlastněné okno cílové aplikace.
    root = int(user32.GetAncestor(fg, GA_ROOT) or 0)
    return root == hwnd


def _thread_id(hwnd: int) -> int:
    pid = wt.DWORD(0)
    return int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))


def _try_set_foreground(hwnd: int) -> None:
    """Jeden pokus o aktivaci. Nepoužívá agresivní/destabilizující postupy."""
    fg = get_foreground_hwnd()
    if fg == hwnd:
        return

    cur_tid = int(kernel32.GetCurrentThreadId())
    fg_tid = _thread_id(fg) if fg else 0
    target_tid = _thread_id(hwnd)

    attached: list[int] = []
    try:
        for tid in (fg_tid, target_tid):
            if tid and tid != cur_tid and tid not in attached:
                if user32.AttachThreadInput(cur_tid, tid, True):
                    attached.append(tid)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)


def ensure_foreground(
    hwnd: int,
    activation_delay_ms: int = 300,
    attempts: int = 3,
    retry_ms: int = 400,
    log=None,
) -> bool:
    """Aktivuje okno a OVĚŘÍ, že je opravdu foreground.

    Postup podle zadání:
      1. aktivace okna,
      2. krátké čekání na dokončení přepnutí,
      3. ověření GetForegroundWindow(),
      4. druhé ověření po krátké prodlevě,
      5. teprve pak smí volající pořídit screenshot / poslat klávesu.

    Vrací True jen tehdy, když obě ověření uspěla.
    """
    if not is_window(hwnd):
        if log:
            log("Okno již neexistuje (HWND %s)" % hwnd)
        return False

    for attempt in range(1, max(1, attempts) + 1):
        if not is_window(hwnd):
            return False
        if is_iconic(hwnd):
            restore_window(hwnd)
        elif not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, SW_SHOW)

        _try_set_foreground(hwnd)
        time.sleep(max(0, activation_delay_ms) / 1000.0)

        if is_foreground(hwnd):
            # druhé, nezávislé ověření – chrání před přepnutím "na poslední chvíli"
            time.sleep(0.08)
            if is_foreground(hwnd):
                return True

        if log:
            log(f"Aktivace okna HWND {hwnd} neuspěla (pokus {attempt}/{attempts})")
        if attempt < attempts:
            time.sleep(max(0, retry_ms) / 1000.0)

    return False


# ------------------------------------------------------------------------
def send_page_down(hwnd: int, method: str = "sendinput") -> None:
    """Odešle Page Down. Volající MUSÍ mít předem ověřený foreground `hwnd`."""
    if method == "postmessage":
        _send_page_down_postmessage(hwnd)
    else:
        _send_page_down_input()


def _send_page_down_input() -> None:
    """SendInput – jediná metoda, kterou klient RDP spolehlivě přenese do relace."""
    scan = user32.MapVirtualKeyW(VK_NEXT, 0)

    def _make(flags: int) -> INPUT:
        return INPUT(
            type=INPUT_KEYBOARD,
            u=_INPUTunion(
                ki=KEYBDINPUT(
                    wVk=VK_NEXT,
                    wScan=scan,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )

    events = (INPUT * 2)(
        _make(KEYEVENTF_EXTENDEDKEY),
        _make(KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP),
    )
    sent = user32.SendInput(2, events, ctypes.sizeof(INPUT))
    if sent != 2:
        raise OSError(f"SendInput odeslal {sent} z 2 událostí (chyba {ctypes.get_last_error()})")


def _send_page_down_postmessage(hwnd: int) -> None:
    scan = user32.MapVirtualKeyW(VK_NEXT, 0)
    lparam_down = 1 | (scan << 16) | (1 << 24)
    lparam_up = lparam_down | (1 << 30) | (1 << 31)
    target = _focused_hwnd_of(hwnd) or hwnd
    if not user32.PostMessageW(target, WM_KEYDOWN, VK_NEXT, lparam_down):
        raise OSError(f"PostMessage WM_KEYDOWN selhal (chyba {ctypes.get_last_error()})")
    if not user32.PostMessageW(target, WM_KEYUP, VK_NEXT, lparam_up):
        raise OSError(f"PostMessage WM_KEYUP selhal (chyba {ctypes.get_last_error()})")


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


def _focused_hwnd_of(hwnd: int) -> int:
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if user32.GetGUIThreadInfo(_thread_id(hwnd), ctypes.byref(info)):
        return int(info.hwndFocus or 0)
    return 0

"""Testy Win32 vrstvy, které lze provést bez cizích oken."""

from __future__ import annotations

import ctypes
import unittest

import helpers  # noqa: F401  (nastaví sys.path na ./src)

import window_manager as wm  # noqa: E402


class TestInputStructLayout(unittest.TestCase):
    """SendInput odmítne strukturu s nesprávnou velikostí chybou 87."""

    def test_input_size_matches_windows_abi(self):
        self.assertEqual(ctypes.sizeof(wm.INPUT), wm.EXPECTED_INPUT_SIZE)

    def test_union_is_as_large_as_mouseinput(self):
        # MOUSEINPUT je největší varianta – podle ní se počítá velikost INPUT
        self.assertEqual(
            ctypes.sizeof(wm._INPUTunion), ctypes.sizeof(wm.MOUSEINPUT)
        )
        self.assertGreaterEqual(
            ctypes.sizeof(wm._INPUTunion), ctypes.sizeof(wm.KEYBDINPUT)
        )

    def test_keybdinput_field_offsets(self):
        self.assertEqual(wm.KEYBDINPUT.wVk.offset, 0)
        self.assertEqual(wm.KEYBDINPUT.wScan.offset, 2)
        self.assertEqual(wm.KEYBDINPUT.dwFlags.offset, 4)
        self.assertEqual(wm.KEYBDINPUT.time.offset, 8)

    def test_page_down_is_an_extended_key(self):
        self.assertEqual(wm.VK_NEXT, 0x22)
        self.assertNotEqual(wm.user32.MapVirtualKeyW(wm.VK_NEXT, 0), 0)


class TestScreenMetrics(unittest.TestCase):
    def test_dpi_awareness_is_enabled(self):
        mode = wm.enable_dpi_awareness()
        self.assertIn(mode, ("per-monitor-v2", "per-monitor", "system"))

    def test_virtual_screen_contains_primary(self):
        vx, vy, vw, vh = wm.virtual_screen_rect()
        pw, ph = wm.primary_screen_size()
        self.assertGreater(vw, 0)
        self.assertGreater(vh, 0)
        self.assertLessEqual(vx, 0)
        self.assertLessEqual(vy, 0)
        self.assertGreaterEqual(vx + vw, pw)
        self.assertGreaterEqual(vy + vh, ph)


class TestWindowLookup(unittest.TestCase):
    def test_list_windows_returns_titled_windows(self):
        windows = wm.list_windows()
        self.assertTrue(windows, "nenalezeno žádné viditelné okno")
        for win in windows:
            self.assertTrue(win.title)
            self.assertTrue(wm.is_window(win.hwnd))
            self.assertIn("HWND", win.label())

    def test_invalid_hwnd_is_never_foreground(self):
        self.assertFalse(wm.is_window(0))
        self.assertFalse(wm.is_foreground(0))
        self.assertFalse(wm.is_foreground(1))

    def test_find_rdp_filters_by_host(self):
        matching, all_rdp = wm.find_rdp("192.168.1.100")
        for win in matching:
            self.assertIn("192.168.1.100", win.title)
        self.assertTrue(set(matching).issubset(set(all_rdp)))

    def test_find_rdp_with_unknown_host_matches_nothing(self):
        matching, _all_rdp = wm.find_rdp("10.255.255.254")
        self.assertEqual(matching, [])

    def test_empty_host_matches_nothing_but_still_lists_sessions(self):
        """Bez vyplněné adresy nelze relaci určit – GUI proto spuštění zakáže."""
        matching, all_rdp = wm.find_rdp("")
        self.assertEqual(matching, [])
        self.assertIsInstance(all_rdp, list)

    def test_ensure_foreground_fails_for_dead_window(self):
        logged: list[str] = []
        self.assertFalse(wm.ensure_foreground(0, 0, 1, 0, log=logged.append))
        self.assertTrue(logged)


if __name__ == "__main__":
    unittest.main()

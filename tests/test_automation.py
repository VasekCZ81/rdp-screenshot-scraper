"""Testy snímacího cyklu.

Win32 vrstva je nahrazena simulací, takže testy běží bez Total Commanderu
a bez RDP relace. Nejdůležitější kontrolovaná vlastnost:

    screenshot nesmí vzniknout, pokud foreground window není Total Commander.
"""

from __future__ import annotations

import glob
import os
import tempfile
import unittest
from unittest import mock

from helpers import make_page, read_bytes as _read  # noqa: E402

import automation  # noqa: E402
import window_manager as wm  # noqa: E402
from automation import AutomationController, RunTargets, close_logger, setup_session_logger  # noqa: E402
from capture import Region  # noqa: E402
from config import AppConfig  # noqa: E402

TC_HWND = 1001
RDP_HWND = 2002


class FakeWin:
    """Simulace Win32 vrstvy včetně evidence pořadí operací."""

    def __init__(
        self,
        tc_activatable: bool = True,
        rdp_activatable: bool = True,
        tc_foreground_ok: bool = True,
        close_rdp_after: int | None = None,
        close_tc_after: int | None = None,
    ) -> None:
        self.tc_activatable = tc_activatable
        self.rdp_activatable = rdp_activatable
        self.tc_foreground_ok = tc_foreground_ok
        self.close_rdp_after = close_rdp_after
        self.close_tc_after = close_tc_after

        self.foreground = 0
        self.page_downs = 0
        self.captures = 0
        self.keys: list[tuple[str, int]] = []
        self.trace: list[str] = []
        self.violations: list[str] = []

    # --- náhrady window_manager ---------------------------------------
    def is_window(self, hwnd: int) -> bool:
        if hwnd == RDP_HWND and self.close_rdp_after is not None:
            return self.captures < self.close_rdp_after
        if hwnd == TC_HWND and self.close_tc_after is not None:
            return self.captures < self.close_tc_after
        return hwnd in (TC_HWND, RDP_HWND)

    def ensure_foreground(self, hwnd, **_kwargs) -> bool:
        if not self.is_window(hwnd):
            return False
        allowed = self.tc_activatable if hwnd == TC_HWND else self.rdp_activatable
        if not allowed:
            self.trace.append(f"activate_fail:{hwnd}")
            return False
        self.foreground = hwnd
        self.trace.append(f"activate:{hwnd}")
        return True

    def is_foreground(self, hwnd: int) -> bool:
        if hwnd == TC_HWND and not self.tc_foreground_ok:
            return False
        return self.foreground == hwnd

    def send_page_down(self, hwnd, method="sendinput") -> None:
        self.send_key(hwnd, "pagedown", method)

    def send_key(self, hwnd, key="pagedown", method="sendinput",
                 presses=1, delay_ms=0) -> None:
        if self.foreground != RDP_HWND:
            self.violations.append("klávesa posuvu odeslána mimo aktivní RDP")
        self.page_downs += 1
        self.keys.append((key, presses))
        self.trace.append("pagedown")

    def is_iconic(self, _hwnd) -> bool:
        return False

    def restore_window(self, _hwnd) -> None:
        pass


class FakeCapturer:
    """Vrací předem připravenou sekvenci snímků a hlídá foreground window."""

    def __init__(self, win: FakeWin, images) -> None:
        self.win = win
        self.images = list(images)

    def grab(self, _region):
        if self.win.foreground != TC_HWND:
            self.win.violations.append("Screenshot pořízen bez aktivního Total Commanderu")
        if not self.win.tc_foreground_ok:
            self.win.violations.append("Screenshot pořízen, ačkoli TC nebyl foreground")
        index = min(self.win.captures, len(self.images) - 1)
        self.win.captures += 1
        self.win.trace.append("capture")
        return self.images[index]

    def close(self) -> None:
        pass


class AutomationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = self.tmp.name
        self.logger = setup_session_logger(self.session_dir)
        self.events: list[tuple[str, object]] = []

    def tearDown(self):
        close_logger(self.logger)
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    def run_controller(self, win: FakeWin, images, config: AppConfig | None = None):
        cfg = config or AppConfig()
        cfg.activation_delay_ms = 0
        cfg.page_down_delay_ms = 0
        cfg.activation_retry_ms = 0
        cfg.clamp()

        targets = RunTargets(
            tc_hwnd=TC_HWND,
            tc_label="Total Commander",
            rdp_hwnd=RDP_HWND,
            rdp_label="RDP 192.168.1.100",
            region=Region(0, 0, 200, 150),
        )
        controller = AutomationController(
            config=cfg,
            targets=targets,
            session_dir=self.session_dir,
            emit=lambda kind, payload: self.events.append((kind, payload)),
            logger=self.logger,
        )

        capturer = FakeCapturer(win, images)
        with mock.patch.object(automation.wm, "is_window", win.is_window), \
             mock.patch.object(automation.wm, "ensure_foreground", win.ensure_foreground), \
             mock.patch.object(automation.wm, "is_foreground", win.is_foreground), \
             mock.patch.object(automation.wm, "send_page_down", win.send_page_down), \
             mock.patch.object(automation.wm, "send_key", win.send_key), \
             mock.patch.object(automation, "validate_region", lambda _r: None), \
             mock.patch.object(automation, "ScreenCapturer", lambda: capturer):
            controller.start()
            controller.join(timeout=30)
        self.assertFalse(controller.is_running(), "vlákno automatizace nedoběhlo")
        return controller

    def event_kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def finished_payload(self) -> dict:
        for kind, payload in self.events:
            if kind == "finished":
                return payload  # type: ignore[return-value]
        self.fail("událost 'finished' nedorazila")

    def saved_pages(self) -> list[str]:
        return sorted(glob.glob(os.path.join(self.session_dir, "page_*.png")))


class TestHappyPath(AutomationTestCase):
    """Test 1 + 2 ze zadání: vícestránkový dokument a detekce konce."""

    def test_unique_pages_and_end_detection(self):
        a, b, c = make_page(1), make_page(2), make_page(3)
        win = FakeWin()
        controller = self.run_controller(win, [a, b, c, c, c])

        self.assertEqual(win.violations, [])
        self.assertEqual(len(controller.page_files), 3, "očekávány 3 unikátní stránky")
        self.assertEqual(win.captures, 5)
        self.assertEqual(win.page_downs, 4)
        self.assertIn("Detekován konec", controller.finished_reason)

        names = [os.path.basename(p) for p in self.saved_pages()]
        self.assertEqual(names, ["page_0001.png", "page_0002.png", "page_0003.png"])

        payload = self.finished_payload()
        self.assertIsNone(payload["error"])
        self.assertTrue(os.path.isfile(payload["pdf"]))
        self.assertIn(b"/Count 3", _read(payload["pdf"]))

    def test_duplicates_are_kept_out_of_pdf(self):
        a, b = make_page(1), make_page(2)
        win = FakeWin()
        controller = self.run_controller(win, [a, b, b, b])
        self.assertEqual(len(controller.page_files), 2)
        self.assertEqual(len(controller.duplicate_files), 2)
        for path in controller.duplicate_files:
            self.assertTrue(os.path.isfile(path))
            self.assertIn("duplicates", path)

    def test_foreground_order_is_correct(self):
        win = FakeWin()
        self.run_controller(win, [make_page(1), make_page(2), make_page(2), make_page(2)])
        trace = [step for step in win.trace if step != "activate_fail"]
        self.assertEqual(trace[0], f"activate:{TC_HWND}")
        self.assertEqual(trace[1], "capture")
        # každému 'capture' musí bezprostředně předcházet aktivace TC
        for index, step in enumerate(trace):
            if step == "capture":
                self.assertEqual(trace[index - 1], f"activate:{TC_HWND}")
        # každému 'pagedown' musí bezprostředně předcházet aktivace RDP
        for index, step in enumerate(trace):
            if step == "pagedown":
                self.assertEqual(trace[index - 1], f"activate:{RDP_HWND}")


class TestFailures(AutomationTestCase):
    def test_no_screenshot_when_total_commander_cannot_be_activated(self):
        """Test 4 ze zadání."""
        win = FakeWin(tc_activatable=False)
        controller = self.run_controller(win, [make_page(1)])
        self.assertEqual(win.captures, 0, "screenshot nesmí vzniknout")
        self.assertEqual(win.page_downs, 0)
        self.assertEqual(win.violations, [])
        self.assertIn("Total Commander", controller.finished_reason)
        self.assertIn("error", self.event_kinds())

    def test_no_page_down_when_rdp_cannot_be_activated(self):
        """Test 5 ze zadání."""
        win = FakeWin(rdp_activatable=False)
        controller = self.run_controller(win, [make_page(1), make_page(2)])
        self.assertEqual(win.page_downs, 0, "Page Down se nesmí odeslat")
        self.assertEqual(win.captures, 1, "výchozí snímek zůstává pořízen")
        self.assertEqual(len(controller.page_files), 1)
        self.assertTrue(os.path.isfile(controller.page_files[0]))
        self.assertIn("RDP", controller.finished_reason)

    def test_foreground_check_blocks_capture(self):
        """Test 3 ze zadání – uživatel přepnul okno těsně před snímkem."""
        win = FakeWin(tc_foreground_ok=False)
        controller = self.run_controller(win, [make_page(1)])
        self.assertEqual(win.captures, 0)
        self.assertEqual(win.violations, [])
        self.assertEqual(controller.page_files, [])

    def test_rdp_closed_during_run_keeps_screenshots(self):
        """Test 6 ze zadání."""
        win = FakeWin(close_rdp_after=2)
        controller = self.run_controller(
            win, [make_page(i) for i in range(1, 8)]
        )
        self.assertGreaterEqual(len(controller.page_files), 2)
        for path in controller.page_files:
            self.assertTrue(os.path.isfile(path), "snímky se nesmí ztratit")
        self.assertIn("RDP", controller.finished_reason)

    def test_total_commander_closed_during_run(self):
        win = FakeWin(close_tc_after=2)
        controller = self.run_controller(win, [make_page(i) for i in range(1, 8)])
        self.assertEqual(win.violations, [])
        self.assertGreaterEqual(len(controller.page_files), 1)
        self.assertIn("Total Commander", controller.finished_reason)

    def test_max_screenshots_limit(self):
        cfg = AppConfig()
        cfg.max_screenshots = 4
        win = FakeWin()
        controller = self.run_controller(
            win, [make_page(i) for i in range(1, 20)], config=cfg
        )
        self.assertEqual(win.captures, 4)
        self.assertIn("limit", self.event_kinds())
        self.assertIn("maximální počet", controller.finished_reason.lower())
        # data zůstávají a PDF se vytvoří
        payload = self.finished_payload()
        self.assertTrue(os.path.isfile(payload["pdf"]))
        self.assertEqual(len(self.saved_pages()), 4)


class TestEndConfirmations(AutomationTestCase):
    def test_single_identical_frame_is_not_the_end(self):
        cfg = AppConfig()
        cfg.end_confirmations = 2
        a, b, c = make_page(1), make_page(2), make_page(3)
        win = FakeWin()
        # b se zopakuje jen jednou, pak dokument pokračuje stránkou c
        controller = self.run_controller(win, [a, b, b, c, c, c], config=cfg)
        self.assertEqual(len(controller.page_files), 3, "b se nesmí uložit dvakrát")
        self.assertIn("Detekován konec", controller.finished_reason)


class TestScrollKeys(unittest.TestCase):
    """Tabulka kláves posuvu."""

    def test_known_keys_have_virtual_codes(self):
        for name, (code, _extended) in wm.SCROLL_KEYS.items():
            self.assertGreater(code, 0, name)
            self.assertEqual(name, name.lower())

    def test_arrows_and_paging_are_extended(self):
        for name in ("pagedown", "pageup", "down", "up", "home", "end"):
            self.assertTrue(wm.SCROLL_KEYS[name][1], f"{name} musí být rozšířená")
        self.assertFalse(wm.SCROLL_KEYS["space"][1])

    def test_page_down_matches_the_dedicated_constant(self):
        self.assertEqual(wm.SCROLL_KEYS["pagedown"][0], wm.VK_NEXT)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            wm.send_key(0, "nesmysl")

    def test_default_key_exists(self):
        self.assertIn(wm.DEFAULT_SCROLL_KEY, wm.SCROLL_KEYS)


class TestScrollStep(unittest.TestCase):
    """Adaptivní krok posuvu."""

    def config(self, **kwargs):
        cfg = AppConfig()
        cfg.scroll_calibrate = True
        cfg.scroll_target_ratio = 0.85
        for key, value in kwargs.items():
            setattr(cfg, key, value)
        cfg.clamp()
        return cfg

    def test_starts_with_one_press_when_calibrating(self):
        step = automation.ScrollStep(self.config(), 1000)
        self.assertEqual(step.presses, 1)
        self.assertTrue(step.calibrating)

    def test_uses_configured_presses_without_calibration(self):
        cfg = self.config(scroll_calibrate=False, scroll_presses=7)
        step = automation.ScrollStep(cfg, 1000)
        self.assertEqual(step.presses, 7)
        self.assertIsNone(step.observe(123), "bez kalibrace se nic neupravuje")
        self.assertEqual(step.presses, 7)

    def test_scales_up_to_reach_the_target(self):
        step = automation.ScrollStep(self.config(), 1000)
        message = step.observe(40)  # jeden stisk = 40 px, cíl 850 px
        self.assertEqual(step.presses, 21)
        self.assertIn("40 px", message)

    def test_does_not_overshoot_the_cap(self):
        step = automation.ScrollStep(self.config(), 100000)
        step.observe(1)
        self.assertLessEqual(step.presses, automation.ScrollStep.MAX_PRESSES)

    def test_lost_overlap_halves_the_step(self):
        step = automation.ScrollStep(self.config(), 1000)
        step.observe(40)
        before = step.presses
        message = step.observe(None)
        self.assertEqual(step.presses, before // 2)
        self.assertIn("zkracuji", message)

    def test_single_press_without_overlap_stops_calibration(self):
        step = automation.ScrollStep(self.config(), 1000)
        message = step.observe(None)
        self.assertEqual(step.presses, 1)
        self.assertFalse(step.calibrating)
        self.assertIn("použitelný překryv", message)

    def test_step_is_determined_only_once(self):
        """Na konci stránky prohlížeč posuv utne – z toho se nesmí přepočítávat."""
        step = automation.ScrollStep(self.config(), 1000)
        step.observe(40)
        presses = step.presses
        self.assertIsNone(step.observe(5), "utnutý posuv nesmí krok nafouknout")
        self.assertEqual(step.presses, presses)
        self.assertIsNone(step.observe(40 * presses))
        self.assertEqual(step.presses, presses)

    def test_shrinking_is_allowed_after_calibration(self):
        step = automation.ScrollStep(self.config(), 1000)
        step.observe(40)
        presses = step.presses
        step.observe(None)
        self.assertEqual(step.presses, presses // 2)


class TestScrollInTheLoop(AutomationTestCase):
    def test_configured_key_and_count_are_sent(self):
        cfg = AppConfig()
        cfg.scroll_key = "down"
        cfg.scroll_presses = 5
        cfg.scroll_calibrate = False
        win = FakeWin()
        self.run_controller(win, [make_page(1), make_page(2), make_page(2), make_page(2)],
                            config=cfg)
        self.assertTrue(win.keys)
        for key, presses in win.keys:
            self.assertEqual(key, "down")
            self.assertEqual(presses, 5)

    def test_default_is_page_down_once(self):
        win = FakeWin()
        self.run_controller(win, [make_page(1), make_page(2), make_page(2), make_page(2)])
        self.assertTrue(win.keys)
        for key, presses in win.keys:
            self.assertEqual(key, "pagedown")
            self.assertEqual(presses, 1)

    def test_calibration_adjusts_the_number_of_presses(self):
        """Snímky s malým posuvem: krok se má natáhnout na cílovou hodnotu."""
        from helpers import make_text_page

        base = make_text_page(1, size=(200, 600))
        shots = [base.crop((0, y, 200, y + 150)) for y in (0, 20, 40, 60, 80)]
        cfg = AppConfig()
        cfg.scroll_calibrate = True
        cfg.scroll_target_ratio = 0.8
        win = FakeWin()
        targets_height = 150
        with mock.patch.object(
            automation.stitch, "find_shift", return_value=(20, 0.0)
        ):
            self.run_controller(win, shots, config=cfg)
        # jeden stisk = 20 px, cíl 0.8 * 150 = 120 px => 6 stisků
        self.assertEqual(win.keys[0][1], 1, "první krok kalibruje jediným stiskem")
        for _key, presses in win.keys[1:]:
            self.assertEqual(presses, 6, f"krok se měl ustálit, {win.keys}")
        self.assertEqual(targets_height, 150)


if __name__ == "__main__":
    unittest.main()

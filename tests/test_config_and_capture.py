"""Testy konfigurace, cest a validace snímané oblasti."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (nastaví sys.path na ./src)

import config as cfg_mod  # noqa: E402
from capture import CaptureError, Region, validate_region  # noqa: E402
from config import AppConfig, page_filename  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_defaults_match_specification(self):
        cfg = AppConfig()
        self.assertEqual(cfg.activation_delay_ms, 300)
        self.assertEqual(cfg.page_down_delay_ms, 700)
        self.assertEqual(cfg.end_confirmations, 2)
        self.assertEqual(cfg.max_screenshots, 1000)
        self.assertEqual(cfg.activation_attempts, 3)
        # Adresa RDP relace není předvyplněná – uživatel ji musí zadat sám.
        self.assertEqual(cfg.rdp_host, "")

    def test_clamp_fixes_nonsense(self):
        cfg = AppConfig(
            activation_attempts=0,
            end_confirmations=0,
            max_screenshots=-5,
            pixel_threshold=9.0,
            hash_threshold=999,
            page_down_method="magic",
            rdp_host="  ",
        )
        cfg.clamp()
        self.assertEqual(cfg.activation_attempts, 1)
        self.assertEqual(cfg.end_confirmations, 1)
        self.assertEqual(cfg.max_screenshots, 1)
        self.assertEqual(cfg.pixel_threshold, 1.0)
        self.assertEqual(cfg.hash_threshold, 64)
        self.assertEqual(cfg.page_down_method, "sendinput")
        self.assertEqual(cfg.rdp_host, "", "prázdná adresa je platný stav")

    def test_page_width_mm_defaults_to_disabled_and_clamps(self):
        self.assertEqual(AppConfig().pdf_page_width_mm, 0.0)
        cfg = AppConfig(pdf_page_width_mm=-10)
        cfg.clamp()
        self.assertEqual(cfg.pdf_page_width_mm, 0.0)
        cfg = AppConfig(pdf_page_width_mm="210")  # z config.json může přijít text
        cfg.clamp()
        self.assertEqual(cfg.pdf_page_width_mm, 210.0)

    def test_upscale_defaults_and_clamps(self):
        cfg = AppConfig()
        self.assertEqual(cfg.ocr_upscale, 2.0)
        self.assertEqual(cfg.pdf_upscale, 1.0)
        cfg = AppConfig(ocr_upscale=99, pdf_upscale=0.25)
        cfg.clamp()
        self.assertEqual(cfg.ocr_upscale, 4.0)
        self.assertEqual(cfg.pdf_upscale, 1.0, "zmenšovat nechceme")

    def test_rdp_host_is_kept_and_trimmed(self):
        cfg = AppConfig(rdp_host="  192.168.1.100  ")
        cfg.clamp()
        self.assertEqual(cfg.rdp_host, "192.168.1.100")

    def test_rdp_host_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cfg_mod, "get_working_dir", lambda: tmp):
                AppConfig(rdp_host="10.0.0.5").save()
                self.assertEqual(AppConfig.load().rdp_host, "10.0.0.5")

    def test_roundtrip_through_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cfg_mod, "get_working_dir", lambda: tmp):
                cfg = AppConfig(page_down_delay_ms=1234, end_confirmations=3)
                self.assertTrue(cfg.save())
                with open(os.path.join(tmp, "config.json"), encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh)["page_down_delay_ms"], 1234)
                loaded = AppConfig.load()
                self.assertEqual(loaded.page_down_delay_ms, 1234)
                self.assertEqual(loaded.end_confirmations, 3)

    def test_broken_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cfg_mod, "get_working_dir", lambda: tmp):
                with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as fh:
                    fh.write("{ tohle není JSON")
                self.assertEqual(AppConfig.load().page_down_delay_ms, 700)

    def test_page_filename_padding_keeps_order(self):
        names = [page_filename(i) for i in (1, 2, 10, 100, 1000)]
        self.assertEqual(names[0], "page_0001.png")
        self.assertEqual(names[-1], "page_1000.png")
        self.assertEqual(names, sorted(names))

    def test_session_dirs_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cfg_mod, "get_working_dir", lambda: tmp):
                first = cfg_mod.new_session_dir()
                second = cfg_mod.new_session_dir()
                self.assertNotEqual(first, second)
                self.assertTrue(os.path.isdir(first))
                self.assertTrue(os.path.isdir(second))


class TestRegionValidation(unittest.TestCase):
    def test_none_region(self):
        with self.assertRaises(CaptureError):
            validate_region(None)

    def test_zero_size(self):
        with self.assertRaises(CaptureError):
            validate_region(Region(10, 10, 0, 100))
        with self.assertRaises(CaptureError):
            validate_region(Region(10, 10, 100, -3))

    def test_region_outside_all_monitors(self):
        with self.assertRaises(CaptureError):
            validate_region(Region(500000, 500000, 100, 100))

    def test_valid_region_passes(self):
        validate_region(Region(0, 0, 50, 50))

    def test_negative_coordinates_are_allowed(self):
        """Monitor vlevo od primárního má záporné X."""
        with mock.patch("capture.virtual_screen_rect", lambda: (-1920, 0, 3840, 1080)):
            validate_region(Region(-1500, 100, 800, 600))


if __name__ == "__main__":
    unittest.main()

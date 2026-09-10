"""Generování .rdp souboru pro relaci s pevným rozlišením."""

from __future__ import annotations

import os
import tempfile
import unittest

import helpers  # noqa: F401  (nastaví sys.path na ./src)

import rdp_session as rdp  # noqa: E402


class TestRdpContent(unittest.TestCase):
    def content(self, host="192.168.30.10", width=2560, height=3600) -> dict[str, str]:
        raw = rdp.build_rdp_content(host, width, height)
        self.assertTrue(raw.endswith("\r\n"), "soubor má končit CRLF")
        values = {}
        for line in raw.strip().splitlines():
            key, _kind, value = line.split(":", 2)
            values[key] = value
        return values

    def test_resolution_is_written(self):
        values = self.content(width=2560, height=3600)
        self.assertEqual(values["desktopwidth"], "2560")
        self.assertEqual(values["desktopheight"], "3600")
        self.assertEqual(values["full address"], "192.168.30.10")

    def test_smart_sizing_and_dynamic_resolution_are_off(self):
        """Obojí by získané rozlišení zahodilo – viz README."""
        values = self.content()
        self.assertEqual(values["smart sizing"], "0")
        self.assertEqual(values["dynamic resolution"], "0")

    def test_windowed_mode(self):
        # Na celou obrazovku by okno nešlo zvětšit nad rámec monitoru.
        self.assertEqual(self.content()["screen mode id"], "1")

    def test_lan_profile_keeps_the_codec_sharp(self):
        values = self.content()
        self.assertEqual(values["compression"], "0")
        self.assertEqual(values["connection type"], "6")
        self.assertEqual(values["networkautodetect"], "0")
        self.assertEqual(values["bandwidthautodetect"], "0")

    def test_no_credentials_are_stored(self):
        raw = rdp.build_rdp_content("192.168.30.10", 2560, 3600).lower()
        for forbidden in ("password", "username", "domain"):
            self.assertNotIn(forbidden, raw)

    def test_empty_host_is_rejected(self):
        for host in ("", "   ", None):
            with self.assertRaises(rdp.RdpSessionError):
                rdp.build_rdp_content(host, 2560, 3600)

    def test_host_is_trimmed(self):
        self.assertEqual(
            self.content(host="  server.firma.local  ")["full address"],
            "server.firma.local",
        )


class TestClampResolution(unittest.TestCase):
    def test_values_are_clamped_to_supported_range(self):
        self.assertEqual(rdp.clamp_resolution(10, 10), (rdp.MIN_SIDE, rdp.MIN_SIDE))
        self.assertEqual(rdp.clamp_resolution(99999, 99999), (rdp.MAX_SIDE, rdp.MAX_SIDE))

    def test_sensible_values_pass_through(self):
        self.assertEqual(rdp.clamp_resolution(2560, 3600), (2560, 3600))

    def test_nonsense_falls_back_to_default(self):
        self.assertEqual(
            rdp.clamp_resolution("x", None), (rdp.DEFAULT_WIDTH, rdp.DEFAULT_HEIGHT)
        )


class TestWriteRdpFile(unittest.TestCase):
    def test_file_is_written_with_stable_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = rdp.write_rdp_file("192.168.30.10", 2560, 3600, folder)
            self.assertEqual(os.path.basename(path), rdp.RDP_FILENAME)
            with open(path, encoding="utf-8") as handle:
                self.assertIn("desktopheight:i:3600", handle.read())

    def test_rewrite_replaces_previous_content(self):
        with tempfile.TemporaryDirectory() as folder:
            rdp.write_rdp_file("192.168.30.10", 2560, 3600, folder)
            path = rdp.write_rdp_file("10.0.0.5", 1920, 2400, folder)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("full address:s:10.0.0.5", text)
            self.assertNotIn("192.168.30.10", text)
            self.assertIn("desktopheight:i:2400", text)


if __name__ == "__main__":
    unittest.main()

"""Minimální čtečka PDF pro testy.

Rozebere PDF vytvořené modulem `pdf_export`, dekóduje `/ToUnicode` CMap
a z obsahu stránek zrekonstruuje text neviditelné vrstvy. Díky tomu jde
ověřit, že text z PDF vyjde přesně tak, jak do něj vstoupil – bez závislosti
na externí knihovně.
"""

from __future__ import annotations

import re
import zlib

_OBJ = re.compile(rb"(\d+) 0 obj\n(.*?)\nendobj\n", re.DOTALL)
_STREAM = re.compile(rb"\nstream\n(.*?)\nendstream", re.DOTALL)
_BFCHAR_BLOCK = re.compile(rb"beginbfchar\n(.*?)endbfchar", re.DOTALL)
_BFCHAR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_HEX_TJ = re.compile(rb"<([0-9A-Fa-f]*)>\s*Tj")
_TM = re.compile(
    rb"/F1\s+([\d.]+)\s+Tf\s+([\d.]+)\s+Tz\s+1 0 0 1\s+([-\d.]+)\s+([-\d.]+)\s+Tm"
)


class PdfDocument:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.objects: dict[int, bytes] = {}
        self.streams: dict[int, bytes] = {}
        for match in _OBJ.finditer(data):
            obj_id = int(match.group(1))
            body = match.group(2)
            self.objects[obj_id] = body
            stream = _STREAM.search(body)
            if stream:
                raw = stream.group(1)
                if b"/FlateDecode" in body:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        pass
                self.streams[obj_id] = raw

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> "PdfDocument":
        with open(path, "rb") as fh:
            return cls(fh.read())

    def find_object(self, marker: bytes) -> tuple[int, bytes] | None:
        for obj_id, body in self.objects.items():
            if marker in body:
                return obj_id, body
        return None

    def page_ids(self) -> list[int]:
        pages = self.find_object(b"/Type /Pages")
        if not pages:
            return []
        kids = re.search(rb"/Kids \[(.*?)\]", pages[1])
        if not kids:
            return []
        return [int(n) for n in re.findall(rb"(\d+) 0 R", kids.group(1))]

    def page_content(self, page_id: int) -> bytes:
        body = self.objects[page_id]
        match = re.search(rb"/Contents (\d+) 0 R", body)
        if not match:
            return b""
        return self.streams.get(int(match.group(1)), b"")

    # ------------------------------------------------------------------
    def to_unicode_map(self) -> dict[int, str]:
        """Kód znaku -> Unicode, načteno z /ToUnicode CMap."""
        font = self.find_object(b"/BaseFont /Courier")
        if not font:
            return {}
        ref = re.search(rb"/ToUnicode (\d+) 0 R", font[1])
        if not ref:
            return {}
        cmap = self.streams.get(int(ref.group(1)), b"")
        mapping: dict[int, str] = {}
        for block in _BFCHAR_BLOCK.finditer(cmap):
            for match in _BFCHAR.finditer(block.group(1)):
                code = int(match.group(1), 16)
                raw = bytes.fromhex(match.group(2).decode("ascii"))
                mapping[code] = raw.decode("utf-16-be")
        return mapping

    def differences_map(self) -> dict[int, str]:
        """Kód znaku -> Unicode, načteno z /Differences (jména uniXXXX)."""
        font = self.find_object(b"/BaseFont /Courier")
        if not font:
            return {}
        match = re.search(rb"/Differences \[(.*?)\]", font[1], re.DOTALL)
        if not match:
            return {}
        tokens = match.group(1).split()
        mapping: dict[int, str] = {}
        code = 0
        for token in tokens:
            if token.isdigit():
                code = int(token)
                continue
            name = token.decode("ascii")
            if name.startswith("/uni"):
                mapping[code] = chr(int(name[4:], 16))
            code += 1
        return mapping

    def page_words(self, page_index: int, mapping: dict[int, str] | None = None) -> list[str]:
        """Slova neviditelné textové vrstvy jedné stránky."""
        if mapping is None:
            mapping = self.to_unicode_map()
        ids = self.page_ids()
        if page_index >= len(ids):
            return []
        content = self.page_content(ids[page_index])
        words = []
        for match in _HEX_TJ.finditer(content):
            codes = bytes.fromhex(match.group(1).decode("ascii"))
            words.append("".join(mapping.get(code, "�") for code in codes))
        return words

    def page_text_positions(self, page_index: int) -> list[tuple[float, float, float, float]]:
        """(velikost, Tz, x, y) pro každé slovo stránky."""
        ids = self.page_ids()
        if page_index >= len(ids):
            return []
        content = self.page_content(ids[page_index])
        return [
            (float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)))
            for m in _TM.finditer(content)
        ]

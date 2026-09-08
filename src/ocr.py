"""OCR pomocí enginu vestavěného ve Windows (`Windows.Media.Ocr`).

Engine je součástí Windows 10/11, takže se nic nestahuje ani neinstaluje.
Dostupné jazyky odpovídají jazykovým balíčkům nainstalovaným ve Windows
(`Nastavení → Čas a jazyk → Jazyk a oblast`).

WinRT API se volá přes krátký PowerShell skript – ten je vložen přímo v tomto
souboru, takže funguje i ze sestaveného `.exe` bez dalších datových souborů.
Všechny stránky se zpracují v JEDNOM procesu PowerShellu; průběh hlásí skript
řádky `PROGRESS i n` na standardní výstup.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Sequence

MAX_IMAGE_DIMENSION = 10000  # limit Windows.Media.Ocr
CREATE_NO_WINDOW = 0x08000000

# PowerShell 5.1 je na Windows 10/11 vždy k dispozici a s WinRT pracuje
# spolehlivěji než pwsh 7.
_SYSTEM_POWERSHELL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)


class OcrError(RuntimeError):
    """OCR se nepodařilo provést. Nikdy nesmí zabránit vytvoření PDF."""


@dataclass(frozen=True)
class Word:
    """Jedno rozpoznané slovo. Souřadnice jsou pixely vstupního obrázku."""

    text: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class PageText:
    width: int
    height: int
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


# ---------------------------------------------------------------------------
# PowerShell skript
# ---------------------------------------------------------------------------
_SCRIPT = r'''
param(
    [Parameter(Mandatory = $true)][string]$Mode,
    [string]$InputJson = "",
    [string]$OutputJson = ""
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $null = $netTask.Wait(-1)
    $netTask.Result
}

$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

function Esc([string]$s) {
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $s.ToCharArray()) {
        $code = [int]$ch
        if ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($ch -eq '\') { [void]$sb.Append('\\') }
        elseif ($code -lt 0x20) { [void]$sb.AppendFormat('\u{0:x4}', $code) }
        else { [void]$sb.Append($ch) }
    }
    $sb.ToString()
}

function Write-Utf8([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

if ($Mode -eq "languages") {
    $tags = @()
    foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
        $tags += '"' + (Esc $l.LanguageTag) + '"'
    }
    Write-Utf8 $OutputJson ("{""languages"":[" + ($tags -join ",") + "]}")
    exit 0
}

$request = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 | ConvertFrom-Json
$language = $request.language
$images = @($request.images)

$engine = $null
if ($language) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new($language))
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) {
    Write-Utf8 $OutputJson '{"error":"Ve Windows neni k dispozici zadny jazyk pro OCR."}'
    exit 2
}

$sb = New-Object System.Text.StringBuilder
[void]$sb.Append('{"pages":[')

$index = 0
$total = $images.Count
foreach ($path in $images) {
    if ($index -gt 0) { [void]$sb.Append(',') }
    $index++
    Write-Output ("PROGRESS {0} {1}" -f $index, $total)
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

        [void]$sb.AppendFormat('{{"width":{0},"height":{1},"words":[',
            $bitmap.PixelWidth, $bitmap.PixelHeight)
        $first = $true
        foreach ($line in $result.Lines) {
            foreach ($w in $line.Words) {
                if (-not $first) { [void]$sb.Append(',') }
                $first = $false
                $r = $w.BoundingRect
                [void]$sb.AppendFormat(
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    '{{"t":"{0}","x":{1:0.##},"y":{2:0.##},"w":{3:0.##},"h":{4:0.##}}}',
                    (Esc $w.Text), $r.X, $r.Y, $r.Width, $r.Height)
            }
        }
        [void]$sb.Append(']}')
        $bitmap.Dispose()
        $stream.Dispose()
    }
    catch {
        [void]$sb.AppendFormat('{{"error":"{0}"}}', (Esc $_.Exception.Message))
    }
}

[void]$sb.Append(']}')
Write-Utf8 $OutputJson $sb.ToString()
exit 0
'''


# ---------------------------------------------------------------------------
def _powershell_exe() -> str:
    return _SYSTEM_POWERSHELL if os.path.isfile(_SYSTEM_POWERSHELL) else "powershell.exe"


def _run(
    mode: str,
    workdir: str,
    payload: dict | None,
    timeout: float,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    script_path = os.path.join(workdir, "ocr_winrt.ps1")
    with open(script_path, "w", encoding="utf-8-sig") as fh:
        fh.write(_SCRIPT)

    output_path = os.path.join(workdir, "ocr_out.json")
    command = [
        _powershell_exe(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script_path,
        "-Mode",
        mode,
        "-OutputJson",
        output_path,
    ]
    if payload is not None:
        input_path = os.path.join(workdir, "ocr_in.json")
        with open(input_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        command += ["-InputJson", input_path]

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise OcrError(f"PowerShell se nepodařilo spustit: {exc}") from exc

    try:
        if on_progress is not None and process.stdout is not None:
            for line in process.stdout:
                parts = line.strip().split()
                if len(parts) == 3 and parts[0] == "PROGRESS":
                    try:
                        on_progress(int(parts[1]), int(parts[2]))
                    except ValueError:
                        pass
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise OcrError("OCR překročilo časový limit.")

    if not os.path.isfile(output_path):
        detail = (stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"návratový kód {process.returncode}"
        raise OcrError(f"OCR selhalo: {message}")

    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OcrError(f"Výstup OCR se nepodařilo přečíst: {exc}") from exc

    if isinstance(data, dict) and data.get("error"):
        raise OcrError(str(data["error"]))
    return data


# ---------------------------------------------------------------------------
def available_languages(timeout: float = 60.0) -> list[str]:
    """Jazykové značky, které umí OCR engine Windows (např. ['cs', 'en-GB'])."""
    with tempfile.TemporaryDirectory(prefix="rdpscraper_ocr_") as workdir:
        data = _run("languages", workdir, None, timeout)
    return [str(tag) for tag in data.get("languages", [])]


def is_available(language: str | None = None) -> bool:
    """True, pokud lze OCR použít (volitelně pro konkrétní jazyk)."""
    try:
        tags = available_languages()
    except OcrError:
        return False
    if not tags:
        return False
    if not language:
        return True
    wanted = language.strip().lower()
    return any(tag.lower() == wanted or tag.lower().startswith(wanted + "-") for tag in tags)


def recognize(
    image_paths: Sequence[str],
    language: str = "cs",
    on_progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[PageText | None]:
    """Rozpozná text ve snímcích. Vrací seznam stejné délky jako `image_paths`.

    Položka je `None`, pokud se stránku nepodařilo zpracovat – zbytek dokumentu
    tím není dotčen.
    """
    paths = [os.path.abspath(p) for p in image_paths]
    if not paths:
        return []

    payload = {"language": language or "", "images": paths}
    timeout = 120.0 + 20.0 * len(paths)

    with tempfile.TemporaryDirectory(prefix="rdpscraper_ocr_") as workdir:
        data = _run("recognize", workdir, payload, timeout, on_progress)

    pages_raw = data.get("pages", [])
    results: list[PageText | None] = []
    for index in range(len(paths)):
        if index >= len(pages_raw):
            results.append(None)
            continue
        entry = pages_raw[index] or {}
        if entry.get("error"):
            if log:
                log(f"OCR stránky {index + 1} selhalo: {entry['error']}")
            results.append(None)
            continue
        words = tuple(
            Word(
                text=str(item.get("t", "")),
                x=float(item.get("x", 0.0)),
                y=float(item.get("y", 0.0)),
                width=float(item.get("w", 0.0)),
                height=float(item.get("h", 0.0)),
            )
            for item in entry.get("words", [])
            if str(item.get("t", ""))
        )
        results.append(
            PageText(
                width=int(entry.get("width", 0)),
                height=int(entry.get("height", 0)),
                words=words,
            )
        )
    return results

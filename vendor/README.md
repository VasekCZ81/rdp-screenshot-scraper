# vendor/ – offline závislosti

Tento adresář obsahuje všechna kolečka (`.whl`), která projekt potřebuje.
Díky nim proběhne instalace i sestavení `.exe` **bez připojení k internetu**:

```bat
build.bat
```

`build.bat` instaluje výhradně odsud (`pip install --no-index --find-links vendor`).
Nic se nestahuje.

## Obsah

| balíček | k čemu |
|---|---|
| `mss` | pořizování screenshotů oblasti obrazovky |
| `pillow` | práce s obrázky a PNG |
| `pyinstaller`, `pyinstaller-hooks-contrib` | sestavení `.exe` |
| `altgraph`, `pefile`, `pywin32-ctypes`, `packaging`, `setuptools` | závislosti PyInstalleru |

Aplikace samotná za běhu potřebuje pouze `mss` a `Pillow`. Zbytek je jen pro build.
Práce s okny (Win32 API) je řešena přes `ctypes` ze standardní knihovny,
`pywin32` proto přiložený není a není potřeba.

## Pro jaké prostředí kolečka platí

* **64bitové Windows** (`win_amd64`),
* **Python 3.10 až 3.14** – jediný balíček vázaný na verzi Pythonu je `Pillow`,
  proto je přiložen pro každou z těchto verzí. Ostatní kolečka jsou
  univerzální (`py3-none-any`, resp. `py3-none-win_amd64`).

Verzi Pythonu na cílovém počítači zjistíte příkazem:

```bat
python --version
```

## Doplnění dalšího prostředí

Pokud cílový počítač má jinou verzi Pythonu nebo 32bitový Python, stáhněte
odpovídající kolečko na počítači s internetem a zkopírujte je sem:

```bat
pip download -d vendor --only-binary=:all: --no-deps ^
    --platform win_amd64 --python-version 3.15 Pillow==12.3.0
```

Pro 32bitový Python použijte `--platform win32` a stáhněte i
`pyinstaller` pro danou platformu:

```bat
pip download -d vendor --only-binary=:all: --no-deps ^
    --platform win32 --python-version 3.12 Pillow==12.3.0 pyinstaller==6.22.2
```

## Aktualizace všech závislostí

Na počítači s internetem, po úpravě verzí v `requirements.txt`:

```bat
pip download -d vendor --only-binary=:all: -r requirements.txt
```

Verze v `requirements.txt` jsou připnuté přesně (`==`), aby build byl
opakovatelný a odpovídal obsahu tohoto adresáře.

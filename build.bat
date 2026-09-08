@echo off
rem ---------------------------------------------------------------
rem  Sestaveni RdpScreenshotScraper.exe (Windows, jeden prikaz)
rem
rem  Bezi zcela OFFLINE - vsechna potrebna kolecka jsou v adresari
rem  vendor\. Internetove pripojeni neni potreba.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PIPFLAGS=--disable-pip-version-check --no-python-version-warning"
set "VENDOR=%CD%\vendor"

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Vytvarim virtualni prostredi .venv ...
    python -m venv .venv
    if errorlevel 1 goto :error
) else (
    echo [1/4] Virtualni prostredi .venv jiz existuje.
)
set "PY=%CD%\.venv\Scripts\python.exe"

if exist "%VENDOR%" (
    echo [2/4] Instaluji zavislosti z vendor\ ^(offline^) ...
    "%PY%" -m pip install %PIPFLAGS% --no-index --find-links "%VENDOR%" -r requirements.txt
    if errorlevel 1 goto :vendor_error
) else (
    echo [2/4] Adresar vendor\ nenalezen - zkousim instalaci z internetu ...
    "%PY%" -m pip install %PIPFLAGS% -r requirements.txt
    if errorlevel 1 goto :error
)

echo [3/4] Spoustim testy ...
set "PYTHONPATH=%CD%\src;%CD%\tests"
"%PY%" -m unittest discover -s tests -t tests
if errorlevel 1 goto :error

echo [4/4] Sestavuji .exe ...
"%PY%" -m PyInstaller --noconfirm --clean RdpScreenshotScraper.spec
if errorlevel 1 goto :error

echo.
echo ================================================================
echo  Hotovo: dist\RdpScreenshotScraper.exe
echo ================================================================
echo.
goto :eof

:vendor_error
echo.
echo *** Instalace z vendor\ selhala ***
echo.
echo Nejcastejsi pricina: tento pocitac ma jinou verzi Pythonu, nez pro
echo kterou je v adresari vendor\ pripravene kolecko knihovny Pillow.
echo Prilozena jsou kolecka pro 64bitovy Python 3.10 az 3.14.
echo Zjistete verzi prikazem:  python --version
echo Podrobnosti a postup doplneni najdete v souboru vendor\README.md
exit /b 1

:error
echo.
echo *** BUILD SELHAL ***
exit /b 1

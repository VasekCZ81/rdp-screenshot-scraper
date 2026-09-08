@echo off
rem ---------------------------------------------------------------
rem  Spusteni aplikace primo ze zdrojovych kodu (bez sestaveni .exe).
rem  Bezi offline - zavislosti se instaluji z adresare vendor\.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PIPFLAGS=--disable-pip-version-check --no-python-version-warning"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Pripravuji virtualni prostredi ...
    python -m venv .venv
    if errorlevel 1 goto :error
    if exist "%CD%\vendor" (
        ".venv\Scripts\python.exe" -m pip install %PIPFLAGS% --no-index --find-links "%CD%\vendor" mss Pillow
    ) else (
        ".venv\Scripts\python.exe" -m pip install %PIPFLAGS% mss Pillow
    )
    if errorlevel 1 goto :error
)

start "" ".venv\Scripts\pythonw.exe" "src\main.py"
goto :eof

:error
echo.
echo *** Priprava prostredi selhala ***
pause
exit /b 1

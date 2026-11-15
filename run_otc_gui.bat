@echo off
rem Double-click to start OTC Assistant.
rem Keep this file in the same folder as the app's .py files and requirements.txt.
setlocal
cd /d "%~dp0"

rem ---- Find Python 3.11 or newer (the app's server needs it) ----
rem The "py" launcher finds every Python installed, wherever it is: the newest of these first,
rem then its default, then "python" on the PATH. An older Python (3.7...) is passed over.
set "PY="
set "PYW="
set "NEED=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
for %%V in (3.14 3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "%NEED%" >nul 2>nul && (
            set "PY=py -%%V"
            set "PYW=pyw -%%V"
        )
    )
)
if not defined PY (
    py -c "%NEED%" >nul 2>nul && (
        set "PY=py"
        set "PYW=pyw"
    )
)
if not defined PY (
    python -c "%NEED%" >nul 2>nul && (
        set "PY=python"
        set "PYW=pythonw"
    )
)
if not defined PY (
    echo OTC Assistant needs Python 3.11 or newer. Found on this PC, if any:
    py -0p 2>nul
    python --version 2>nul
    echo.
    echo Install the newest Python from https://www.python.org/downloads/
    echo keeping "py launcher" ticked, or ticking "Add python.exe to PATH", then try again.
    echo An older Python can stay installed: the newer one is used.
    echo.
    pause
    exit /b 1
)

rem ---- Install the required packages on first run ----
%PY% -c "import PyQt6.QtWidgets, tzdata, numpy, schwab" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages, first run only. This takes a minute...
    echo.
    %PY% -m pip install --upgrade -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Package installation failed. See the messages above.
        pause
        exit /b 1
    )
)

rem ---- Make sure the app files are here ----
if not exist "otc_daily_gui.py" goto missing
if not exist "otc_daily.py" goto missing
if not exist "otc_chart.py" goto missing
if not exist "otc_zones.py" goto missing
if not exist "otc_atm.py" goto missing
if not exist "otc_level2.py" goto missing
if not exist "otc_checklist.py" goto missing
if not exist "otc_intraday.py" goto missing
if not exist "checklist.txt" goto missing

rem ---- First run only: put a shortcut with the app icon on the desktop ----
rem (Delete shortcut_created.txt to have it created again.)
if exist "%~dp0shortcut_created.txt" goto shortcut_done
if not exist "%~dp0app_icon.ico" goto shortcut_done
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'OTC Assistant.lnk')); $s.TargetPath='%~dp0run_otc_gui.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0app_icon.ico,0'; $s.WindowStyle=7; $s.Description='OTC Assistant'; $s.Save()" >nul 2>nul
if not errorlevel 1 echo Desktop shortcut created on first run.> "%~dp0shortcut_created.txt"
:shortcut_done

rem ---- Start the app without leaving a console window open ----
start "" %PYW% "%~dp0otc_daily_gui.py"
exit /b 0

:missing
echo otc_daily_gui.py, otc_daily.py, otc_chart.py, otc_zones.py, otc_atm.py, otc_level2.py, otc_checklist.py, otc_intraday.py and checklist.txt must be in the same folder as this .bat file.
pause
exit /b 1

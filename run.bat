@echo off
setlocal enabledelayedexpansion
title Backlink Generator
cd /d "%~dp0"

REM Pin host + port so the server and the browser always agree, no matter what a
REM stray PORT environment variable (set by some dev tools) says. web_server.py
REM checks BACKLINK_PORT first, so this wins.
set "BACKLINK_HOST=127.0.0.1"
set "BACKLINK_PORT=8000"

echo.
echo  ============================================================
echo     BACKLINK GENERATOR
echo  ============================================================
echo.

REM ==== 1. Find a suitable Python (3.10-3.13); auto-install 3.12 if missing ====
call :detect_python
if not defined PYCMD (
  echo  Python was not found on this computer.
  echo  Installing Python 3.12 automatically now ^(no admin rights needed^)...
  echo.
  call :install_python
  call :detect_python
)
if not defined PYCMD (
  echo.
  echo  [X] Python could not be installed automatically.
  echo.
  echo  Please install Python 3.12 by hand from:
  echo      https://www.python.org/downloads/release/python-3127/
  echo  On the first screen, TICK "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

for /f "tokens=*" %%v in ('%PYCMD% --version 2^>^&1') do set "PYVER=%%v"
echo  Using %PYVER%
echo.

REM ==== 2. Create the private environment; rebuild it if it looks broken ====
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" --version >nul 2>&1
  if errorlevel 1 (
    echo  The existing environment looks broken - rebuilding it...
    rmdir /s /q ".venv" >nul 2>&1
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo  First-time setup: creating a private environment...
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo  [X] Could not create the environment. See the message above.
    pause
    exit /b 1
  )
)

set "PY=.venv\Scripts\python.exe"

REM ==== 3. Install / repair packages ====
REM Uses goto-label flow (not parenthesised if-blocks) because the Python one-liner
REM and the pip echoes contain "(" ")" which would prematurely close an if-block.
REM Reinstall when: no marker yet, OR a required package fails to import (self-heals
REM an out-of-sync environment), OR requirements.txt changed since the last install.
set "NEED_INSTALL="
if not exist ".venv\.setup_done" set "NEED_INSTALL=1"

set "REQHASH="
for /f "skip=1 delims=" %%h in ('certutil -hashfile requirements.txt SHA1 2^>nul') do if not defined REQHASH set "REQHASH=%%h"

if defined NEED_INSTALL goto :do_install

REM Verify the key packages import; repair the environment if any are missing.
"%PY%" -c "import importlib.util as u,sys;sys.exit(0 if all(u.find_spec(x) for x in ('fastapi','uvicorn','pydantic','playwright','numpy','cv2','easyocr','PIL')) else 1)" >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"
if defined NEED_INSTALL echo  Some components are missing - repairing the environment...
if defined NEED_INSTALL goto :do_install

REM Deps import fine: only reinstall if requirements.txt actually changed vs the last
REM recorded hash. If none was recorded yet, just record it (don't rebuild a working env).
set "STOREDHASH="
if exist ".venv\.reqhash" set /p STOREDHASH=<".venv\.reqhash"
if not defined STOREDHASH goto :record_hash
if "!REQHASH!"=="!STOREDHASH!" goto :run
echo  requirements.txt changed - updating packages...
goto :do_install

:record_hash
>".venv\.reqhash" echo !REQHASH!
goto :run

:do_install
echo.
echo  Installing / updating components. The FIRST run downloads ~1-2 GB and can
echo  take 10-20 minutes. Please keep this window open. Later runs start instantly.
echo.
echo  [1/4] Updating installer tools ^(pip, setuptools, wheel^)...
"%PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :installfail
echo.
echo  [2/4] Installing PyTorch ^(CPU build - this is the big one^)...
"%PY%" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto :installfail
echo.
echo  [3/4] Installing the remaining packages...
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :installfail
echo.
echo  [4/4] Installing the browser engine...
"%PY%" -m playwright install chromium
if errorlevel 1 goto :installfail
echo done> ".venv\.setup_done"
>".venv\.reqhash" echo !REQHASH!
echo.
echo  Setup complete.

goto :run

:installfail
echo.
echo  ============================================================
echo  [X] Setup could not finish. The error is in the text ABOVE.
echo  ============================================================
echo.
echo  Most common fixes:
echo    1. Check you have internet access and ~2 GB free disk space.
echo    2. Delete the ".venv" folder in this project, then run this file again.
echo.
echo  If it still fails, copy the red error text above and send it over.
echo.
pause
exit /b 1

:run
REM Warn (don't block) if the port is already taken - usually the app is already open.
netstat -ano | findstr /R /C:":%BACKLINK_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo  [!] Port %BACKLINK_PORT% is already in use - the app may already be running.
  echo      If the page doesn't load, close the other app window and run this again.
  echo.
)

REM ==== 4. Open the app in the browser shortly after the server starts ====
start "Open Backlink Generator" cmd /c "timeout /t 6 >nul & start http://localhost:%BACKLINK_PORT%"

echo.
echo  Starting the app...
echo  Your browser will open at  http://localhost:%BACKLINK_PORT%
echo.
echo  Keep THIS window open while you use the app.
echo  To stop the app: close this window (or press Ctrl+C).
echo.

"%PY%" web_server.py

echo.
echo  The app has stopped.
echo  ^(If it closed immediately with a red error above, copy that text and send it over.^)
pause
exit /b 0


REM ============================================================
REM  Subroutines
REM ============================================================

:detect_python
REM Sets PYCMD to a launcher/path for a Python 3.10-3.13 interpreter, or leaves it empty.
set "PYCMD="
py -3.12 --version >nul 2>&1 && ( set "PYCMD=py -3.12" & goto :eof )
py -3.11 --version >nul 2>&1 && ( set "PYCMD=py -3.11" & goto :eof )
py -3.13 --version >nul 2>&1 && ( set "PYCMD=py -3.13" & goto :eof )
py -3.10 --version >nul 2>&1 && ( set "PYCMD=py -3.10" & goto :eof )
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" ( set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe" & goto :eof )
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" ( set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe" & goto :eof )
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" ( set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe" & goto :eof )
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" ( set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe" & goto :eof )
REM Last resort: a generic "python" on PATH, but only if it is 3.10-3.13.
where python >nul 2>&1 || goto :eof
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "GVER=%%v"
for /f "tokens=1,2 delims=." %%a in ("!GVER!") do ( set "GMAJ=%%a" & set "GMIN=%%b" )
if "!GMAJ!"=="3" if !GMIN! GEQ 10 if !GMIN! LEQ 13 set "PYCMD=python"
goto :eof

:install_python
REM Try Windows Package Manager (winget) first, then the official python.org installer.
where winget >nul 2>&1 && (
  echo  [a] Installing via Windows Package Manager ^(winget^)...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-source-agreements --accept-package-agreements
  call :detect_python
)
if defined PYCMD ( echo  Python installed successfully. & goto :eof )

echo  [b] Downloading the official Python 3.12 installer from python.org...
set "PYINST=%TEMP%\python-3.12.7-amd64.exe"
if exist "%PYINST%" del "%PYINST%" >nul 2>&1
curl -L -o "%PYINST%" "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" 2>nul
if not exist "%PYINST%" powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%PYINST%' } catch { exit 1 }"
if not exist "%PYINST%" ( echo  [X] Could not download the Python installer. & goto :eof )

echo  Installing Python 3.12 ^(per-user, no admin needed^) - about a minute...
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
del "%PYINST%" >nul 2>&1
call :detect_python
goto :eof

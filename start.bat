@echo off
setlocal enableextensions
title AI Learning Tutor - Launcher

REM Silent mode (set by start.vbs) and /nobrowser flag
set "SILENT=0"
set "NOBROWSER=0"
if /i "%~1"=="/silent"    set "SILENT=1"
if /i "%~2"=="/silent"    set "SILENT=1"
if /i "%~1"=="/nobrowser" set "NOBROWSER=1"
if /i "%~2"=="/nobrowser" set "NOBROWSER=1"

REM ============================================================
REM  AI Learning Tutor - one click local launcher
REM  - Double-click start.vbs for a fully silent launch (no console window)
REM  - Double-clicking start.bat directly also works, but shows this launcher text
REM  - Closing this window does NOT stop services and does NOT erase data
REM  - Data lives in: backend\tutor.db  and  data\storage\<user_id>\
REM ============================================================

REM ---------- 1. Resolve paths from this script location ----------
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BE=%ROOT%\backend"
set "FE=%ROOT%\frontend"
set "DATA=%ROOT%\data"
set "DBFILE=%ROOT%\backend\tutor.db"
set "PYSITE=%ROOT%\.venv\Scripts\python.exe"

echo.
echo ============================================================
echo   AI Learning Tutor - local launcher
echo ============================================================
echo   Project : %ROOT%
echo.

REM ---------- 2. Preflight checks ----------
if not exist "%PYSITE%" goto :err_python
if not exist "%BE%\app\main.py" goto :err_layout
if not exist "%FE%\node_modules\next\dist\bin\next" goto :err_frontend

REM ---------- 3. Locate Node.js ----------
set "PF86=%ProgramFiles(x86)%"
set "NODE_DIR="
call :probe_node "D:\nodejs"
call :probe_node "%ProgramFiles%\nodejs"
call :probe_node "%PF86%\nodejs"
call :probe_node "%LOCALAPPDATA%\Programs\nodejs"
if not defined NODE_DIR for /f "delims=" %%N in ('where node 2^>nul') do call :probe_node "%%~dpN"
if not defined NODE_DIR goto :err_node

REM Give child windows this project venv python + detected node first
set "PATH=%ROOT%\.venv\Scripts;%NODE_DIR%;%PATH%"

REM Local ffmpeg lives in tools\ffmpeg\bin only (never the system PATH).
REM If the binaries are missing, download a copy there so video ingest does
REM not die with WinError 2 / "系统找不到指定的文件".
if not exist "%ROOT%\tools\ffmpeg\bin\ffmpeg.exe" (
  echo   [ffmpeg] local copy missing, downloading into tools\ffmpeg\bin ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\bootstrap_ffmpeg.ps1"
)
if exist "%ROOT%\tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%ROOT%\tools\ffmpeg\bin;%PATH%"

REM ---------- 4. Runtime environment (all data is persisted on disk) ----------
if not exist "%DATA%" mkdir "%DATA%" >nul 2>&1
if not exist "%DATA%\storage" mkdir "%DATA%\storage" >nul 2>&1
if not exist "%ROOT%\tmp" mkdir "%ROOT%\tmp" >nul 2>&1

call :tofwds "%DBFILE%"
set "DATABASE_URL=sqlite+aiosqlite:///%FWDS%"
set "DATABASE_URL_SYNC=sqlite:///%FWDS%"
set "TASK_BACKEND=inline"
set "STORAGE_BACKEND=local"
REM NOTE: do NOT set LOCAL_STORAGE_PATH here. Its code default is already
REM "%DATA%\storage", and a process environment variable would PIN it, which
REM would make the storage-location field on /settings/llm read-only. Leave it
REM to .env / the settings page.
REM NOTE: do NOT set LLM_DEFAULT_PROVIDER / LLM_PROVIDER_* here.
REM This process environment overrides the .env file, so pinning it to "mock"
REM would silently disable the real LLM. Routing is governed by .env instead.
REM NOTE: STT_PROVIDER is left to .env (mock | faster_whisper | siliconflow).
REM Do not pin it here. ffmpeg and the venv python stay on THIS session PATH
REM only — never the system PATH.
set "RAG_PROVIDER=pgvector"
REM OCR: reading images / scanned PDFs. "auto" uses the offline ONNX engine when
REM it is installed, otherwise falls back to a vision LLM. OCR_LANG=auto switches
REM to the Japanese model automatically when kana come out at low confidence.
REM Leave these unset to use the defaults; override in .env when needed.
REM   set "OCR_PROVIDER=auto"
REM   set "OCR_LANG=auto"
set "SECRET_KEY=dev-secret-change-me-use-at-least-32-bytes"
set "CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000"
set "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000"

REM ---------- 5. Rebuild frontend when src is newer than `next start` bundle ----------
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\frontend_build_needed.ps1" -Root "%ROOT%"
if errorlevel 1 (
  echo   [build] frontend source is newer than .next; running npm run build ...
  pushd "%FE%"
  call "%NODE_DIR%\npm.cmd" run build
  if errorlevel 1 (
    popd
    echo   [ERROR] npm run build failed. The UI would keep last week's JavaScript.
    goto :halt
  )
  popd
)

REM ---------- 6. Recycle stale :8000/:3000 after a code or .env drop ----------
REM Reusing a listener that answers /health used to keep last week's uvicorn
REM (the [deepseek / deepseek-chat] error stamp) after a pull. Compare a
REM fingerprint of backend/app + frontend/src + .env + start.bat + BUILD_ID.
set "WANTED="
set "HAVE="
set "RELOAD=0"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\code_stamp.ps1" -Root "%ROOT%"`) do set "WANTED=%%S"
if exist "%ROOT%\tmp\launcher.stamp" set /p HAVE=<"%ROOT%\tmp\launcher.stamp"
if not defined WANTED set "RELOAD=1"
if /i not "%WANTED%"=="%HAVE%" set "RELOAD=1"
if "%RELOAD%"=="1" (
  echo   [reload] code or config on disk is newer than the running services
  echo            recycling ports 8000 and 3000 so this drop loads
  call :kill_port 8000
  call :kill_port 3000
  timeout /t 2 /nobreak >nul 2>&1
)

REM ---------- 7. Start backend ----------
call :port_busy 8000
if "%BUSY%"=="1" goto :be_running
echo   [start] backend api on port 8000 ... (logs: tmp\backend8000.log)
powershell -NoProfile -Command "Start-Process -FilePath '%PYSITE%' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' -WorkingDirectory '%BE%' -WindowStyle Hidden -RedirectStandardOutput '%ROOT%\tmp\backend8000.log' -RedirectStandardError '%ROOT%\tmp\backend8000.err.log'"
goto :be_done
:be_running
if "%RELOAD%"=="1" goto :err_stale_port_8000
REM Stamp matched: same drop as last launch. Reuse only if /health answers.
echo   [reuse] port 8000 is busy, checking it answers /health ...
set /a HCHK=0
:be_health
curl -f -s -o NUL -m 3 "http://127.0.0.1:8000/health" >nul 2>&1
if not errorlevel 1 goto :be_healthy
set /a HCHK+=1
if %HCHK% GEQ 10 goto :be_unhealthy
timeout /t 1 /nobreak >nul 2>&1
goto :be_health
:be_healthy
echo   [reuse] existing backend answered /health and matches this drop
goto :be_done
:be_unhealthy
echo   [reload] port 8000 is busy but /health failed; recycling it
call :kill_port 8000
timeout /t 2 /nobreak >nul 2>&1
call :port_busy 8000
if "%BUSY%"=="1" goto :err_stale_port_8000
echo   [start] backend api on port 8000 ... (logs: tmp\backend8000.log)
powershell -NoProfile -Command "Start-Process -FilePath '%PYSITE%' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' -WorkingDirectory '%BE%' -WindowStyle Hidden -RedirectStandardOutput '%ROOT%\tmp\backend8000.log' -RedirectStandardError '%ROOT%\tmp\backend8000.err.log'"
goto :be_done
:be_done

REM ---------- 8. Start frontend ----------
call :port_busy 3000
if "%BUSY%"=="1" goto :fe_running
echo   [start] frontend on port 3000 ... (logs: tmp\frontend3000.log)
powershell -NoProfile -Command "Start-Process -FilePath '%NODE_DIR%\node.exe' -ArgumentList 'node_modules\next\dist\bin\next','start','-p','3000' -WorkingDirectory '%FE%' -WindowStyle Hidden -RedirectStandardOutput '%ROOT%\tmp\frontend3000.log' -RedirectStandardError '%ROOT%\tmp\frontend3000.err.log'"
goto :fe_done
:fe_running
if "%RELOAD%"=="1" goto :err_stale_port_3000
echo   [reuse] port 3000 already listening and matches this drop
goto :fe_done
:fe_done

if defined WANTED (
  >"%ROOT%\tmp\launcher.stamp" echo %WANTED%
)

REM ---------- 9. Wait until both are ready ----------
echo.
call :wait_url "http://127.0.0.1:8000/health" "backend api"
call :wait_url "http://127.0.0.1:3000/" "frontend ui"

REM ---------- 10. Open the browser ----------
echo.
if "%NOBROWSER%"=="1" goto :no_browser
echo   [open] browser http://localhost:3000
start "" "http://localhost:3000"
goto :done
:no_browser
echo   [skip] /nobrowser given, browser not opened
:done

echo.
echo ============================================================
echo   Ready
echo ============================================================
echo   App      : http://localhost:3000
echo   API docs : http://127.0.0.1:8000/docs
echo.
echo   Data     : backend\tutor.db   (accounts, sources, quizzes)
echo              data\storage\<id>\ (profile / records / resources)
echo   Closing this window keeps services and data intact.
echo   To stop, run stop.bat (services now run hidden).
echo   A later start.vbs recycles :8000/:3000 when code or .env changed.
echo ============================================================
echo.
if "%SILENT%"=="0" pause
goto :eof

REM ============================================================
REM  Subroutines
REM ============================================================

REM Record %1 as node dir when it holds node.exe
:probe_node
if defined NODE_DIR goto :eof
if exist "%~1\node.exe" set "NODE_DIR=%~1"
goto :eof

REM Backslashes to forward slashes, result in FWDS
:tofwds
set "FWDS=%~1"
set "FWDS=%FWDS:\=/%"
goto :eof

REM BUSY=1 when port %1 is already listened on
:port_busy
set "BUSY=0"
netstat -an | findstr /c:":%~1 " | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 set "BUSY=1"
goto :eof

REM Same kill as stop.bat, used to recycle listeners we started.
:kill_port
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %~1 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
goto :eof

REM Poll %1 until it answers, 90s budget, label %2
:wait_url
setlocal
set "URL=%~1"
set "LABEL=%~2"
set /a TRIES=0
:wu_loop
curl -f -s -o NUL -m 3 "%URL%" >nul 2>&1
if not errorlevel 1 goto :wu_ok
set /a TRIES+=1
if %TRIES% GEQ 90 goto :wu_fail
timeout /t 1 /nobreak >nul 2>&1
goto :wu_loop
:wu_ok
echo   [ready] %LABEL%
endlocal
goto :eof
:wu_fail
echo   [timeout] %LABEL% not ready within 90s, check its window for errors
endlocal
goto :eof

REM ============================================================
REM  Error messages
REM ============================================================

:err_python
echo   [ERROR] python venv not found: %PYSITE%
echo           Run these first:
echo             python -m venv .venv
echo             .venv\Scripts\python.exe -m pip install -e "./backend[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
goto :halt

:err_layout
echo   [ERROR] missing %BE%\app\main.py
echo           Keep start.bat next to the backend and frontend folders.
goto :halt

:err_frontend
echo   [ERROR] frontend dependencies missing: %FE%\node_modules
echo           Run first:
echo             cd frontend
echo             npm install
goto :halt

:err_node
echo   [ERROR] Node.js not found. Install Node.js 18+ and make sure node.exe is on PATH.
goto :halt

:err_stale_port_8000
echo   [ERROR] port 8000 is still busy after a recycle. This start will NOT
echo           keep last week's python. Run stop.bat, then start.vbs again.
goto :halt

:err_stale_port_3000
echo   [ERROR] port 3000 is still busy after a recycle. Run stop.bat, then
echo           start.vbs again. The URL stays http://localhost:3000.
goto :halt

:halt
echo.
if "%SILENT%"=="0" pause
endlocal
exit /b 1

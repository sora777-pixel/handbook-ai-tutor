@echo off
setlocal enableextensions
title AI Learning Tutor - Launcher

REM /silent (no pause at the end) and /nobrowser (do not open a browser)
set "SILENT=0"
set "NOBROWSER=0"
if /i "%~1"=="/silent"    set "SILENT=1"
if /i "%~2"=="/silent"    set "SILENT=1"
if /i "%~1"=="/nobrowser" set "NOBROWSER=1"
if /i "%~2"=="/nobrowser" set "NOBROWSER=1"

REM ============================================================
REM  AI Learning Tutor - one click local launcher
REM
REM  Double-click start.bat and everything below happens automatically:
REM    1. python env     creates .venv on first run; rebuilds it when the
REM                       interpreter that made it is gone (Anaconda removed,
REM                       python moved) -- scripts\bootstrap_python.ps1
REM    2. node modules   npm install on first run
REM    3. .env + folders copied from .env.example, data\ / tmp\ created
REM    4. frontend build only when frontend\src is newer than the build
REM    5. backend :8000  waits for /health, dumps the log and STOPS on failure
REM    6. frontend :3000 waits for the page, then opens the browser
REM    7. watchdog       closing the browser stops everything by itself
REM
REM  This file is the ONLY entry point: double-click it.  Type start.bat /silent
REM  to suppress the closing prompt, /nobrowser to leave the browser alone.
REM  There is no stop script: closing every browser tab ends the app.
REM  Progress is appended to tmp\launcher.log on every run, silent or not.
REM  Nothing here is destructive: data in backend\tutor.db and
REM  data\storage\<user_id>\ is never touched by a start.
REM ============================================================

REM ---------- 1. Resolve paths from this script location ----------
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BE=%ROOT%\backend"
set "FE=%ROOT%\frontend"
set "DATA=%ROOT%\data"
set "DBFILE=%ROOT%\backend\tutor.db"
set "VENV=%ROOT%\.venv"
set "PYSITE=%VENV%\Scripts\python.exe"
set "TMPD=%ROOT%\tmp"
set "LOG=%TMPD%\launcher.log"
set "STAMP=%TMPD%\launcher.stamp"
set "WDPID=%TMPD%\watchdog.pid"

if not exist "%TMPD%" mkdir "%TMPD%" >nul 2>&1

REM ---------- 1b. One launcher at a time ----------
REM Two launchers fight over three things at once: the log files (Windows then
REM refuses the second Start-Process with "The process cannot access the file
REM because it is being used by another process"), port :8000 (EADDRINUSE) and
REM the SQLite file (database is locked -> the progress bar freezes). The guard
REM is a file the OS refuses to share, so it cannot be bypassed.
set "LOCK=%TMPD%\launcher.lock"
set "CMDPID="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId" 2^>nul`) do set "CMDPID=%%P"
set "LOCKSTATE=unknown"
if defined CMDPID (
  REM Result is "acquired", "acquired-stale" or "busy:<pid>".
  for /f "usebackq delims=" %%K in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\launcher_lock.ps1" -Lock "%LOCK%" -OwnerPid %CMDPID% 2^>nul`) do set "LOCKSTATE=%%K"
)
if not defined CMDPID (
  REM Could not identify our own cmd pid -- better to start than to deadlock.
  call :say "  [lock] could not identify this launcher process; single-instance guard is OFF"
)
if "%LOCKSTATE:~0,4%"=="busy" (
  call :say "  [busy] another launcher is already running (lock: %LOCKSTATE%)."
  call :say "         Nothing was started, so no port was touched."
  call :say "         If no launcher window is open, delete tmp\launcher.lock and retry."
  call :fail "another AI Learning Tutor launcher is already running"
)
if "%LOCKSTATE%"=="unknown" set "LOCKSTATE=acquired"
REM (only reached when this launcher may proceed)

REM ---------- 1c. Per-launch log files ----------
REM Fixed names meant a launch during a half-dead previous run collided on
REM tmp\backend8000.log. Timestamped names remove the collision entirely and
REM keep the evidence of each attempt; old ones are pruned below.
set "STAMPID="
for /f "usebackq delims=" %%T in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd-HHmmss" 2^>nul`) do set "STAMPID=%%T"
if not defined STAMPID set "STAMPID=run"
set "BE_LOG=%TMPD%\backend-%STAMPID%.log"
set "BE_ERR=%TMPD%\backend-%STAMPID%.err.log"
set "FE_LOG=%TMPD%\frontend-%STAMPID%.log"
set "FE_ERR=%TMPD%\frontend-%STAMPID%.err.log"
REM Keep the five newest backend/frontend logs; anything older is noise.
for /f "skip=5 delims=" %%F in ('dir /b /o-d "%TMPD%\backend-*.log" "%TMPD%\frontend-*.log" 2^>nul') do del /q "%%F" >nul 2>&1

> "%LOG%" echo ==== start.bat %DATE% %TIME% (launch %STAMPID%) ====

call :say "============================================================"
call :say "  AI Learning Tutor - local launcher"
call :say "============================================================"
call :say "  Project : %ROOT%"

REM ============================================================
REM  2. Python environment (validate, else bootstrap)
REM ============================================================
call :say "  [check] python environment ..."
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\bootstrap_python.ps1" -Root "%ROOT%" >> "%LOG%" 2>&1
if errorlevel 1 (
  call :say "  [ERROR] The python environment could not be prepared."
  call :say "          See the [python] lines in tmp\launcher.log."
  call :fail "python environment could not be prepared (see tmp\launcher.log)"
)

REM ---------- 3. Locate Node.js ----------
set "PF86=%ProgramFiles(x86)%"
set "NODE_DIR="
call :probe_node "%ProgramFiles%\nodejs"
call :probe_node "%PF86%\nodejs"
call :probe_node "%LOCALAPPDATA%\Programs\nodejs"
if not defined NODE_DIR for /f "delims=" %%N in ('where node 2^>nul') do call :probe_node "%%~dpN"
if not defined NODE_DIR (
  call :say "  [ERROR] Node.js not found. Install Node.js 18+ and make sure node.exe is on PATH."
  call :fail "Node.js 18+ is not installed or not on PATH"
)

REM Give child windows this project venv python + detected node first
set "PATH=%VENV%\Scripts;%NODE_DIR%;%PATH%"

REM Local ffmpeg lives in tools\ffmpeg\bin only (never the system PATH).
REM If the binaries are missing, download a copy there so video ingest does
REM not die with WinError 2 / "系统找不到指定的文件".
if not exist "%ROOT%\tools\ffmpeg\bin\ffmpeg.exe" (
  call :say "  [ffmpeg] local copy missing, downloading into tools\ffmpeg\bin ..."
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\bootstrap_ffmpeg.ps1" >> "%LOG%" 2>&1
)
if exist "%ROOT%\tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%ROOT%\tools\ffmpeg\bin;%PATH%"

REM ============================================================
REM  4. First-run project state: .env, folders, node_modules
REM ============================================================
if not exist "%ROOT%\.env" (
  if exist "%ROOT%\.env.example" (
    call :say "  [env] first run: creating .env from .env.example ..."
    copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
  ) else (
    call :say "  [env] .env is missing and there is no .env.example to copy from"
  )
)
if exist "%ROOT%\.env" (
  REM Generate a key only when the file has no usable one. Done in PowerShell
  REM because `findstr /c:"SECRET_KEY=."` treats the dot as a LITERAL character
  REM (with /c: there is no regex), so the check never matched and every launch
  REM appended another key to .env.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%ROOT%\.env'; if (-not (Select-String -LiteralPath $p -Pattern '^SECRET_KEY=.+' -Quiet)) { $k=-join ((48..57)+(97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_}); Add-Content -LiteralPath $p -Value ('SECRET_KEY=' + $k) -Encoding ASCII; Write-Output 'generated' } else { Write-Output 'present' }" > "%TMPD%\secret_check.txt" 2>&1
  set "SECRET_KEY_STATE=unknown"
  for /f "tokens=*" %%K in ('type "%TMPD%\secret_check.txt" 2^>nul') do set "SECRET_KEY_STATE=%%K"
  del /q "%TMPD%\secret_check.txt" >nul 2>&1
  if /i "%SECRET_KEY_STATE%"=="generated" call :say "  [env] .env had no SECRET_KEY; generated one"
)

if not exist "%DATA%" mkdir "%DATA%" >nul 2>&1
if not exist "%DATA%\storage" mkdir "%DATA%\storage" >nul 2>&1

REM A heartbeat left over from an earlier session would make the watchdog see
REM "no browser for hours" and stop the app seconds after this launch.
del /q "%TMPD%\last_beat" >nul 2>&1

if not exist "%FE%\node_modules\next\dist\bin\next" (
  call :say "  [npm] first run: installing frontend dependencies, this takes a few minutes ..."
  pushd "%FE%"
  call "%NODE_DIR%\npm.cmd" install --no-audit --no-fund >> "%LOG%" 2>&1
  if errorlevel 1 (
    popd
    call :fail "npm install failed (see tmp\launcher.log)"
  )
  popd
)

REM ---------- 5. Runtime environment (all data is persisted on disk) ----------
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
REM Seconds without a browser page before the watchdog stops everything. The
REM requirement is "closing the frontend closes the app", so the default is a
REM short grace period rather than three minutes: it absorbs a reload or a
REM closed-then-reopened tab without leaving the app running all evening.
REM Set AUTO_STOP_IDLE_SECONDS=0 to disable auto-stop entirely.
if not defined AUTO_STOP_IDLE_SECONDS set "AUTO_STOP_IDLE_SECONDS=60"
REM OCR: reading images / scanned PDFs. "auto" uses the offline ONNX engine when
REM it is installed, otherwise falls back to a vision LLM. OCR_LANG=auto switches
REM to the Japanese model automatically when kana come out at low confidence.
REM Leave these unset to use the defaults; override in .env when needed.
REM   set "OCR_PROVIDER=auto"
REM   set "OCR_LANG=auto"
set "SECRET_KEY=dev-secret-change-me-use-at-least-32-bytes"
set "CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000"
set "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000"

REM Local health checks must never go through a proxy. Tools and corporate VPN
REM clients export HTTP_PROXY/HTTPS_PROXY globally, and curl then asks the proxy
REM for 127.0.0.1, which answers "upstream connect failed" (502) for the whole
REM budget -- that is how a perfectly healthy backend used to look dead.
set "NO_PROXY=127.0.0.1,localhost,::1"
set "no_proxy=127.0.0.1,localhost,::1"

REM ---------- 6. Rebuild frontend when src is newer than `next start` bundle ----------
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\frontend_build_needed.ps1" -Root "%ROOT%"
if errorlevel 1 (
  call :say "  [build] frontend source is newer than the build; running npm run build ..."
  pushd "%FE%"
  call "%NODE_DIR%\npm.cmd" run build >> "%LOG%" 2>&1
  if errorlevel 1 (
    popd
    call :dump_log "%LOG%" "tmp\launcher.log"
    call :fail "npm run build failed (see tmp\launcher.log)"
  )
  popd
  call :say "  [build] done"
)

REM ---------- 7. Clean leftovers, then recycle :8000/:3000 if needed ----------
REM A crashed run leaves python/node processes behind. Killing only whatever
REM holds :8000/:3000 misses the rest (a dead `next` child still owns the log
REM file, an uvicorn that lost its port still owns the database), and the next
REM launch then trips over "The process cannot access the file because it is
REM being used by another process". So every launch starts by removing this
REM project's own leftovers -- matched on command line, never on name alone.
call :say "  [clean] stopping leftover processes from a previous run ..."
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\clean_stale.ps1" -Root "%ROOT%" >> "%LOG%" 2>&1
call :kill_watchdog

REM Reusing a listener that answers /health used to keep last week's uvicorn
REM (the [deepseek / deepseek-chat] error stamp) after a pull. Compare a
REM fingerprint of backend/app + frontend/src + .env + start.bat + BUILD_ID.
set "WANTED="
set "HAVE="
set "RELOAD=0"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\code_stamp.ps1" -Root "%ROOT%"`) do set "WANTED=%%S"
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
if not defined WANTED set "RELOAD=1"
if /i not "%WANTED%"=="%HAVE%" set "RELOAD=1"
if "%RELOAD%"=="1" (
  call :say "  [reload] code or config on disk is newer than the running services"
  call :say "           recycling ports 8000 and 3000 so this drop loads"
  call :kill_port 8000
  call :kill_port 3000
  timeout /t 2 /nobreak >nul 2>&1
)

REM ---------- 8. Start backend (or reuse a healthy one) ----------
call :port_busy 8000
if "%BUSY%"=="1" (
  if "%RELOAD%"=="1" goto :be_start
  curl -f -s -o NUL -m 3 "http://127.0.0.1:8000/health" >nul 2>&1
  if not errorlevel 1 (
    call :say "  [reuse] backend :8000 is healthy and matches this drop"
    goto :be_ready
  )
  call :say "  [reload] port 8000 is busy but /health failed; recycling it"
  call :kill_port 8000
  timeout /t 2 /nobreak >nul 2>&1
)
:be_start
call :port_busy 8000
if "%BUSY%"=="1" (
  call :say "  [ERROR] port 8000 is still busy after a recycle. This start will NOT"
  call :say "          keep last week's python. Current owner(s):"
  call :who_owns 8000
  call :say "          close it, then start.bat again."
  call :fail "port 8000 is still busy after a recycle"
)
call :say "  [start] backend api on port 8000 ... (logs: tmp\backend-%STAMPID%.log)"
powershell -NoProfile -Command "Start-Process -FilePath '%PYSITE%' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' -WorkingDirectory '%BE%' -WindowStyle Hidden -RedirectStandardOutput '%BE_LOG%' -RedirectStandardError '%BE_ERR%'"
:be_ready

REM ---------- 9. Wait until the backend answers, and fail loudly ----------
REM The old launcher waited 90 x (curl + sleep) and then opened the browser
REM anyway, so a dead backend surfaced minutes later as "Backend unreachable"
REM on a page the user had already started typing into. Now a failure prints
REM the tail of the launch's backend-*.err.log and never opens a browser.
call :wait_url "http://127.0.0.1:8000/health" "backend api" 120
if not "%READY%"=="1" (
  call :say "  [ERROR] the backend never became healthy on http://127.0.0.1:8000/health"
  call :say "          The browser was NOT opened on purpose -- fix this first."
  call :dump_log "%BE_ERR%" "%BE_ERR%"
  call :dump_log "%BE_LOG%" "%BE_LOG%"
  call :fail "backend never became healthy (see %BE_ERR%)"
)

REM ---------- 10. Start frontend (or reuse a healthy one) ----------
call :port_busy 3000
if "%BUSY%"=="1" (
  if "%RELOAD%"=="1" goto :fe_start
  curl -f -s -o NUL -m 5 "http://127.0.0.1:3000/" >nul 2>&1
  if not errorlevel 1 (
    call :say "  [reuse] frontend :3000 is serving and matches this drop"
    goto :fe_ready
  )
  call :say "  [reload] port 3000 is busy but not serving; recycling it"
  call :kill_port 3000
  timeout /t 2 /nobreak >nul 2>&1
)
:fe_start
call :port_busy 3000
if "%BUSY%"=="1" (
  call :say "  [ERROR] port 3000 is still busy after a recycle. Current owner(s):"
  call :who_owns 3000
  call :say "          close it, then start.bat again."
  call :say "          The URL stays http://localhost:3000."
  call :fail "port 3000 is still busy after a recycle"
)
call :say "  [start] frontend on port 3000 ... (logs: tmp\frontend-%STAMPID%.log)"
powershell -NoProfile -Command "Start-Process -FilePath '%NODE_DIR%\node.exe' -ArgumentList 'node_modules\next\dist\bin\next','start','-p','3000' -WorkingDirectory '%FE%' -WindowStyle Hidden -RedirectStandardOutput '%FE_LOG%' -RedirectStandardError '%FE_ERR%'"
:fe_ready

call :wait_url "http://127.0.0.1:3000/" "frontend ui" 60
if not "%READY%"=="1" (
  call :say "  [ERROR] the frontend never served http://127.0.0.1:3000/"
  call :say "          The browser was NOT opened on purpose -- fix this first."
  call :dump_log "%FE_ERR%" "%FE_ERR%"
  call :dump_log "%FE_LOG%" "%FE_LOG%"
  call :fail "frontend never served :3000 (see %FE_ERR%)"
)

if defined WANTED (
  >"%STAMP%" echo %WANTED%
)

REM ---------- 11. Watchdog: closing the browser stops the app ----------
REM Output is redirected to its own files on purpose: a watchdog attached to
REM this console would die with the console window, taking auto-stop with it.
call :kill_watchdog
powershell -NoProfile -Command "$p = Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%ROOT%\scripts\session_watchdog.ps1','-Root','%ROOT%','-IdleSeconds','%AUTO_STOP_IDLE_SECONDS%' -WindowStyle Hidden -RedirectStandardOutput '%TMPD%\watchdog.out.log' -RedirectStandardError '%TMPD%\watchdog.err.log' -PassThru; Set-Content -LiteralPath '%WDPID%' -Value $p.Id -Encoding ASCII" >> "%LOG%" 2>&1

REM ---------- 12. Open the browser ----------
call :say ""
if "%NOBROWSER%"=="1" (
  call :say "  [skip] /nobrowser given, browser not opened"
) else (
  call :say "  [open] browser http://localhost:3000"
  start "" "http://localhost:3000"
)

call :say ""
call :say "============================================================"
call :say "  Ready"
call :say "============================================================"
call :say "  App      : http://localhost:3000"
call :say "  API docs : http://127.0.0.1:8000/docs"
call :say ""
call :say "  Data     : backend\tutor.db   (accounts, sources, quizzes)"
call :say "             data\storage\<id>\ (profile / records / resources)"
call :say ""
call :say "  Auto-stop: closing every browser tab stops backend + frontend"
call :say "             after %AUTO_STOP_IDLE_SECONDS%s without a page ping."
call :say "             Set AUTO_STOP_IDLE_SECONDS=0 before launching to disable."
call :say "  Manual   : run start.bat again to restart; the console window may"
call :say "             be closed freely -- the app lives until the browser does."
call :say "  Log      : tmp\launcher.log"
call :say "============================================================"

call :say "  Logs      : tmp\backend-%STAMPID%.log / frontend-%STAMPID%.log"
call :say "  Launcher  : tmp\launcher.log"
call :say "============================================================"

REM The guard only protects the startup itself: once the services run, they are
REM the app, and a second launch should be free to reuse them.
call :release_lock
if "%SILENT%"=="0" pause
endlocal
exit /b 0

REM ============================================================
REM  Subroutines
REM ============================================================

REM %1 = line: print it and keep it in tmp\launcher.log
:say
if "%~1"=="" (
  echo.
  >>"%LOG%" echo.
  goto :eof
)
echo %~1
>>"%LOG%" echo %~1
goto :eof

REM Drop the single-instance guard (ours only)
:release_lock
if not defined LOCK goto :eof
if not defined CMDPID goto :eof
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\launcher_lock.ps1" -Lock "%LOCK%" -OwnerPid %CMDPID% -Release >nul 2>&1
goto :eof

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

REM Stop a listener we started (or a stale one from a previous run).
REM /T matters: `next start` runs a child server, and killing only the parent
REM leaves that child holding the log file -- the next launch then fails with
REM "The process cannot access the file because it is being used by another
REM process". After killing we WAIT for the OS to actually release the port
REM instead of sleeping a fixed 2s.
:kill_port
for /f "tokens=1-5" %%a in ('netstat -ano -p tcp ^| findstr "LISTENING" ^| findstr /c:":%~1 "') do (
  if "%%d"=="LISTENING" taskkill /PID %%e /T /F >nul 2>&1
)
set /a KWTRIES=0
:kp_wait
call :port_busy %~1
if "%BUSY%"=="0" goto :eof
set /a KWTRIES+=1
if %KWTRIES% GEQ 20 goto :eof
timeout /t 1 /nobreak >nul 2>&1
goto :kp_wait

REM Who is holding %1 -- the old launcher only said "busy", which sent people
REM hunting through Task Manager blind.
:who_owns
for /f "tokens=1-5" %%a in ('netstat -ano -p tcp ^| findstr "LISTENING" ^| findstr /c:":%~1 "') do (
  if "%%d"=="LISTENING" (
    for /f "tokens=1" %%P in ('tasklist /FI "PID eq %%e" /NH 2^>nul') do call :say "          %%~P (PID %%e) holds port %~1"
  )
)
goto :eof

REM Stop a watchdog left over from an earlier launch
:kill_watchdog
if not exist "%WDPID%" goto :eof
set "WDID="
set /p WDID=<"%WDPID%"
if not defined WDID goto :eof
taskkill /PID %WDID% /F >nul 2>&1
del /q "%WDPID%" >nul 2>&1
goto :eof

REM Poll %1 until it answers (%3 tries), label %2. READY=1 / READY=0
:wait_url
set "URL=%~1"
set "LABEL=%~2"
set /a LIMIT=%~3
set /a TRIES=0
:wu_loop
curl -f -s -o NUL -m 3 "%URL%" >nul 2>&1
if not errorlevel 1 goto :wu_ok
set /a TRIES+=1
if %TRIES% GEQ %LIMIT% goto :wu_fail
timeout /t 1 /nobreak >nul 2>&1
goto :wu_loop
:wu_ok
call :say "  [ready] %LABEL%"
set "READY=1"
goto :eof
:wu_fail
set "READY=0"
goto :eof

REM Mirror the tail of %1 into the console and tmp\launcher.log, labelled %2
:dump_log
if not exist "%~1" goto :eof
call :say "  ---- last lines of %~2 ----"
powershell -NoProfile -Command "Get-Content -LiteralPath '%~1' -Tail 12 -ErrorAction SilentlyContinue | Tee-Object -FilePath '%LOG%' -Append"
call :say "  --------------------------------"
goto :eof

REM %1 = one-line reason: pause when interactive, message box when silent
:fail
>>"%LOG%" echo [ERROR] %~1
call :release_lock
if "%SILENT%"=="1" (
  powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms | Out-Null; [System.Windows.Forms.MessageBox]::Show('%~1','AI Learning Tutor failed to start')" >nul 2>&1
) else (
  echo.
  echo   Press any key to close this window.
  pause >nul
)
endlocal
exit /b 1
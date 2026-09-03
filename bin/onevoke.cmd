@echo off
setlocal DisableDelayedExpansion
set "NoDefaultCurrentDirectoryInExePath=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "ONEVOKE_PYTHON_EXE="

if defined ONEVOKE_PYTHON goto configured_python
if exist "%SystemRoot%\py.exe" (
  "%SystemRoot%\py.exe" -3 -X utf8 -c "import sys; raise SystemExit(sys.version_info.major != 3)" >nul 2>nul
  if not errorlevel 1 (
    set "ONEVOKE_PYTHON_EXE=%SystemRoot%\py.exe"
    goto run_py
  )
)
for %%D in ("%CD%\py.exe") do set "ONEVOKE_CURRENT_PY=%%~fD"
for /f "delims=" %%P in ('"%SystemRoot%\System32\where.exe" "$PATH:py.exe" 2^>nul') do (
  if /i not "%%~fP"=="%ONEVOKE_CURRENT_PY%" if not "%%~zP"=="0" (
    "%%~fP" -3 -X utf8 -c "import sys; raise SystemExit(sys.version_info.major != 3)" >nul 2>nul
    if not errorlevel 1 (
      set "ONEVOKE_PYTHON_EXE=%%~fP"
      goto run_py
    )
  )
)
for %%D in ("%CD%\python.exe") do set "ONEVOKE_CURRENT_PYTHON=%%~fD"
for /f "delims=" %%P in ('"%SystemRoot%\System32\where.exe" "$PATH:python.exe" 2^>nul') do (
  if /i not "%%~fP"=="%ONEVOKE_CURRENT_PYTHON%" if not "%%~zP"=="0" (
    "%%~fP" -X utf8 -c "import sys; raise SystemExit(sys.version_info.major != 3)" >nul 2>nul
    if not errorlevel 1 (
      set "ONEVOKE_PYTHON_EXE=%%~fP"
      goto run_python
    )
  )
)
for %%D in ("%CD%\python3.exe") do set "ONEVOKE_CURRENT_PYTHON3=%%~fD"
for /f "delims=" %%P in ('"%SystemRoot%\System32\where.exe" "$PATH:python3.exe" 2^>nul') do (
  if /i not "%%~fP"=="%ONEVOKE_CURRENT_PYTHON3%" if not "%%~zP"=="0" (
    "%%~fP" -X utf8 -c "import sys; raise SystemExit(sys.version_info.major != 3)" >nul 2>nul
    if not errorlevel 1 (
      set "ONEVOKE_PYTHON_EXE=%%~fP"
      goto run_python
    )
  )
)
for /f "delims=" %%P in ('"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "Get-AppxPackage -Name PythonSoftwareFoundation.Python.* ^| Sort-Object Version -Descending ^| ForEach-Object { Join-Path $_.InstallLocation python.exe } ^| Where-Object { Test-Path -LiteralPath $_ } ^| Select-Object -First 1" 2^>nul') do (
  if not "%%~zP"=="0" (
    "%%~fP" -X utf8 -c "import sys; raise SystemExit(sys.version_info.major != 3)" >nul 2>nul
    if not errorlevel 1 (
      set "ONEVOKE_PYTHON_EXE=%%~fP"
      goto run_python
    )
  )
)
goto python_missing

:configured_python
for %%P in ("%ONEVOKE_PYTHON%") do (
  if /i not "%%~fP"=="%%~P" goto python_missing
  set "ONEVOKE_PYTHON_EXE=%%~fP"
)
if not exist "%ONEVOKE_PYTHON_EXE%" goto python_missing
for %%S in ("%ONEVOKE_PYTHON_EXE%") do if "%%~zS"=="0" goto python_missing
"%ONEVOKE_PYTHON_EXE%" -X utf8 -c "import sys; raise SystemExit(sys.version_info.major != 3)" >nul 2>nul
if errorlevel 1 goto python_missing

:run_python
"%ONEVOKE_PYTHON_EXE%" -X utf8 "%~dp0onevoke" %*
exit /b %errorlevel%

:run_py
"%ONEVOKE_PYTHON_EXE%" -3 -X utf8 "%~dp0onevoke" %*
exit /b %errorlevel%

:python_missing
>&2 echo onevoke: Python 3 was not found. Install Python 3 or add it to PATH.
exit /b 127

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "ios_loop_wizard.py" (
  echo 没找到 ios_loop_wizard.py，请把这个 bat 放在项目目录里再运行。
  pause
  exit /b 1
)

set "PY=py -3"
where py >nul 2>nul
if errorlevel 1 set "PY=python"

%PY% -X utf8 ios_loop_wizard.py
pause

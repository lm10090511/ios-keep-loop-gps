@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "web_gui.py" (
  echo 没找到 web_gui.py，请把这个 bat 放在项目目录里再运行。
  pause
  exit /b 1
)

set "PY=py -3"
where py >nul 2>nul
if errorlevel 1 set "PY=python"

echo ============================================================
echo  循环虚拟定位 - 网页版
echo ============================================================
echo.
echo 正在启动本地服务，稍后浏览器会自动打开 ...
echo 关掉这个黑窗口 = 停止服务（手机定位会自己回到真实位置）
echo.

%PY% -X utf8 web_gui.py --open
if errorlevel 1 (
  echo.
  echo 启动失败，看上面的报错。
  pause
)

@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
echo.
echo  ============================================================
echo   Step 1: UIA read-only diagnostic (no memory patch, no send)
echo   Need: WeChat 4.x running and logged in, window visible.
echo  ============================================================
echo.
"%~dp0runtime\python\python.exe" -u "%~dp0tools\uia-diag.py" --all
echo.
echo  ============================================================
echo   Done. Check above:
echo     - path has NO Chinese chars
echo     - main window found
echo     - candidate RVA or feature-scan hit
echo  ============================================================
pause

@echo off
setlocal
pushd "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\export_taobao_cookie.py %*
) else if exist ".venv\bin\python" (
  ".venv\bin\python" tools\export_taobao_cookie.py %*
) else if exist "%SystemRoot%\py.exe" (
  py -3 tools\export_taobao_cookie.py %*
) else (
  python tools\export_taobao_cookie.py %*
)

set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
  echo Export completed. Upload the generated JSON in Settings - Taobao account.
) else (
  echo Export failed. Check the message above.
)
pause
popd
exit /b %CODE%

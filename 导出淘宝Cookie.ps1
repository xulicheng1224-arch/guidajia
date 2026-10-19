$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $Root
try {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) {
        $Python = $venv
        $PythonArgs = @()
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = "py"
        $PythonArgs = @("-3")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = "python"
        $PythonArgs = @()
    } else {
        throw "未找到 Python 3，请先安装 Python 3.10 或更高版本。"
    }
    & $Python @PythonArgs "tools\export_taobao_cookie.py" @args
    $Code = $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $Code = 1
} finally {
    Pop-Location
}
Write-Host ""
Read-Host "按 Enter 关闭此窗口"
exit $Code

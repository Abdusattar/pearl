$ErrorActionPreference = "SilentlyContinue"
Get-Process -Name python | Where-Object { $_.Path -like "*python*" } | ForEach-Object {
    $cmd = (Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
    if ($cmd -like "*uvicorn*") { Stop-Process -Id $_.Id -Force }
}
Start-Sleep -Seconds 1
Write-Host "Starting Pearl server on http://127.0.0.1:8000" -ForegroundColor Green
Set-Location $PSScriptRoot\..
$env:PYTHONIOENCODING = "utf-8"
python -m uvicorn app.main:app --reload --port 8000

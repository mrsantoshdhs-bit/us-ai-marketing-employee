# Start the local dashboard at http://127.0.0.1:8765
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& "$root\.venv\Scripts\python.exe" -m aime serve

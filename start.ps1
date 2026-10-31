param([Nullable[int]]$Port)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
& .venv/Scripts/python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install dependencies.' }
$serverArguments = @('-m', 'app.server')
if ($PSBoundParameters.ContainsKey('Port')) { $serverArguments += @('--port', $Port) }
& .venv/Scripts/python.exe @serverArguments

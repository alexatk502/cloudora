# Cloudora: set up (the first time) and run, on Windows. No administrator rights needed.
#
#   .\scripts\quickstart.ps1                 reachable from your network (HTTPS)
#   .\scripts\quickstart.ps1 --anywhere      reachable from any network through a Cloudflare quick tunnel
#   .\scripts\quickstart.ps1 --local         only from this computer (to try it out)
#   .\scripts\quickstart.ps1 --help          every option (e.g. --key-file to keep the same key, --view-only)
#
# Or double-click scripts\quickstart.cmd.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Cloudora = Join-Path $Venv "Scripts\cloudora.exe"
if (-not (Test-Path $Cloudora)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Py = "py"; $PyArgs = @("-3") }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $Py = "python"; $PyArgs = @() }
    else { Write-Error "Python 3.11 or newer is required: https://www.python.org/downloads/ (tick 'Add python.exe to PATH')" }
    & $Py @PyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { Write-Error "Python 3.11 or newer is required: https://www.python.org/downloads/" }
    Write-Host "[cloudora] first run: installing into $Venv"
    & $Py @PyArgs -m venv $Venv
    & (Join-Path $Venv "Scripts\python.exe") -m pip install -q --upgrade pip
    & (Join-Path $Venv "Scripts\pip.exe") install -q -e $Root
    New-Item -ItemType File -Force (Join-Path $Venv ".cloudora-installed") | Out-Null
} else {
    $Stamp = Join-Path $Venv ".cloudora-installed"
    $Project = Join-Path $Root "pyproject.toml"
    if (-not (Test-Path $Stamp) -or (Get-Item $Project).LastWriteTime -gt (Get-Item $Stamp).LastWriteTime) {
        Write-Host "[cloudora] updated: installing what the new version needs"
        & (Join-Path $Venv "Scripts\pip.exe") install -q -e $Root
        New-Item -ItemType File -Force $Stamp | Out-Null
    }
}
& $Cloudora @args
exit $LASTEXITCODE

# Builds dist\antilag.exe: a single executable with Python, Flet, cryptography
# and wintun.dll inside. When opened, Windows asks for administrator rights (UAC
# manifest), so the panel never starts without being able to create the tunnel.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -SinAdmin   # for testing
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Version 1.2.0
#
# The release workflow (.github/workflows/release.yml) runs this same script.
param([switch]$SinAdmin, [string]$Version = '0.1.0')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install --user --quiet "flet>=1.0" pyinstaller cryptography
if (-not (Test-Path 'antilag\tun\wintun.dll')) { python -m antilag fetch-wintun }

$scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
$flet = Join-Path $scripts 'flet.exe'

$name = 'antilag'
$extra = @('--uac-admin')
if ($SinAdmin) { $name = 'antilag-prueba'; $extra = @() }

& $flet pack antilag_app.py --name $name @extra `
    --add-binary 'antilag\tun\wintun.dll;antilag\tun' `
    --product-name 'antilag' --product-version $Version --file-version $Version `
    --file-description 'Failover multi-enlace para Tibia' --company-name 'antilag' `
    -y
if ($LASTEXITCODE -ne 0) { throw "flet pack fallo ($LASTEXITCODE)" }
Get-Item "dist\$name.exe" | Select-Object Name, @{n='MB'; e={[math]::Round($_.Length / 1MB, 1)}}

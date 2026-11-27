# Builds the Windows app (Electron window + Python backend):
#   dist\electron\MassSyncedLyrics-Setup-<version>.exe     installer (per user, no admin rights)
#   dist\electron\MassSyncedLyrics-<version>-portable.exe  single file, nothing to install
#   dist\MassSyncedLyrics-<version>-windows.zip           both, plus the command-line tool
# Usage (PowerShell, from this folder):  .\build.ps1
#   -Python <cmd>   Python launcher to use (default: py)
#   -SkipTests      skip the test suites
param(
    [string]$Python = "py",
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$FpcalcVersion = "1.6.2"
$version = (& $Python -c "import sys; sys.path.insert(0, 'src'); import mass_synced_lyrics as m; print(m.__version__)").Trim()
Write-Host "Building Mass Synced Lyrics $version"

& $Python -m pip install --upgrade -r requirements.txt pyinstaller pytest
if ($LASTEXITCODE) { throw "pip install failed" }

Push-Location desktop
try {
    # the window's version follows the Python package
    $pkg = Get-Content package.json -Raw | ConvertFrom-Json
    if ($pkg.version -ne $version) {
        & npm version $version --no-git-tag-version --allow-same-version
        if ($LASTEXITCODE) { throw "npm version failed" }
    }
    & npm ci --no-audit --no-fund
    if ($LASTEXITCODE) { throw "npm ci failed" }
} finally { Pop-Location }

if (-not $SkipTests) {
    & $Python -m pytest -q
    if ($LASTEXITCODE) { throw "Python tests failed" }
    Push-Location desktop
    try {
        & npm test
        if ($LASTEXITCODE) { throw "window tests failed" }
    } finally { Pop-Location }
}

if (-not (Test-Path "src\mass_synced_lyrics\_buildinfo.py")) {
    & $Python scripts\write_buildinfo.py
}

New-Item -ItemType Directory -Force build | Out-Null
if (-not (Test-Path "build\fpcalc.exe")) {
    $zip = "build\chromaprint-fpcalc.zip"
    $url = "https://github.com/acoustid/chromaprint/releases/download/v$FpcalcVersion/chromaprint-fpcalc-$FpcalcVersion-windows-x86_64.zip"
    Write-Host "Downloading fpcalc $FpcalcVersion"
    # Python's downloader avoids Windows PowerShell 5.1's old TLS defaults
    & $Python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" $url $zip
    if ($LASTEXITCODE) { throw "fpcalc download failed" }
    Expand-Archive -Force $zip build\chromaprint
    Copy-Item (Get-ChildItem build\chromaprint -Recurse -Filter fpcalc.exe | Select-Object -First 1).FullName build\fpcalc.exe
}

# The backend: a folder (starts fast; the window starts it), with the decoder (PyAV/FFmpeg), fpcalc and the
# GPU aligner's worker script
$common = @("--noconfirm", "--clean", "--paths", "src", "--add-binary", "build\fpcalc.exe;.",
            "--icon", "src\mass_synced_lyrics\assets\icon.ico")
Remove-Item -Recurse -Force dist\msl-backend -ErrorAction SilentlyContinue
& $Python -m PyInstaller @common --onedir --console --name msl-backend `
    --add-data "src\mass_synced_lyrics\assets;mass_synced_lyrics\assets" `
    --add-data "src\mass_synced_lyrics\aligner_worker.py;mass_synced_lyrics" `
    --collect-all av --exclude-module tkinter launchers\backend.py
if ($LASTEXITCODE) { throw "PyInstaller (backend) failed" }
& $Python -m PyInstaller @common --onefile --console --exclude-module av --exclude-module numpy --exclude-module tkinter `
    --name mass-synced-lyrics-cli launchers\cli.py
if ($LASTEXITCODE) { throw "PyInstaller (CLI) failed" }

# The window, with the backend inside it
Push-Location desktop
try {
    Remove-Item -Recurse -Force ..\dist\electron -ErrorAction SilentlyContinue
    & npx electron-builder --win nsis portable --x64 --publish never
    if ($LASTEXITCODE) { throw "electron-builder failed" }
} finally { Pop-Location }

$setup = "dist\electron\MassSyncedLyrics-Setup-$version.exe"
$portable = "dist\electron\MassSyncedLyrics-$version-portable.exe"
$pkgDir = "dist\MassSyncedLyrics-$version-windows"
Remove-Item -Recurse -Force $pkgDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $pkgDir | Out-Null
Copy-Item $setup, $portable, dist\mass-synced-lyrics-cli.exe, README.md, LICENSE, THIRD_PARTY_NOTICES.md $pkgDir
Compress-Archive -Force -Path "$pkgDir\*" -DestinationPath "$pkgDir.zip"
# Keep a ready-to-run copy of the app in the project folder itself
Copy-Item $portable MassSyncedLyrics.exe -Force
Write-Host "Done: $setup, $portable, $pkgDir.zip (and MassSyncedLyrics.exe in the project folder)"

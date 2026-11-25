# Builds the Windows app: dist\MassSyncedLyrics-<version>-windows.zip
# Usage (PowerShell, from this folder):  .\build.ps1
#   -Python <cmd>   Python launcher to use (default: py)
#   -SkipTests      skip the test suite
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

if (-not $SkipTests) {
    & $Python -m pytest -q -k "not gui"
    if ($LASTEXITCODE) { throw "tests failed" }
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

$common = @("--noconfirm", "--clean", "--onefile", "--paths", "src", "--add-binary", "build\fpcalc.exe;.",
            "--icon", "src\mass_synced_lyrics\assets\icon.ico")
# The GUI also needs the audio player stack (PyAV/FFmpeg for decoding, PortAudio via sounddevice).
$player = @("--add-data", "src\mass_synced_lyrics\assets;mass_synced_lyrics\assets",
            "--add-data", "src\mass_synced_lyrics\aligner_worker.py;mass_synced_lyrics", "--collect-data", "sv_ttk",
            "--collect-all", "av", "--collect-all", "_sounddevice_data", "--hidden-import", "sounddevice")
& $Python -m PyInstaller @common @player --windowed --name MassSyncedLyrics launchers\gui.py
if ($LASTEXITCODE) { throw "PyInstaller (GUI) failed" }
& $Python -m PyInstaller @common --console --exclude-module av --exclude-module sounddevice --exclude-module numpy --exclude-module tkinter --exclude-module sv_ttk --name mass-synced-lyrics-cli launchers\cli.py
if ($LASTEXITCODE) { throw "PyInstaller (CLI) failed" }

$pkg = "dist\MassSyncedLyrics-$version-windows"
Remove-Item -Recurse -Force $pkg -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $pkg | Out-Null
Copy-Item dist\MassSyncedLyrics.exe, dist\mass-synced-lyrics-cli.exe, README.md, LICENSE, THIRD_PARTY_NOTICES.md $pkg
Compress-Archive -Force -Path "$pkg\*" -DestinationPath "$pkg.zip"
# Keep a ready-to-run copy of the app in the project folder itself
Copy-Item dist\MassSyncedLyrics.exe . -Force
Write-Host "Done: $pkg.zip (and MassSyncedLyrics.exe in the project folder)"

<#
<summary>
Install Deckplate for the logged in user on Windows with a shortcut on the
Desktop and in the Start Menu. No admin: everything goes under
%LOCALAPPDATA%\Programs\Deckplate.
</summary>
<param name="Program">Optional path to the deckplate.exe to install.</param>
<remarks>
Run from the unzipped release folder:
    powershell -ExecutionPolicy Bypass -File deploy\install-desktop.ps1
Copies deckplate.exe, deckplate-launch.ps1 and deckplate.ico to the install
folder, then writes the two shortcuts. Each shortcut runs the launcher through
powershell.exe with a hidden window, so a click shows only the Deckplate
window. Idempotent: re-run after every release and the program is replaced.
Stop the daemon first (taskkill /im deckplate.exe) because Windows will not
overwrite a running exe. The program comes from -Program, else from
deckplate.exe beside the deploy folder, else from the App mirror of a source
tree.
</remarks>
#>
param([string]$Program = '')
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

if (-not $Program) {
    $candidates = @(
        (Join-Path $root 'deckplate.exe'),
        (Join-Path $root 'Deckplate App\windows\deckplate.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { $Program = $candidate; break }
    }
    if (-not $Program) {
        Write-Error 'no deckplate.exe found; pass its path with -Program'
        exit 1
    }
}
if (-not (Test-Path $Program)) {
    Write-Error "$Program is not a file"
    exit 1
}

$target = Join-Path $env:LOCALAPPDATA 'Programs\Deckplate'
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path $Program -Destination (Join-Path $target 'deckplate.exe') -Force
Copy-Item -Path (Join-Path $here 'deckplate-launch.ps1') -Destination $target -Force
Copy-Item -Path (Join-Path $here 'deckplate.ico') -Destination $target -Force
Write-Host "==> program, launcher and icon in $target"

$launcher = Join-Path $target 'deckplate-launch.ps1'
$icon = Join-Path $target 'deckplate.ico'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Deckplate'
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @($desktop, $startMenu)) {
    $path = Join-Path $folder 'Deckplate.lnk'
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $powershell
    $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
    $link.WorkingDirectory = $target
    $link.IconLocation = "$icon,0"
    $link.Description = 'Drive the Soomfon stream deck and open its configuration page'
    $link.Save()
    Write-Host "==> shortcut $path"
}

Write-Host ''
Write-Host 'Done. Click Deckplate on the desktop or in the Start Menu: it starts the'
Write-Host 'daemon if needed and opens the window. The daemon log is'
Write-Host (Join-Path $env:LOCALAPPDATA 'deckplate\run.log')

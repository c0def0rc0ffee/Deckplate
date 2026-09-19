<#
<summary>
Desktop click handler for Deckplate on Windows: brings up the daemon if it is
not already running, then opens the configuration window.
</summary>
<remarks>
Installed beside deckplate.exe by deploy\install-desktop.ps1, which points the
Desktop and Start Menu shortcuts at this file through powershell.exe with a
hidden window. Idempotent: a second click while the daemon is up just opens
the window again. The daemon is started in a hidden console, logs to
%LOCALAPPDATA%\deckplate\run.log and keeps running after the window closes.
Stop it from Task Manager or with:  taskkill /im deckplate.exe
It is started with --keep-official because no console can ask the question:
if the official SOOMFON app is running the deck stays with that app, so close
it first or set official_software = "stop" in the config. Failures are shown
in a message box, since a hidden window has no console to print to.
</remarks>
#>
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$program = Join-Path $here 'deckplate.exe'
$port = if ($env:DECKPLATE_PORT) { [int]$env:DECKPLATE_PORT } else { 8765 }

# <summary>Show a message box, the only output a hidden window has.</summary>
# <param name="text">The message.</param>
function Show-Notice([string]$text) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($text, 'Deckplate') | Out-Null
}

# <summary>True when something is listening on the daemon's local port.</summary>
# <remarks>A bare socket test. The gui command does the real check against the
# API and says so if the port belongs to something else.</remarks>
function Test-Listening {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (-not (Test-Path $program)) {
    Show-Notice "deckplate.exe is not beside the launcher. Run deploy\install-desktop.ps1 again."
    exit 1
}

if (-not (Test-Listening)) {
    $state = Join-Path $env:LOCALAPPDATA 'deckplate'
    New-Item -ItemType Directory -Force -Path $state | Out-Null
    $log = Join-Path $state 'run.log'
    Add-Content -Path $log -Value "=== $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') started from the desktop ==="
    # cmd.exe does the redirection so both streams share the one log file. The
    # outer pair of quotes is cmd's own rule for a /c string that holds quotes.
    $command = "`"`"$program`" run --keep-official >> `"$log`" 2>&1`""
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $command -WindowStyle Hidden
    $up = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Listening) { $up = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $up) {
        Show-Notice "The daemon did not start. See $log"
        exit 1
    }
}

& $program gui

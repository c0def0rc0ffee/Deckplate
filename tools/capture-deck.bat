@echo off
setlocal enableextensions
title Deckplate USB capture

rem <summary>
rem Deckplate USB capture helper.
rem Run this AS ADMINISTRATOR (right-click the file, "Run as administrator").
rem USBPcap needs admin rights to open the capture devices.
rem </summary>
rem <remarks>
rem It captures all USB traffic on the three root hubs at once, so the deck is
rem caught whichever port it is plugged into. We only decode the deck's own
rem packets afterwards and delete the rest.
rem
rem PRIVACY: this records every device on those hubs while it runs, which can
rem include keyboard and mouse input. Do not type passwords or anything
rem sensitive while the capture is running.
rem
rem This is the only sanctioned way to learn what the deck understands. Nothing
rem is ever sent to a deck to find out what it does: a capture listens to the
rem official app and a new command is only ever added as a byte for byte copy
rem of what was seen here. docs/hardware-safety.md is binding on that point.
rem
rem The three hub names are assumed to be USBPcap1, USBPcap2 and USBPcap3.
rem A machine with more or differently numbered hubs will silently capture the
rem wrong ones, so check the file sizes reported at the end: an empty capture
rem means the deck was on a hub that was not being watched.
rem Stopping is by killing every USBPcapCMD process, so any capture you started
rem by hand outside this script stops with it. The files are written into a
rem captures folder beside this script and the previous three are deleted at
rem the start of each run, so copy anything worth keeping before re-running.
rem </remarks>

set "USBPCAP=%ProgramFiles%\USBPcap\USBPcapCMD.exe"
set "OUT=%~dp0captures"

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo This script must be run as Administrator.
  echo Right-click capture-deck.bat and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

if not exist "%USBPCAP%" (
  echo Could not find USBPcapCMD at "%USBPCAP%".
  pause
  exit /b 1
)

if not exist "%OUT%" mkdir "%OUT%"
del /q "%OUT%\hub1.pcap" "%OUT%\hub2.pcap" "%OUT%\hub3.pcap" 2>nul

echo Starting capture on USBPcap1, USBPcap2 and USBPcap3 ...
start "usbpcap1" /b "%USBPCAP%" -d \\.\USBPcap1 -A --inject-descriptors -o "%OUT%\hub1.pcap"
start "usbpcap2" /b "%USBPCAP%" -d \\.\USBPcap2 -A --inject-descriptors -o "%OUT%\hub2.pcap"
start "usbpcap3" /b "%USBPCAP%" -d \\.\USBPcap3 -A --inject-descriptors -o "%OUT%\hub3.pcap"

echo.
echo ============================================================
echo   CAPTURE IS RUNNING
echo.
echo   Do this, one step at a time, pausing a few seconds between
echo   each so the packets group cleanly:
echo.
echo     1. Plug in the deck.
echo     2. Open the official SOOMFON / StreamDock app and let it
echo        connect. The surround should appear around the keys.
echo     3. Unplug the deck, wait, then replug it with the app open
echo        (this records the connect and the surround being painted).
echo     4. Move the brightness slider up and down.
echo     5. Set a picture on one key.
echo     6. Try any background, theme or colour option in the app.
echo     7. Use the app's boot-logo feature with a picture.
echo     8. Press each side button once.
echo.
echo   When you have finished, come back to this window and
echo   press any key to STOP the capture.
echo ============================================================
echo.
pause

echo.
echo Stopping capture ...
taskkill /F /IM USBPcapCMD.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul

echo.
echo Done. Capture files:
dir /b "%OUT%\hub1.pcap" "%OUT%\hub2.pcap" "%OUT%\hub3.pcap" 2>nul
for %%F in ("%OUT%\hub1.pcap" "%OUT%\hub2.pcap" "%OUT%\hub3.pcap") do (
  if exist "%%~F" echo   %%~nxF  %%~zF bytes
)
echo.
echo You can close this window now. The capture is done.
pause

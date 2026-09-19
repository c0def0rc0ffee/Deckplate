#!/usr/bin/env python3
"""<summary>
Build the single file program for this platform and package it.

Runs PyInstaller, stages the result with the files a user needs, and writes
    Deckplate Dist/deckplate-v<version>-<platform>.zip
then mirrors the same files into
    Deckplate App/<platform>/

    python tools/build.py

Options:
    --no-tests   skip the test run (build-zip.sh has already run it)
    --no-zip     build and stage only, for a quick look at the output
</summary>
<remarks>
On Linux, build-zip.sh calls this after the tests and the version stamp. On
Windows run it directly from the virtual environment, because the official app
and the capture tooling only exist there.

The version is read from the VERSION file at the repository root and is never
written by this script. Moving the version is the Linux packaging script's job:
this one only stamps whatever it finds into the zip name and into INSTALL.txt.
So a build started here with a stale VERSION produces a stale file name and no
warning, which is the trap to watch for when building on Windows.

The build refuses to start while the development fake deck is switched on. See
<see cref="refuse_development_flags"/>: a shipped program that quietly drives a
fake instead of the hardware would hide a disconnect from the person using it,
so the check runs before PyInstaller rather than after.

Order matters and is not obvious from reading main alone. The flag check comes
first because it is instant and fatal, then the tests, then the build, then the
staging, then the zip, then the mirror. A red suite must never reach a zip, so
``--no-tests`` exists only for the caller that has already run them.

The App folder is build output and is replaced wholesale on every run. Nothing
in it is ever edited by hand: a change made there is lost at the next build and
never reaches the source.

Python and no shell tricks on purpose: the same script works on both
platforms, and the Linux packaging convention keeps bash for build-zip.sh.
</remarks>"""

from __future__ import annotations

import argparse
import os
import re
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "deckplate"
DIST_DIR = ROOT / "Deckplate Dist"
APP_DIR = ROOT / "Deckplate App"
BUILD_DIR = ROOT / "build"
PLATFORM = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "linux")
EXE = f"{NAME}.exe" if PLATFORM == "windows" else NAME

INSTALL_LINUX = """Deckplate {version} for Linux

1. Put the program on your path, with a Deckplate icon on the desktop and in
   the menu (no sudo, installs to ~/.local/bin):
       ./deploy/install-desktop.sh
   A click on that icon starts the daemon if it is not running and opens the
   configuration window, so steps 3 and 4 are one click. Or by hand:
       install -m 0755 deckplate ~/.local/bin/deckplate

2. Let your user talk to the deck (once, needs sudo), then unplug and replug it:
       sudo ./deploy/install-udev.sh

3. Run it. The first run writes ~/.config/deckplate/config.toml:
       deckplate run

4. In another terminal, open the configuration page in a window:
       deckplate gui
   The window needs the WebKitGTK bindings; if it does not open, install them
       sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
   or use    deckplate gui --browser    to open the page in your browser.

5. To start with your session:
       mkdir -p ~/.config/systemd/user
       cp deploy/deckplate.service ~/.config/systemd/user/
       systemctl --user daemon-reload
       systemctl --user enable --now deckplate

The config file is plain TOML and is applied live whenever it is saved.
"""

INSTALL_WINDOWS = """Deckplate {version} for Windows

1. Unzip this folder anywhere, for example C:\\Tools\\Deckplate.

2. For a Deckplate icon on the desktop and in the Start Menu (no admin,
   installs to %LOCALAPPDATA%\\Programs\\Deckplate), open a terminal in the
   folder and run:
       powershell -ExecutionPolicy Bypass -File deploy\\install-desktop.ps1
   A click on that icon starts the daemon if it is not running and opens the
   configuration window, so steps 4 and 5 are one click. The daemon keeps
   running after the window closes and logs to %LOCALAPPDATA%\\deckplate\\run.log.
   Stop it with:  taskkill /im deckplate.exe

3. Close the official SOOMFON app if it is running (or let the program ask).

4. Or by hand: open a terminal in the folder and run the daemon. The first
   run writes %APPDATA%\\deckplate\\config.toml:
       deckplate.exe run

5. In another terminal, open the configuration page in a window:
       deckplate.exe gui
   The window uses the WebView2 runtime that ships with Windows 11. On an
   older machine use    deckplate.exe gui --browser    instead.

6. To start with Windows: press Win+R, type  shell:startup  and put a
   shortcut to    deckplate.exe run --keep-official    in that folder.

The config file is plain TOML and is applied live whenever it is saved.
"""


def say(text: str) -> None:
    """
    <summary>
    Print a build progress line, flushed so it appears in order under a wrapper.
    </summary>
    <param name="text">The line.</param>
    """
    print(f"[build] {text}", flush=True)


def die(text: str) -> None:
    """<summary>
    Print a build error and end the process with a failure status.
    </summary>
    <param name="text">What went wrong, in the imperative where there is
    something the reader can do about it.</param>
    <remarks>
    This never returns, despite the None annotation, so calling it is the end
    of that path and no guard is needed after it. It exits rather than raising
    on purpose: the calling shell script reads the status, and a traceback
    would bury the one line that says what to fix.
    </remarks>
    """
    print(f"[build] ERROR: {text}", file=sys.stderr, flush=True)
    sys.exit(1)


def read_version() -> str:
    """<summary>
    Read the version string from the VERSION file at the repository root.
    </summary>
    <returns>The file's contents with surrounding whitespace stripped, such as
    "1.0.8".</returns>
    <remarks>
    Read only. This script never writes VERSION and never moves any part of
    it: the Linux packaging script owns that, and only the build number moves
    on its own. Whatever is in the file is what goes into the zip name and into
    the INSTALL.txt heading, so check it before building on Windows, where
    nothing has stamped it for you.
    </remarks>
    """
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def refuse_development_flags() -> None:
    """<summary>
    Refuse to build while the development fake deck flag is switched on.
    </summary>
    <remarks>
    A shipped program must never drive a fake deck. When
    ``FAKE_DECK_WHEN_ABSENT`` is True and no hardware is present, the daemon
    runs happily against an in memory transport, which is exactly what is
    wanted while working on the configuration page and exactly what must never
    reach a user: a disconnected deck would look like a working one.

    The check is a text search of deckplate/device.py rather than an import,
    so it holds whatever state the module would have ended up in and cannot be
    fooled by something reassigning the flag at run time. It matches the
    assignment as it is written, so keep that line spelled plainly: reformat it
    and this gate silently stops working.

    Called before the tests and before PyInstaller, because it costs nothing
    and there is no point building anything if it fails.
    </remarks>
    """
    source = (ROOT / "deckplate" / "device.py").read_text(encoding="utf-8")
    if "FAKE_DECK_WHEN_ABSENT = True" in source:
        die("FAKE_DECK_WHEN_ABSENT is True in deckplate/device.py: turn it off before building")
    say("development flags are off")


def run_tests(python: str) -> None:
    """<summary>
    Run the whole pytest suite and stop the build if any of it fails.
    </summary>
    <param name="python">The interpreter to run pytest with, normally the one
    running this script so the build and the tests share an environment.</param>
    <remarks>
    Output is left to go straight to the terminal rather than captured, so a
    failure is readable where it happened. There is no way to carry on past a
    red suite from here: ``--no-tests`` skips the run entirely and exists only
    for the packaging script that has already run it.
    </remarks>
    """
    say("running the tests")
    result = subprocess.run([python, "-m", "pytest", "-q"], cwd=ROOT)
    if result.returncode != 0:
        die("tests failed: not building")


SPEC = """\
# PyInstaller spec written by tools/build.py. Do not edit; edit build.py.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    [{entry!r}],
    pathex=[{root!r}],
    datas=[({web!r}, "deckplate/web"), ({themes!r}, "deckplate/themes"),
           ({config!r}, "deckplate")] + collect_data_files("webview"),
    hiddenimports=collect_submodules("pynput") + collect_submodules("webview"),
    # On Linux pywebview draws with GTK, and PyInstaller's GTK hook bundles
    # every icon theme and GTK theme on the build machine unless told which:
    # 1 GB of cursors on Mint, a 440 MB program that took 11 seconds to start.
    # The window only needs the stock theme; the desktop supplies the rest.
    hooksconfig={{"gi": {{"icons": ["Adwaita", "hicolor"], "themes": ["Default"], "languages": ["en_GB", "en"]}}}},
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name={name!r}, console=True, upx=False, strip=False, icon={icon!r})
"""


def run_pyinstaller(python: str, work: Path) -> Path:
    """<summary>
    Write a spec file into the work folder and build the single file program.
    </summary>
    <param name="python">The interpreter whose environment holds PyInstaller
    and the runtime dependencies that will be bundled.</param>
    <param name="work">A throwaway folder for the spec, the intermediate work
    tree and the built binary. The caller removes it afterwards.</param>
    <returns>The path of the built executable inside that work folder.</returns>
    <remarks>
    The spec is generated every time from <see cref="SPEC"/> rather than kept
    in the repository, so the two can never drift apart. Editing a spec file
    found on disk achieves nothing: it is overwritten on the next run.

    The data folders it lists have to land at their package relative paths
    inside the bundle, because the code finds them beside its own file at run
    time. Move one and the program builds cleanly and then fails when it looks
    for its web assets or its icon themes.

    PyInstaller returning zero is not proof of a build, so the executable is
    checked for before the path is handed back.
    </remarks>
    """
    try:
        subprocess.run([python, "-m", "PyInstaller", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        die("PyInstaller is not installed: pip install pyinstaller")
    distpath = work / "bin"
    spec = work / f"{NAME}.spec"
    spec.write_text(SPEC.format(
        entry=str(ROOT / "deckplate" / "__main__.py"),
        root=str(ROOT),
        web=str(ROOT / "deckplate" / "web"),
        # The shipped icon themes. themes.py finds them beside its own file, so
        # they have to land at deckplate/themes inside the bundle, exactly as
        # the web folder does.
        themes=str(ROOT / "deckplate" / "themes"),
        config=str(ROOT / "deckplate" / "default-config.toml"),
        name=NAME,
        # The exe carries the icon the shortcut uses. Windows only: on Linux
        # the icon lives in deploy/deckplate.svg and PyInstaller ignores it.
        icon=str(ROOT / "deploy" / "deckplate.ico") if PLATFORM == "windows" else None,
    ), encoding="utf-8")
    args = [
        python, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(distpath),
        "--workpath", str(work / "work"),
        str(spec),
    ]
    say("running PyInstaller")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        die("PyInstaller failed")
    exe = distpath / EXE
    if not exe.is_file():
        die(f"expected {exe} after the build")
    return exe


def stage(exe: Path, version: str, work: Path) -> Path:
    """<summary>
    Assemble the exact tree that will be zipped and mirrored: the program, the
    per platform deploy files and an INSTALL.txt.
    </summary>
    <param name="exe">The executable produced by PyInstaller.</param>
    <param name="version">The version string, stamped into INSTALL.txt.</param>
    <param name="work">The throwaway folder to build the staged tree inside.
    It must not already contain a ``stage`` folder.</param>
    <returns>The staged folder. Its contents are the release, exactly.</returns>
    <remarks>
    The deploy files are listed by name rather than copied wholesale, so a
    working file left in the deploy folder is not shipped by accident and a new
    file that should ship has to be added here deliberately.

    The executable bit is put back on the Linux binary because copying does not
    always carry it, and a release nobody can run is a quiet failure.

    INSTALL.txt is written with the line endings of its own platform: a Windows
    user opening a file with bare newlines in Notepad sees one long line.
    </remarks>
    """
    staged = work / "stage"
    staged.mkdir()
    shutil.copy2(exe, staged / EXE)
    if PLATFORM != "windows":
        (staged / EXE).chmod((staged / EXE).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        deploy = staged / "deploy"
        deploy.mkdir()
        for name in ("40-deckplate.rules", "install-udev.sh", "deckplate.service",
                     "install-desktop.sh", "deckplate-launch.sh", "deckplate.desktop", "deckplate.svg"):
            shutil.copy2(ROOT / "deploy" / name, deploy / name)
            if name.endswith(".sh"):
                (deploy / name).chmod(0o755)
        text = INSTALL_LINUX
    else:
        deploy = staged / "deploy"
        deploy.mkdir()
        for name in ("install-desktop.ps1", "deckplate-launch.ps1", "deckplate.ico"):
            shutil.copy2(ROOT / "deploy" / name, deploy / name)
        text = INSTALL_WINDOWS
    (staged / "INSTALL.txt").write_text(text.format(version=version), encoding="utf-8", newline="\r\n" if PLATFORM == "windows" else "\n")
    return staged


def make_zip(staged: Path, destination: Path) -> None:
    """<summary>
    Zip the staged tree, preserving Unix permissions and forward slash paths.
    </summary>
    <param name="staged">The tree from <see cref="stage"/>. Its own name does
    not appear in the archive: paths are relative to it.</param>
    <param name="destination">The zip to write. An existing file at that path
    is replaced rather than added to.</param>
    <remarks>
    Entries are written by hand instead of with the convenience call so two
    things survive: the file mode, which carries the executable bit for anyone
    unzipping on Linux, and posix style paths. Backslash separated entries
    break on Linux, which is why the platform's own archiver is never used to
    produce a release zip.

    Files are sorted so two builds of the same tree produce the same ordering
    and a diff between releases is about content.
    </remarks>
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(p for p in staged.rglob("*") if p.is_file()):
            arcname = file.relative_to(staged).as_posix()
            info = zipfile.ZipInfo.from_file(file, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            # keep the executable bit for Linux users unzipping on Linux
            info.external_attr = (file.stat().st_mode & 0xFFFF) << 16
            with file.open("rb") as handle:
                archive.writestr(info, handle.read())


def mirror(staged: Path, target: Path) -> None:
    """<summary>
    Replace the contents of the App folder with an exact copy of the staged
    files.
    </summary>
    <param name="staged">The tree from <see cref="stage"/>.</param>
    <param name="target">The App folder for this platform. Everything already
    in it is removed first, so it must contain nothing but build output.</param>
    <remarks>
    This is the App folder convention: build output, never hand edited. Fix the
    source and rebuild, because anything written here goes at the next build
    and never reaches the repository.

    The folder itself is kept and only its contents replaced: on Windows a
    terminal or a sync client can hold the directory open, which makes
    removing it fail even though its files can be replaced.

    A file that cannot be replaced ends the build with the name of the file, so
    the program holding it can be closed and the build rerun. Carrying on would
    leave a half old, half new mirror that matches no release.
    </remarks>
    """
    target.mkdir(parents=True, exist_ok=True)
    for entry in target.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as err:
            die(f"cannot replace {entry}: {err}. Close any program using it and rerun")
    shutil.copytree(staged, target, dirs_exist_ok=True)



def help_text(doc: str | None) -> str:
    """<summary>
    Strip the XML doc tags out of a module docstring so it can be shown as
    command line help.
    </summary>
    <param name="doc">A module docstring in the house comment style, or None.</param>
    <returns>The prose alone, with the tag lines removed.</returns>
    <remarks>
    Every file in this project carries a tagged header block, and argparse
    prints whatever description it is handed verbatim. Passing the raw
    docstring therefore shows tags to the user, which is what this exists to
    prevent. Only lines that are nothing but a tag are dropped, so prose and
    the indented examples inside the block survive untouched. A cross
    reference written inline in a sentence is unwrapped to the bare name
    rather than dropped, because deleting the line would take the sentence
    around it with it.
    </remarks>
    """
    if not doc:
        return ""
    tag = re.compile(r"^\s*</?(?:summary|remarks|param|returns|exception|see|seealso)\b[^>]*>\s*$")
    ref = re.compile(r"<(?:see|seealso)\s+cref=\"([^\"]+)\"\s*/?>")
    kept = (ref.sub(r"\1", line) for line in doc.splitlines() if not tag.match(line))
    return "\n".join(kept).strip("\n")

def main() -> int:
    """<summary>
    Command line entry point: check, test, build, stage, zip and mirror.
    </summary>
    <returns>0 on success. Failures leave through <see cref="die"/> and never
    come back here.</returns>
    <remarks>
    The steps are in a fixed order for a reason, and it is the order to keep:
    the fake deck flag first because it is instant, the tests next because
    nothing red may be packaged, and the mirror last because it is the only
    step that touches a folder outside the work tree.

    The work folder is made under ``build`` rather than the system temporary
    directory, so a build of several hundred megabytes lands on the same disk
    as the repository, and it is removed even when a step fails.

    With ``--no-zip`` the staged tree is kept under ``build`` and nothing is
    written to the Dist or App folders at all, which is the way to look at what
    would ship without producing a release.
    </remarks>
    """
    parser = argparse.ArgumentParser(description=help_text(__doc__), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    python = sys.executable
    version = read_version()
    say(f"Deckplate v{version} for {PLATFORM} ({platform.machine()}) with {python}")
    refuse_development_flags()
    if not args.no_tests:
        run_tests(python)

    BUILD_DIR.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="deckplate-build-", dir=BUILD_DIR))
    try:
        exe = run_pyinstaller(python, work)
        say(f"built {exe} ({exe.stat().st_size / 1048576:.1f} MB)")
        staged = stage(exe, version, work)
        if args.no_zip:
            keep = BUILD_DIR / f"stage-{PLATFORM}"
            mirror(staged, keep)
            say(f"staged in {keep}")
            return 0
        zip_path = DIST_DIR / f"{NAME}-v{version}-{PLATFORM}.zip"
        make_zip(staged, zip_path)
        say(f"wrote {zip_path} ({zip_path.stat().st_size / 1048576:.1f} MB)")
        app = APP_DIR / PLATFORM
        mirror(staged, app)
        say(f"mirrored {app}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# <summary>
# Packaging build for Deckplate, following the same procedure as the
# Hooked and Furballistics build scripts but for a Python application rather
# than a web site. Builds two versioned zips from the project root plus the
# local run copy:
#   Deckplate Dist/deckplate-v<version>-linux.zip   the single file
#                                                    program plus the
#                                                    deploy files
#   Deckplate Git/deckplate-v<version>-src.zip       GitHub bound source
#   Deckplate App/linux/                             mirror of the Dist zip
# </summary>
# <param name="--no-bump">
# Ship the VERSION file verbatim instead of moving the build segment on. For
# minor and major releases: hand edit VERSION to e.g. 1.1.0, build once with
# --no-bump, and later builds resume incrementing from there (1.1.1, ...).
# </param>
# <remarks>
# Run from anywhere:  ./build-zip.sh
#
# The compile step is PyInstaller, driven by tools/build.py (Python so the
# same script builds the Windows zip on the Windows machine, where it is run
# directly). There is no Vite folder and no Apache mirror. The one time Linux
# step that needs sudo is deploy/install-udev.sh and packaging never depends
# on it.
#
# Order matters and is not obvious from reading downwards. VERSION moves on
# first, before the tests and before anything is stamped, so the package and
# both zips carry the same new number. Then the tests run, and a red suite
# stops the build before a single zip is written. Nothing here publishes,
# pushes or tags: the zips are left on disk for a human to look at.
#
# The script is not idempotent by design. Every run without --no-bump burns a
# build number whether or not the zips differ, so a failed run costs a version.
# </remarks>
set -euo pipefail

NO_BUMP=0
for arg in "$@"; do
    case "$arg" in
        --no-bump) NO_BUMP=1 ;;
        *) echo "Unknown option: $arg (only --no-bump is supported)" >&2; exit 2 ;;
    esac
done

warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v zip   >/dev/null || die "zip not found: sudo apt install zip"
command -v rsync >/dev/null || die "rsync not found: sudo apt install rsync"

# Prefer the project's own virtual environment when there is one, otherwise
# whatever python3 is on the path. The tests need pytest, hidapi and Pillow:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
if [[ -x "$root/.venv/bin/python" ]]; then
    python="$root/.venv/bin/python"
elif command -v python3 >/dev/null; then
    python="$(command -v python3)"
else
    die "python3 not found"
fi
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required ($python)"

# Version scheme is major.minor.BUILD: every packaged build moves the third
# segment on (1.0.0 -> 1.0.1), written back to VERSION here BEFORE the tests
# and the stamping below so the package and the zips carry the new number.
# Major and minor move only when Rob says so: edit VERSION by hand for those.
version_file="$root/VERSION"
version="$(tr -d '[:space:]' < "$version_file")"
[[ "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] \
    || die "VERSION '$version' is not major.minor.build"
if [[ $NO_BUMP -eq 1 ]]; then
    echo "Building v$version (hand set, --no-bump)"
else
    version="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))"
    printf '%s\n' "$version" > "$version_file"
    echo "Building v$version (build segment moved on)"
fi

name='deckplate'
dist_dir="$root/Deckplate Dist"
git_dir="$root/Deckplate Git"
app_dir="$root/Deckplate App"
stage="$(mktemp -d /tmp/deckplate-build.XXXXXX)"
trap 'rm -rf "$stage"' EXIT

# Local-only exclusions are kept in .git/info/exclude, not here and not in
# .gitignore, because both this script and .gitignore ship in the source zip
# and a published file must not carry those names. Read them at build time so
# the zips match what git ignores. No patterns means the protection is gone,
# so the build stops rather than packaging without it.
local_excludes=()
local_patterns=()
if [ -f "$root/.git/info/exclude" ]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "${line// }" || "$line" == \#* ]] && continue
        local_excludes+=(--exclude="$line")
        local_patterns+=("$line")
    done < "$root/.git/info/exclude"
fi
[[ ${#local_patterns[@]} -gt 0 ]] || die "no local exclude patterns found: refusing to package"

# <summary>
# Stop the build if any locally excluded name survived into a staged tree.
# </summary>
# <param name="1">The staged directory to search, already populated.</param>
# <remarks>
# Fail closed. An exclusion that silently stopped working is the whole risk
# here, so check the staged trees rather than trusting the patterns above.
# Call this after the tree is staged and before it is zipped: it inspects what
# is on disk, so calling it earlier proves nothing.
# Each pattern is stripped of a leading and trailing slash and matched against
# the entry name alone, which is why a pattern in a subfolder is caught too.
# Patterns with a path separator in them are therefore not checked here: keep
# the excludes as bare names.
# </remarks>
# <exception cref="die">A locally excluded entry reached the stage.</exception>
assert_local_excluded() {
    local dir="$1" pat clean
    for pat in "${local_patterns[@]}"; do
        clean="${pat%/}"; clean="${clean#/}"
        if [ -n "$(find "$dir" -name "$clean" -print -quit 2>/dev/null)" ]; then
            die "a locally excluded entry reached the stage ($clean): refusing to package"
        fi
    done
}

# Excluded from BOTH zips. Nothing secret lives in this tree today (the
# weather source needs no key) so these are a backstop for the day one gets
# dropped in. Deliberately broad: a bare credentials file with no extension
# once rode into nine of Coldfront's src zips before its patterns were widened.
both_exclude_files=(--exclude='*.zip' --exclude='*.7z' --exclude='*.tmp' --exclude='*.log' --exclude='.env'
                    --exclude='*SFTP*' --exclude='*FTP*'
                    --exclude='*credential*' --exclude='*password*' --exclude='*secret*'
                    --exclude='*.pem' --exclude='*.key' --exclude='id_rsa*')

# Never staged into either zip: convention output folders, tooling, generated
# and environment trees. The Git zip is the source tree to push, not a clone,
# so '.git/' stays out of it. The editor and tooling state goes out with it,
# through the local-only patterns read above rather than by name here.
exclude_dirs=(--exclude='Deckplate Dist/' --exclude='Deckplate Git/' --exclude='Deckplate App/'
              --exclude='$RECYCLE.BIN/' --exclude='.git/'
              --exclude='node_modules/' --exclude='.venv/' --exclude='venv/'
              --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/'
              --exclude='*.egg-info/' --exclude='build/' --exclude='dist/')

mkdir -p "$dist_dir" "$git_dir"

# <summary>
# Write the version being built into one file, in place, if it is not there
# already.
# </summary>
# <param name="1">The file to edit.</param>
# <param name="2">
# An anchored sed and grep pattern naming the line, up to and including the
# opening quote of the value, such as '^version = '.
# </param>
# <remarks>
# Stamp VERSION into pyproject.toml and the package so the three never drift.
# VERSION is the single source of truth.
# The pattern is used as a regular expression twice, so it must suit both grep
# -E and sed -E, and it must match exactly one line: a loose pattern rewrites
# every line it touches with no warning.
# The value has to be a double quoted string on the same line. Nothing is said
# when the file already carries the right version, which is the usual case on
# a rebuild of the same number.
# </remarks>
stamp() {
    local file="$1" pattern="$2"
    if ! grep -qE "$pattern\"$version\"" "$file"; then
        sed -E -i "s/($pattern)\"[^\"]*\"/\1\"$version\"/" "$file"
        echo "Stamped v$version into ${file#$root/}"
    fi
}
stamp "$root/pyproject.toml" '^version = '
stamp "$root/deckplate/__init__.py" '^__version__ = '

# <summary>
# Zip the contents of a staged directory, replacing any zip already there.
# </summary>
# <param name="1">The staged directory whose contents become the zip root.</param>
# <param name="2">
# Where to write the zip. Must be an absolute path: zip runs from inside the
# staged directory, so a relative one lands in the stage and is thrown away
# with it.
# </param>
# <remarks>
# The subshell cd is what keeps the paths inside the zip relative to the stage
# rather than carrying the build machine's folders with them. The old zip is
# removed first because zip updates an existing archive in place, which would
# otherwise leave files from a previous build sitting in the new one.
# </remarks>
make_zip() {
    local source_dir="$1" destination="$2"
    rm -f "$destination"
    (cd "$source_dir" && zip -qr "$destination" .)
}

zip_size_mb() {
    local bytes
    bytes="$(stat -c%s "$1")"
    awk -v b="$bytes" 'BEGIN { printf "%.2f", b / 1048576 }'
}

# 1) Tests first. A red suite must not ship.
(cd "$root" && "$python" -m pytest -q) || die "tests failed: not packaging"

# 2) Dist zip and App mirror: the single file program built by PyInstaller,
#    staged with the deploy files and an INSTALL.txt, zipped as
#    deckplate-v<version>-linux.zip and mirrored into 'Deckplate App/linux'.
#    tools/build.py does all of that; the tests have just run so it skips them.
"$python" -m PyInstaller --version >/dev/null 2>&1 \
    || die "PyInstaller not found: $python -m pip install -r requirements-dev.txt"
(cd "$root" && "$python" tools/build.py --no-tests) || die "platform build failed"
dist_zip="$dist_dir/$name-v$version-linux.zip"
[[ -f "$dist_zip" ]] || die "expected $dist_zip after the build"
echo "Built: $dist_zip ($(zip_size_mb "$dist_zip") MB)"
[[ -x "$app_dir/linux/$name" ]] || die "App mirror missing the program"
echo "Mirrored: $app_dir/linux"

# 3) Git zip: the full source tree, housekeeping and tests included.
#    .gitignore is the one exclusion list: the filter below makes rsync honour
#    every pattern in it, in every folder, so the zip matches git ls-files.
#    The lists above stay as a backstop. Two hand kept lists drifted on
#    14/09/2026 and a src zip picked up the pre rename output folders, the
#    USB captures and a chat transcript, none of them tracked.
src_stage="$stage/src"
rsync -a --filter=':- .gitignore' "${local_excludes[@]}" "${both_exclude_files[@]}" "${exclude_dirs[@]}" \
      "$root/" "$src_stage/"
assert_local_excluded "$src_stage"

git_zip="$git_dir/$name-v$version-src.zip"
make_zip "$src_stage" "$git_zip"
echo "Built: $git_zip ($(zip_size_mb "$git_zip") MB)"

echo
echo "The Windows zip is built on the Windows machine with:  python tools\\build.py"

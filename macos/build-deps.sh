#!/bin/bash
#
# Build what wintogo needs on macOS that Homebrew doesn't provide, into .deps/
# in this checkout (delete .deps/ to undo):
#
#   ntfs-3g  the library and tools (mkntfs, ntfscat, ntfscp) without the FUSE
#            driver, which on macOS needs the closed-source macFUSE
#   wimlib   with NTFS-3G support; Homebrew's is built without it
#   hivex    with Python bindings for this python3; Homebrew's has none
#
# The sources are the releases Homebrew uses, checked against the same SHA-256
# sums. Needs: brew install autoconf automake libtool pkgconf
#
# The wintogo programs put .deps/ first on their PATH by themselves. Rerun
# this after moving the checkout or switching python3 to a new version.

set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=$HERE/.deps
WORK=$PREFIX/build
PYTHON=${PYTHON:-$(command -v python3)}
JOBS=$(sysctl -n hw.ncpu)

NTFS3G=2026.9.28
NTFS3G_URL=https://github.com/tuxera/ntfs-3g/archive/refs/tags/$NTFS3G.tar.gz
NTFS3G_SHA=8a75293f07bf40df5701d296de32a4cb0e46d6ae135abb4f41b438df9868a390
WIMLIB=1.14.5
WIMLIB_URL=https://wimlib.net/downloads/wimlib-$WIMLIB.tar.gz
WIMLIB_SHA=84221a3abd5b91228f15f8e6065c335a336237b5738197b75bf419eea561a194
HIVEX=1.3.24
HIVEX_URL=https://download.libguestfs.org/hivex/hivex-$HIVEX.tar.gz
HIVEX_SHA=a52fa45cecc9a78adb2d28605d68261e4f1fd4514a778a5473013d2ccc8a193c

die() { echo "build-deps: error: $*" >&2; exit 1; }
log() { echo "==> $*" >&2; }

[[ $(uname -s) == Darwin ]] || die "this is for macOS; on Linux install the distribution's packages"
missing=
for pair in autoreconf:autoconf aclocal:automake glibtoolize:libtool pkg-config:pkgconf; do
    command -v "${pair%%:*}" >/dev/null || missing="$missing ${pair##*:}"
done
[[ -z $missing ]] || die "missing build tools; run: brew install$missing"
command -v cc >/dev/null || die "no C compiler; run: xcode-select --install"
[[ -n $PYTHON ]] || die "python3 not found"

# fetch NAME URL SHA256: download into $WORK and unpack to $WORK/NAME.
fetch() {
    local archive=$WORK/$1.tar.gz
    if [[ ! -f $archive ]] || ! echo "$3  $archive" | shasum -a 256 -c -s; then
        log "downloading $1"
        curl -fL --retry 3 -o "$archive.part" "$2"
        echo "$3  $archive.part" | shasum -a 256 -c -s || die "checksum mismatch for $2"
        mv "$archive.part" "$archive"
    fi
    rm -rf "${WORK:?}/$1"
    mkdir "$WORK/$1"
    tar -xzf "$archive" -C "$WORK/$1" --strip-components 1
}

mkdir -p "$WORK"
export PKG_CONFIG_PATH=$PREFIX/lib/pkgconfig

fetch ntfs-3g "$NTFS3G_URL" "$NTFS3G_SHA"
log "building ntfs-3g $NTFS3G"
(
    cd "$WORK/ntfs-3g"
    LIBTOOLIZE=glibtoolize autoreconf --install --force >/dev/null
    # Without --exec-prefix the library and tools would go to /lib and /sbin.
    # nfconv rewrites file names in Unicode NFD, as macOS's own filesystems
    # do; NTFS volumes for Windows want them as given.
    ./configure --prefix="$PREFIX" --exec-prefix="$PREFIX" \
                --disable-ntfs-3g --disable-nfconv --disable-crypto \
                --disable-ldconfig --disable-static --disable-dependency-tracking --quiet
    make -j"$JOBS" >/dev/null
    make install >/dev/null
)

fetch wimlib "$WIMLIB_URL" "$WIMLIB_SHA"
log "building wimlib $WIMLIB"
(
    cd "$WORK/wimlib"
    ./configure --prefix="$PREFIX" --with-ntfs-3g --without-fuse \
                --disable-static --disable-dependency-tracking --quiet
    make -j"$JOBS" >/dev/null
    make install >/dev/null
)

fetch hivex "$HIVEX_URL" "$HIVEX_SHA"
log "building hivex $HIVEX for $PYTHON"
(
    cd "$WORK/hivex"
    # The Python module leaves Python's own symbols to the interpreter.
    PYTHON=$PYTHON LDFLAGS="-Wl,-undefined,dynamic_lookup" \
        ./configure --prefix="$PREFIX" --with-python-installdir="$PREFIX/python" \
                    --disable-ocaml --disable-perl --disable-ruby --disable-nls --without-readline \
                    --disable-static --disable-dependency-tracking --quiet
    # Apple's ld has no GNU version scripts, and configure falls back to
    # Solaris' -M; like Homebrew, swap in -map, which only names a link map
    # to write. Only the library and the Python module are needed (building
    # the test images crashes on macOS).
    for dir in gnulib/lib lib python; do
        make -C "$dir" -j"$JOBS" VERSION_SCRIPT_FLAGS="-Wl,-map -Wl," >/dev/null
    done
    make -C lib install >/dev/null
    make -C python install >/dev/null
)

log "checking"
"$PREFIX/bin/wimlib-imagex" --version | head -1
"$PREFIX/sbin/mkntfs" --version 2>&1 | grep -m1 .
PYTHONPATH=$PREFIX/python "$PYTHON" -c 'import hivex; print("hivex Python module OK")'
rm -rf "$WORK"
log "done: built into $PREFIX"

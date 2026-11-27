#!/bin/sh
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

set -eu

output=${1:?output executable required}
source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
archive=tint2-17.1.3-2.tar.bz2
cd "$source_dir"
if [ ! -f "$archive" ]; then
  curl -fsSL --retry 3 -o "$archive" "https://distfiles.alpinelinux.org/distfiles/v3.24/$archive"
fi
sha512sum -c SHA512SUMS

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Tint2 can have events in Xlib's queue after the socket is no longer readable.
# Drain that queue before waiting in pselect (#305).
tar -xjf "$archive" -C "$work"
source=$work/tint2-17.1.3
patch --batch --forward --fuzz=0 -d "$source" -p1 < drain-queued-events.patch
cmake3.5 -S "$source" -B "$work/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=None \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DCMAKE_INSTALL_SYSCONFDIR=/etc \
  -DCMAKE_INSTALL_LIBDIR=lib
cmake --build "$work/build" --target tint2
install -D -m 755 "$work/build/tint2" "$output"
strip --strip-unneeded "$output"

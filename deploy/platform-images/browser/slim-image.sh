#!/bin/sh
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Build-time slimming for nova-ve-browser. The Dockerfile runs it in the same
# step as the package install (so nothing it removes ever reaches a layer);
# it needs apk and a C compiler and is not shipped.
#
# apk installs the station as one dependency closure of about 700 MB, and
# about 40% of it never runs on this desktop. apk will not uninstall those
# packages — firefox and xvfb declare them — so their files are removed here
# while apk's database is left as it is: an "apk add" inside a running
# station then does not pull them back in.
#
#   GL rasteriser   Xvfb hard-links libGL.so.1, which hard-links Mesa's
#                   libgallium, which hard-links LLVM (207 MiB). Nothing on
#                   this desktop can use GL: there is no GPU and Firefox
#                   already composites in software. libGL.so.1 stays (it is
#                   what Xvfb links) and libgallium is replaced by a stub
#                   answering the driver hooks libGL imports. Cost: the
#                   station's Firefox has no WebGL.
#   video codecs    firefox declares ffmpeg-libavcodec but only dlopen()s it
#                   (about 50 MiB with the codec libraries and Firefox's own
#                   FFmpeg glue). Cost: no H.264/HEVC/AAC playback; the
#                   built-in decoders (VP8/VP9/AV1, Opus/Vorbis) are fine.
#   bitmap fonts    xvfb declares X11's bitmap fonts; its own are built in.
#   DejaVu faces    condensed, extra-light, math and monospace-oblique
#                   variants that nothing here selects.
#
# The build fails if an ELF left behind has an unresolved library, or if Xvfb
# does not start on the result.

set -eu

GL_PACKAGES="mesa llvm22-libs spirv-tools mesa-egl mesa-gles"
CODEC_PACKAGES="ffmpeg-libavcodec ffmpeg-libavutil ffmpeg-libswresample x265-libs aom-libs libSvtAv1Enc libjxl x264-libs rav1e-libs libvorbis libtheora libgomp lame-libs soxr xvidcore libogg libwebpmux libhwy hwdata-pci libva libvdpau numactl libheif-aom libheif-jpeg libheif-rav1e libheif-svtenc libheif-x264 libheif-x265"
# Firefox's FFmpeg glue and the HEIF FFmpeg plug-in are loaded on demand.
CODEC_FILES="/usr/lib/firefox/libmozavcodec.so /usr/lib/firefox/libmozavutil.so /usr/lib/firefox/libmozinference.so /usr/lib/libheif/libheif-ffmpegdec.so"
# Everything else in font-dejavu goes.
KEEP_FACES="DejaVuSans.ttf DejaVuSans-Bold.ttf DejaVuSans-Oblique.ttf DejaVuSans-BoldOblique.ttf DejaVuSansMono.ttf DejaVuSansMono-Bold.ttf DejaVuSerif.ttf DejaVuSerif-Bold.ttf DejaVuSerif-Italic.ttf DejaVuSerif-BoldItalic.ttf"

loader="/lib/ld-musl-$(uname -m).so.1"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT INT TERM

fail() {
  echo "slim-image: $*" >&2
  exit 1
}

# Remove the files apk recorded for installed packages and drop the
# directories that leaves empty. apk keeps listing the packages.
drop() {
  local path dir
  for path in "$@"; do
    apk info -e "${path}" >/dev/null 2>&1 || fail "${path} is not installed"
  done
  apk info -L "$@" 2>/dev/null | grep -v ' contains:$' | grep -v '^$' | sed 's#^#/#' > "${tmp}/dropped"
  while IFS= read -r path; do rm -f "${path}"; done < "${tmp}/dropped"
  sed 's#^\(.*/\)[^/]*$#\1#' "${tmp}/dropped" | sort -u | while IFS= read -r dir; do
    dir="${dir%/}"
    while [ "${dir}" != /usr/lib ] && [ "${dir}" != /usr/share ]; do
      rmdir "${dir}" 2>/dev/null || break
      dir="$(dirname "${dir}")"
    done
  done
}

# Libraries that nothing provides any more, as "ELF -> library" lines.
unresolved_elfs() {
  local file
  find /bin /sbin /usr/bin /usr/sbin /usr/lib /usr/libexec -type f 2>/dev/null | while IFS= read -r file; do
    [ "$(head -c 4 "${file}" 2>/dev/null)" = "$(printf '\177ELF')" ] || continue
    "${loader}" --list "${file}" 2>&1 | awk -v file="${file}" '/Error loading shared library/ { print file " -> " $5 }'
  done
}

xvfb_starts() {
  local pid i
  Xvfb :99 -screen 0 320x240x24 -nolisten tcp > "${tmp}/xvfb.log" 2>&1 &
  pid=$!
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
      return 0
    fi
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  kill "${pid}" 2>/dev/null || true
  cat "${tmp}/xvfb.log" >&2
  return 1
}

apk add --no-cache --virtual .slim-tools gcc binutils

# ---- GL rasteriser -------------------------------------------------------
# libGL must keep resolving every symbol it imports from libgallium, so the
# stub is generated from the installed pair: one thread-local/object
# definition for each data symbol and a zero-returning hook for each function.
gallium="$(cd /usr/lib && ls libgallium-*.so 2>/dev/null | head -n 1)"
[ -n "${gallium}" ] || fail "Mesa's libgallium was not found"
[ -e /usr/lib/libGL.so.1 ] || fail "Mesa's libGL.so.1 was not found"
readelf -sW "/usr/lib/${gallium}" | awk '$4 == "TLS" { print $8 }' | sed 's/@.*//' | sort -u > "${tmp}/tls"
readelf -sW "/usr/lib/${gallium}" | awk '$4 == "OBJECT" { print $8 }' | sed 's/@.*//' | sort -u > "${tmp}/objects"
nm -D --defined-only "/usr/lib/${gallium}" | awk '{ print $3 }' | sed 's/@.*//' | sort -u > "${tmp}/exported"
nm -D --undefined-only /usr/lib/libGL.so.1 | awk '{ print $2 }' | sed 's/@.*//' | sort -u | comm -12 - "${tmp}/exported" > "${tmp}/imports"
[ -s "${tmp}/imports" ] || fail "libGL imports nothing from ${gallium}; Mesa's layout changed"
{
  echo "/* Generated by slim-image.sh: stand-in for Mesa's driver backend. */"
  comm -12 "${tmp}/imports" "${tmp}/tls" | sed 's/.*/__thread void *&;/'
  comm -12 "${tmp}/imports" "${tmp}/objects" | sed 's/.*/long &;/'
  cat "${tmp}/tls" "${tmp}/objects" | sort -u > "${tmp}/data"
  comm -23 "${tmp}/imports" "${tmp}/data" | sed 's/.*/long &(void) { return 0; }/'
} > "${tmp}/stub.c"
gcc -shared -nostdlib -Wl,-soname,"${gallium}" -o "${tmp}/${gallium}" "${tmp}/stub.c"
strip --strip-unneeded "${tmp}/${gallium}"

drop ${GL_PACKAGES}
install -m 755 "${tmp}/${gallium}" "/usr/lib/${gallium}"

# ---- Video codecs --------------------------------------------------------
drop ${CODEC_PACKAGES}
rm -f ${CODEC_FILES}

# ---- Fonts ---------------------------------------------------------------
drop font-cursor-misc font-misc-misc
for face in ${KEEP_FACES}; do
  [ -f "/usr/share/fonts/dejavu/${face}" ] || fail "font-dejavu no longer ships ${face}"
done
for face in /usr/share/fonts/dejavu/*.ttf; do
  case " ${KEEP_FACES} " in
    *" $(basename "${face}")"*) ;;
    *) rm -f "${face}" ;;
  esac
done
# The generated font indexes and caches list faces that are gone.
rm -f /usr/share/fonts/dejavu/fonts.dir /usr/share/fonts/dejavu/fonts.scale
fc-cache -f >/dev/null 2>&1
rm -rf /root/.cache/fontconfig

# ---- Guards --------------------------------------------------------------
unresolved_elfs > "${tmp}/unresolved"
if [ -s "${tmp}/unresolved" ]; then
  cat "${tmp}/unresolved" >&2
  fail "binaries with unresolved libraries (see above)"
fi
xvfb_starts || fail "Xvfb does not start on the slimmed image"
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /var/log/X99.log

apk del .slim-tools >/dev/null
echo "slim-image: done — $(apk info | wc -l) packages installed"

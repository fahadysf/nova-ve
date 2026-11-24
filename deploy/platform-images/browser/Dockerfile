FROM alpine:3.24 AS wallpaper

# Stage 1 — render the desktop wallpaper from the vector source. librsvg
# exists only in this builder stage; the final image ships just the PNG (a
# few KB — flat fills compress to almost nothing).
RUN apk add --no-cache rsvg-convert
COPY wallpaper.svg /tmp/wallpaper.svg
RUN rsvg-convert -o /tmp/wallpaper.png /tmp/wallpaper.svg


FROM alpine:3.24

# Platform container (#288): nova-ve-browser — the EVE eve-firefox analog.
# Current-stable Firefox (alpine 3.24 tracks the upstream release line with
# security updates) in a minimal openbox desktop with a tint2 taskbar for
# window switching and the nova-ve brand mark as the root-window wallpaper,
# presented through VNC on 5900 so the in-browser Guacamole VNC console
# shows it exactly like EVE presents eve-firefox. A busybox telnet debug
# shell on 23 backs the template's telnet second console.
#
# The taskbar matters: with no panel, a minimized Firefox/terminal had no
# way back on screen (the VNC console often eats Alt+Tab before it reaches
# openbox). tint2 gives every window a clickable button; see tint2rc.
#
# Rebuild the image (see deploy/scripts/build-platform-images.sh) to pick up
# newer Firefox releases from the alpine repos.
COPY --from=wallpaper /tmp/wallpaper.png /usr/local/share/nova-ve/wallpaper.png
COPY tint2rc /usr/local/share/nova-ve/tint2rc
COPY console-shell /usr/local/bin/console-shell
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY slim-image.sh /tmp/slim-image.sh
RUN <<'SH'
set -eu
# Install and slim in one step, so nothing the slimming removes ever reaches
# a layer: apk installs the station at about 700 MB, slim-image.sh takes it
# to about 410 MB (its header says what goes and what that costs).
apk add --no-cache \
      bash \
      busybox-extras \
      firefox \
      openbox \
      tint2 \
      xterm \
      xvfb \
      x11vnc \
      xsetroot \
      xwallpaper \
      xdpyinfo \
      iproute2 \
      iputils \
      util-linux \
      font-dejavu
/tmp/slim-image.sh
rm /tmp/slim-image.sh
chmod +x /usr/local/bin/console-shell /usr/local/bin/entrypoint.sh
SH

ENV DISPLAY=:0 \
    NOVA_VE_VNC_GEOMETRY=1280x800x24 \
    NOVA_VE_VNC_PORT=5900

EXPOSE 5900 23

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

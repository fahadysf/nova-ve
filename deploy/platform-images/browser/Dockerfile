FROM alpine:3.24

# Platform container (#288): nova-ve-browser — the EVE eve-firefox analog.
# Current-stable Firefox (alpine 3.24 tracks the upstream release line with
# security updates) in a minimal openbox desktop, presented through VNC on
# 5900 so the in-browser Guacamole VNC console shows it exactly like EVE
# presents eve-firefox. A busybox telnet debug shell on 23 backs the
# template's telnet second console.
#
# Rebuild the image (see deploy/scripts/build-platform-images.sh) to pick up
# newer Firefox releases from the alpine repos.
RUN apk add --no-cache \
      bash \
      busybox-extras \
      firefox \
      openbox \
      xterm \
      xvfb \
      x11vnc \
      xdpyinfo \
      iproute2 \
      iputils \
      util-linux \
      dbus \
      font-dejavu \
      ttf-freefont

COPY console-shell /usr/local/bin/console-shell
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/console-shell /usr/local/bin/entrypoint.sh

ENV DISPLAY=:0 \
    NOVA_VE_VNC_GEOMETRY=1280x800x24 \
    NOVA_VE_VNC_PORT=5900

EXPOSE 5900 23

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

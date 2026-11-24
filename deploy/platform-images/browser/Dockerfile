FROM alpine:3.24 AS wallpaper
RUN apk add --no-cache rsvg-convert
COPY browser/wallpaper.svg /tmp/wallpaper.svg
RUN rsvg-convert -o /tmp/wallpaper.png /tmp/wallpaper.svg

FROM alpine:3.24

# Browser Station keeps VNC available for existing lab nodes. Its Openbox
# session also starts in a dedicated xRDP/Xorg display for RDP consoles.
COPY --from=wallpaper /tmp/wallpaper.png /usr/local/share/nova-ve/wallpaper.png
COPY browser/tint2rc /usr/local/share/nova-ve/tint2rc
COPY browser/console-shell /usr/local/bin/console-shell
COPY browser/session /usr/local/bin/nova-ve-browser-session
COPY common/rdp-server /usr/local/bin/nova-ve-rdp-server
COPY common/configure-rdp browser/slim-image.sh /tmp/
# This image composites in software on both displays, with no virtual GPU.
# Avoid loading GLX in Xorg against the trimmed Mesa dependency.
RUN apk add --no-cache \
      bash \
      busybox-extras \
      curl \
      dbus \
      firefox \
      iproute2 \
      iputils \
      openbox \
      openssl \
      tini \
      tint2 \
      util-linux \
      xorg-server \
      xorgxrdp \
      xrdp \
      xterm \
      xvfb \
      x11vnc \
      xsetroot \
      xwallpaper \
      xdpyinfo \
      font-dejavu \
 && chmod +x /usr/local/bin/console-shell /tmp/configure-rdp /tmp/slim-image.sh \
 && /tmp/configure-rdp \
 && ln -sf /usr/local/bin/nova-ve-browser-session /etc/xrdp/startwm.sh \
 && sed -i '/Load "glx"/d; s|Option "DRMDevice" .*|Option "DRMDevice" ""|; s/Option "DRI3" "1"/Option "DRI3" "0"/' /etc/X11/xrdp/xorg.conf \
 && sed -i '/param=.xorgxrdp.%s.log/a param=-extension\nparam=GLX' /etc/xrdp/sesman.ini \
 && /tmp/slim-image.sh \
 && rm /tmp/configure-rdp /tmp/slim-image.sh

COPY browser/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV DISPLAY=:0 \
    NOVA_VE_VNC_GEOMETRY=1280x800x24 \
    NOVA_VE_VNC_PORT=5900

EXPOSE 3389 5900 23
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]

FROM alpine:3.24 AS wallpaper
RUN apk add --no-cache rsvg-convert
COPY browser/wallpaper.svg /tmp/wallpaper.svg
RUN rsvg-convert -o /tmp/wallpaper.png /tmp/wallpaper.svg

FROM alpine:3.24

COPY --from=wallpaper /tmp/wallpaper.png /usr/local/share/nova-ve/wallpaper.png
COPY browser/console-shell /usr/local/bin/console-shell
COPY common/rdp-server /usr/local/bin/nova-ve-rdp-server
COPY common/configure-rdp /tmp/configure-rdp
COPY desktop/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY desktop/session /usr/local/bin/nova-ve-desktop-session
COPY mgmt-server/requirements.txt /tmp/requirements.txt

# Install the MATE session components and lab tools used by the desktop.
RUN apk add --no-cache \
      bash busybox-extras dbus dconf font-dejavu openssl tini util-linux \
      xorg-server xorgxrdp xrdp xdpyinfo xrandr xset \
      adwaita-icon-theme caja marco mate-panel mate-session-manager \
      mate-settings-daemon mate-terminal mate-themes mate-control-center \
      mate-notification-daemon mate-screensaver mate-utils \
      engrampa eom pluma zathura zathura-pdf-poppler zathura-djvu \
      firefox chromium filezilla freerdp remmina vlc wireshark \
      bind-tools curl dhcpcd ethtool git iperf iperf3 iproute2 iputils \
      lftp mtr net-snmp-tools nmap-ncat openssh rsync tcpdump tftp-hpa \
      traceroute vim wget unzip \
      apache2 dnsmasq php85 php85-apache2 vsftpd \
      openjdk8-jre python3 py3-pip py3-lxml \
 && chmod +x /usr/local/bin/console-shell /tmp/configure-rdp \
 && /tmp/configure-rdp \
 && addgroup lab wireshark \
 && rm /tmp/configure-rdp \
 && ln -sf /usr/local/bin/nova-ve-desktop-session /etc/xrdp/startwm.sh \
 && python3 -m venv --system-site-packages /opt/nova-ve/venv \
 && /opt/nova-ve/venv/bin/pip install --no-cache-dir --no-compile -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

RUN apk add --no-cache asciidoctor dconf-editor libxml2-utils libxslt \
      pulseaudio pulseaudio-utils tshark
COPY desktop/90-nova-ve.gschema.override /usr/share/glib-2.0/schemas/
RUN glib-compile-schemas --strict /usr/share/glib-2.0/schemas

ENV PATH="/opt/nova-ve/venv/bin:${PATH}"
EXPOSE 3389 23
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]

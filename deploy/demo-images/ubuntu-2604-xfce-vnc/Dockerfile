FROM ubuntu:26.04

LABEL org.opencontainers.image.title="nova-ve/ubuntu-2604-xfce-vnc" \
      org.opencontainers.image.description="Ubuntu 26.04 XFCE lab container with passwordless VNC and NetworkManager" \
      org.opencontainers.image.source="https://github.com/accetto/ubuntu-vnc-xfce-g3" \
      org.opencontainers.image.vendor="nova-ve"

ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d; \
    chmod +x /usr/sbin/policy-rc.d; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      dbus \
      dbus-x11 \
      iproute2 \
      iputils-ping \
      less \
      net-tools \
      network-manager \
      procps \
      sudo \
      tini \
      x11-utils \
      x11-xserver-utils \
      x11vnc \
      xauth \
      xfonts-base \
      xterm \
      xvfb \
      xfce4 \
      xfce4-terminal; \
    rm -f /usr/sbin/policy-rc.d; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir -p /etc/NetworkManager/conf.d; \
    printf '[main]\nplugins=keyfile\n\n[ifupdown]\nmanaged=true\n\n[keyfile]\nunmanaged-devices=none\n' \
      >/etc/NetworkManager/conf.d/nova-ve-managed.conf

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV DISPLAY=:0 \
    NOVA_VE_VNC_GEOMETRY=1360x768x24 \
    NOVA_VE_VNC_PORT=5900 \
    VNC_PORT=5900

EXPOSE 5900

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

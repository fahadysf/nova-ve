FROM alpine:3.22

# x11vnc serves the Xvfb display; fluxbox + xterm give the operator something
# clickable on the desktop. iproute2/iputils are bundled so the typical lab
# "show me the interfaces / ping a neighbour" workflow works out of the box.
RUN apk add --no-cache \
      bash \
      busybox-extras \
      fluxbox \
      iproute2 \
      iputils \
      xdpyinfo \
      x11vnc \
      xterm \
      xvfb \
      util-linux

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV DISPLAY=:0 \
    NOVA_VE_VNC_GEOMETRY=1024x768x24 \
    NOVA_VE_VNC_PORT=5900

EXPOSE 5900

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

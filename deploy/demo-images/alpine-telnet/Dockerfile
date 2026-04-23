FROM alpine:3.22

RUN apk add --no-cache bash busybox-extras iproute2 iputils tmux util-linux

COPY console-shell /usr/local/bin/console-shell
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/console-shell /usr/local/bin/entrypoint.sh

EXPOSE 23

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

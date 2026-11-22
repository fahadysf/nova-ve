FROM python:3.13-alpine

# Platform container (#288): nova-ve-mgmt-server — the EVE eve-gui-server
# analog minus the GUI. A network-management workstation for lab nodes:
# current Python + ansible/napalm/netmiko (floors pinned in requirements.txt,
# latest satisfying versions installed at build time), sshd, and the same
# busybox telnet console shell the alpine-telnet demo image uses.
#
# Python 3.13 tracks the current stable upstream release; the alpine base
# carries current openssh/iperf3/tcpdump. Rebuild the image (see
# deploy/scripts/build-platform-images.sh) to pick up newer releases.
RUN apk add --no-cache \
      bash \
      busybox-extras \
      openssh-server \
      openssh-client \
      iperf3 \
      tcpdump \
      git \
      curl \
      iproute2 \
      iputils \
      util-linux

WORKDIR /opt/nova-ve
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY console-shell /usr/local/bin/console-shell
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/console-shell /usr/local/bin/entrypoint.sh

EXPOSE 23 22

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Platform images

Run `deploy/scripts/build-platform-images.sh` from the checkout to build the
images and add them to Nova VE's lab image catalog.

- **Browser Station** (`nova-ve/browser:latest`) provides Firefox, an Openbox
  desktop, a taskbar, a terminal, and the Nova VE wallpaper. Existing nodes
  can keep their VNC console. Choose RDP on the node to use a desktop that
  follows the size of its console window. VNC and RDP open separate Firefox
  sessions. Allocate more than 1 GiB to a node running both graphical
  consoles. For a standalone RDP-only container, set `NOVA_VE_BROWSER_VNC=0`.
- **Lab Desktop** (`nova-ve/desktop:latest`) provides an Alpine MATE desktop
  and the same wallpaper. It includes Firefox, Chromium, Wireshark,
  FileZilla, Remmina, FreeRDP, VLC, and tools for documents and archives.
  Network tools cover DNS, SNMP, DHCP, SSH, FTP, TFTP, packet capture, and
  throughput testing. Python, Ansible, NAPALM, Netmiko, Git, and a Java 8
  runtime are included. Start Apache/PHP, dnsmasq, SSH, FTP, and TFTP services
  as needed. The default console is RDP.

Both images have a secondary telnet shell for use on lab networks. In Nova VE,
the built-in RDP launcher connects as the unprivileged `lab` user. For a
standalone container, set `NOVA_VE_RDP_PASSWORD` at startup and connect with
that password on port 3389. Leave the variable unset to generate a password
at startup. A container administrator can retrieve it from
`/run/nova-ve/rdp-password`. Each container generates its own TLS certificate.

Verify a running image with
`deploy/scripts/verify-platform-images.sh browser <container>` or
`deploy/scripts/verify-platform-images.sh desktop <container>`.

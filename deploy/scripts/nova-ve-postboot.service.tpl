[Unit]
Description=nova-ve post-reboot bridge-cloud provisioning
DefaultDependencies=no
Requires=systemd-networkd.service
After=network-pre.target systemd-networkd.service
Before=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/nova-ve/bridge-cloud.state
RequiresMountsFor=/etc /var/log /run

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=300
ExecStartPre=/usr/bin/test -O /etc/nova-ve/bridge-cloud.state
ExecStartPre=/usr/bin/systemctl is-active --quiet systemd-networkd.service
ExecStartPre=/usr/bin/flock -n /run/nova-ve-postboot.lock true
ExecStart=/usr/bin/flock -n /run/nova-ve-postboot.lock /opt/nova-ve/bin/nova-ve-bridge-cloud.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nova-ve-postboot

[Install]
WantedBy=multi-user.target

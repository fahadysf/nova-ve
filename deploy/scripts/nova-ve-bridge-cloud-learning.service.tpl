[Unit]
Description=nova-ve Bridge-Cloud uplink MAC-learning disable
DefaultDependencies=no
Requires=systemd-networkd.service
After=systemd-networkd.service nova-ve-postboot.service
Before=network-online.target
Wants=network-online.target
ConditionPathExistsGlob=/sys/class/net/br-eth*

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=60
ExecStart=/opt/nova-ve/bin/nova-ve-bridge-cloud-learning.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nova-ve-bridge-cloud-learning

[Install]
WantedBy=multi-user.target

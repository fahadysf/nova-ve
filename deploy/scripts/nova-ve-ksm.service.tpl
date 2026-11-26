[Unit]
Description=nova-ve Kernel Samepage Merging tuning for lab density
# After the stock QEMU KVM preparation (writes run per /etc/default/qemu-kvm),
# before the backend so the first node started on a fresh boot is already
# benefiting rather than waiting for the next scan cycle.
After=qemu-kvm.service
Before=nova-ve-backend.service
# ksmtuned (pulled in by qemu-kvm on Ubuntu) toggles KSM on and off based on
# host memory pressure — off when memory is plentiful. That is the wrong shape
# for a lab host: KSM's whole value comes from running during the seconds
# after a lab boots, before the pages have been dirtied. Racing against it
# also means whichever unit writes /sys/kernel/mm/ksm/run last wins. Mask it
# here so the nova-ve tuning script is the only voice on this file.
Conflicts=ksmtuned.service
ConditionPathIsDirectory=/sys/kernel/mm/ksm

[Service]
Type=oneshot
RemainAfterExit=yes
# Belt-and-braces: mask ksmtuned before running the tuning script, so a fresh
# install can't have ksmtuned come back up and flip run=0 after us.
ExecStartPre=-/usr/bin/systemctl mask ksmtuned.service
ExecStartPre=-/usr/bin/systemctl stop ksmtuned.service
ExecStart=/opt/nova-ve/bin/nova-ve-ksm.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nova-ve-ksm

[Install]
WantedBy=multi-user.target

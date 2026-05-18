[Unit]
Description=nova-ve FastAPI backend
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=simple
User=__APP_OWNER__
Group=__APP_GROUP__
WorkingDirectory=__APP_ROOT__/backend
EnvironmentFile=/etc/nova-ve/backend.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=__APP_ROOT__/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3
# QEMU/docker/dynamips children spawned by the backend are spawned with
# start_new_session=True (own POSIX session) but stay inside the unit's
# cgroup, so the default KillMode=control-group would SIGKILL them on
# `systemctl restart`. KillMode=process makes systemd signal only the
# main uvicorn PID and leave the lab workloads running. The startup
# reconcile pass in NodeRuntimeService re-adopts surviving processes
# into the in-memory registry. Issue #225.
KillMode=process

[Install]
WantedBy=multi-user.target

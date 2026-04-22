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

[Install]
WantedBy=multi-user.target

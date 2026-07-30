#!/bin/bash
# Bootstrap the mock AWX+JIRA server as a systemd service on Amazon Linux 2023.
# The mock_server.py body is embedded below by the deploy step (see mocks/README.md).
set -euxo pipefail

mkdir -p /opt/mock
# The deploy process appends the mock_server.py contents to /opt/mock/mock_server.py
# via base64 in the launch step; if present at /opt/mock already, keep it.

cat >/etc/systemd/system/mock-awx-jira.service <<'UNIT'
[Unit]
Description=Mock AWX + JIRA server for Infrastructure Provisioning Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/mock/mock_server.py
Restart=always
RestartSec=3
User=root
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now mock-awx-jira.service

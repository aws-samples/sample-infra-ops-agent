"""Mock AWX + JIRA server for the Infrastructure Provisioning Agent.

Restores the demo's downstream integrations without real AWX/JIRA installs.
Serves ONLY the REST paths the agent actually calls:

JIRA (port 8080):
  GET  /rest/api/3/issue/{id}
  POST /rest/api/3/issue/{id}/comment
AWX (port 80):
  GET  /api/v2/ping/
  GET  /api/v2/job_templates/?name=<name>
  POST /api/v2/job_templates/{id}/launch/
  GET  /api/v2/jobs/{id}/

Returns the exact field/ID shapes the agent expects (customfield_10001/10003,
named job templates, job ids). Pure stdlib — no dependencies to install.
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ─── Seed data the agent expects ────────────────────────────────────────────

TICKETS = {
    "INFRA-1234": {
        "key": "INFRA-1234",
        "fields": {
            "summary": "Provision Linux EC2 for data-platform dev",
            "status": {"name": "Open"},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Jane Doe"},
            "description": "Need a t3.small Amazon Linux 2023 instance in development "
                           "for the data-platform project. Owner data-platform-team, "
                           "cost-center CC-4412.",
            "customfield_10001": "development",
            "customfield_10002": "data-platform",
            "customfield_10003": "1x t3.small, 50GB gp3, Amazon Linux 2023",
        },
    },
    "INFRA-1238": {
        "key": "INFRA-1238",
        "fields": {
            "summary": "Provision dev EC2 dev-infrateam-01",
            "status": {"name": "Open"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "Jane Doe"},
            "description": "Provision dev-infrateam-01 (t3.micro, Ubuntu 22.04) in "
                           "development for the infra-team project. Owner infra-team, "
                           "cost-center CC-1001.",
            "customfield_10001": "development",
            "customfield_10002": "infra-team",
            "customfield_10003": "1x t3.micro, 30GB gp3, Ubuntu 22.04",
        },
    },
}

JOB_TEMPLATES = [
    {"id": 7, "name": "Create Linux EC2 Instance"},
    {"id": 8, "name": "Create Windows EC2 Instance"},
    {"id": 9, "name": "PreCheck Assessment"},
    {"id": 10, "name": "OS Patch Management"},
]

# In-memory job store: job_id -> job record
_JOBS = {}
_JOB_SEQ = [1000]
_LOCK = threading.Lock()


def _new_job(template_id, template_name, extra_vars):
    with _LOCK:
        _JOB_SEQ[0] += 1
        jid = _JOB_SEQ[0]
        _JOBS[jid] = {
            "id": jid,
            "name": template_name,
            "job_template": template_id,
            "status": "pending",
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished": None,
            "elapsed": 0.0,
            "extra_vars": json.dumps(extra_vars),
            "_born": time.time(),
        }
    return _JOBS[jid]


def _job_view(job):
    """Advance a mock job through pending -> running -> successful over ~15s."""
    age = time.time() - job["_born"]
    if age < 3:
        job["status"] = "pending"
    elif age < 15:
        job["status"] = "running"
    else:
        if job["status"] != "successful":
            job["status"] = "successful"
            job["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    job["elapsed"] = round(age, 1)
    return {k: v for k, v in job.items() if not k.startswith("_")}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockAWXJira/1.0"

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # Keep container logs readable
        print("[mock] " + (fmt % args))

    # ─── GET ───
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # AWX ping
        if path == "/api/v2/ping/":
            return self._send(200, {"ha": False, "version": "mock-1.0", "active_node": "mock"})

        # AWX job template search
        if path == "/api/v2/job_templates/":
            name = (qs.get("name") or [""])[0]
            results = [t for t in JOB_TEMPLATES if t["name"] == name] if name else JOB_TEMPLATES
            return self._send(200, {"count": len(results), "results": results})

        # AWX job status
        m = re.match(r"^/api/v2/jobs/(\d+)/?$", path)
        if m:
            jid = int(m.group(1))
            job = _JOBS.get(jid)
            if not job:
                return self._send(404, {"detail": "Not found."})
            return self._send(200, _job_view(job))

        # JIRA issue lookup
        m = re.match(r"^/rest/api/[23]/issue/([A-Za-z0-9\-]+)/?$", path)
        if m:
            ticket = TICKETS.get(m.group(1).upper())
            if not ticket:
                return self._send(404, {"errorMessages": [f"Issue {m.group(1)} does not exist"]})
            return self._send(200, ticket)

        return self._send(404, {"detail": f"No mock route for GET {path}"})

    # ─── POST ───
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}

        # AWX launch
        m = re.match(r"^/api/v2/job_templates/(\d+)/launch/?$", path)
        if m:
            tid = int(m.group(1))
            tmpl = next((t for t in JOB_TEMPLATES if t["id"] == tid), None)
            if not tmpl:
                return self._send(404, {"detail": "Not found."})
            job = _new_job(tid, tmpl["name"], body.get("extra_vars", {}))
            return self._send(201, _job_view(job))

        # JIRA add comment
        m = re.match(r"^/rest/api/[23]/issue/([A-Za-z0-9\-]+)/comment/?$", path)
        if m:
            return self._send(201, {"id": "10000", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

        return self._send(404, {"detail": f"No mock route for POST {path}"})


def _serve(port):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[mock] listening on :{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    # AWX on 80, JIRA on 8080 — matches the agent's configured base URLs.
    threading.Thread(target=_serve, args=(8080,), daemon=True).start()
    _serve(80)

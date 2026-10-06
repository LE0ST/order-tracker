# Order Tracker — Observability & Automated Incident Response

A production-grade demonstration service built for **Homework 4 (DevOps and Observability for AI-Built Apps)** in the *AI Dev Tools Zoomcamp 2026*.

The project implements a complete telemetry, alerting, and automated incident response pipeline for a FastAPI order tracking system. It integrates OpenTelemetry, Prometheus, Grafana, Loki, Tempo, and a security-hardened headless incident response agent powered by OpenAI Codex.

---

## Architecture Overview

```text
+--------------------+        +---------------------------+
|    Order Tracker   | -----> |   OpenTelemetry Collector |
|   (FastAPI App)    |  OTLP  |      (Port 4317 / 4318)   |
+--------------------+        +-------------+-------------+
                                            |
         +----------------------------------+----------------------------------+
         |                                  |                                  |
         v                                  v                                  v
+------------------+              +------------------+               +------------------+
|    Prometheus    |              |   Grafana Loki   |               |  Grafana Tempo   |
|   (Metrics :9090)|              |   (Logs :3100)   |               |  (Traces :3200)  |
+--------+---------+              +--------+---------+               +--------+---------+
         |                                 |                                  |
         +---------------------------------+----------------------------------+
                                           |
                                           v
                             +---------------------------+
                             |          Grafana          |
                             |   (Dashboard & Alerts)    |
                             +-------------+-------------+
                                           | Webhook (POST /alerts)
                                           v
                             +---------------------------+
                             |     Incident Responder    |
                             |    (Port 8001 / Python)   |
                             +-------------+-------------+
                                           | Headless execution (gated)
                                           v
                             +---------------------------+
                             |     Autonomous Codex      |
                             |  (Triage & Root Cause Fix)|
                             +---------------------------+
```

---

## Quickstart

### Prerequisites
- Docker & Docker Compose
- Python 3.11+ and `uv` (for local development and tests)

### Run the Telemetry Stack
Start all services in background:
```bash
docker compose up --build -d
```

To stop all services:
```bash
docker compose down
```
*(Add `-v` only if you want to wipe persistent SQLite database volumes).*

### Service Endpoints

| Service | Endpoint | Purpose | Credentials |
| :--- | :--- | :--- | :--- |
| **Order Tracker App** | <http://localhost:8000> | Web UI & API (`/healthz`, `/api/orders`) | None |
| **Grafana** | <http://localhost:3000> | Unified Dashboards & Alert Rules | `admin` / `admin` (Anonymous Admin enabled) |
| **Prometheus** | <http://localhost:9090> | Time-series metrics engine & scraping | None |
| **Grafana Loki** | <http://localhost:3100> | Structured log aggregation engine | None |
| **Grafana Tempo** | <http://localhost:3200> | Distributed trace storage | None |
| **Incident Responder** | <http://localhost:8001> | Webhook receiver (`/healthz`, `/alerts`) | None |

---

## Telemetry & Observability Pipeline

1. **Distributed Tracing (Tempo)**:
   - Instrumented using OpenTelemetry Python SDK `TracerProvider`.
   - Propagates trace context across HTTP handlers (`get_order`, `list_orders`).
   - Automatically attaches exceptions to active spans with full stack traces.
2. **Metrics Collection (Prometheus)**:
   - Custom `http_requests_total` counter instrumented via OpenTelemetry `MeterProvider`.
   - Records request dimensions: `route` and `status_code`.
   - Pushed via OTLP to OpenTelemetry Collector, scraped every 15s by Prometheus.
3. **Structured Logging (Loki)**:
   - Configured with `LoggingHandler` attached to OpenTelemetry `LoggerProvider`.
   - Seamless correlation between logs, metrics, and traces via shared `trace_id` and `span_id`.
4. **Dashboards & Alerting (Grafana)**:
   - Pre-provisioned `OrderTracker` dashboard visualizing request rates, status code breakdowns, error logs, and trace spans.
   - Provisioned alert rule `OrderTracker5xxResponses` evaluating 5xx rates over 1-minute windows.
   - Contact point and notification policy routing alerts automatically to the Incident Responder webhook.

---

## Automated Incident Response & Remediation

The Incident Responder service (`incident-response/main.py`) acts as an automated site reliability engineer:

1. **Alert Ingestion**: Receives webhook alerts from Grafana at `POST /alerts`.
2. **Evidence Collection**:
   - Queries Loki for application and error logs surrounding the alert timeframe.
   - Extracts `trace_id` from correlated error logs and queries Tempo for the distributed trace tree.
   - Saves raw alert, extracted evidence, and full trace dumps into a bounded incident directory.
3. **Autonomous Investigation (OpenAI Codex)**:
   - Formulates a targeted diagnostic prompt containing incident labels, error logs, and trace exceptions.
   - Invokes OpenAI Codex headlessly in a bounded subprocess with strict execution timeouts.
   - Captures stdout, stderr, process exit codes, and repository changes.

---

## Incident Analysis & Root Cause (Question 6)

During the real incident simulation, a request was dispatched to:
```bash
curl -i http://localhost:8000/api/orders/express-1002
```
This triggered `HTTP 500 Internal Server Error` and fired the `OrderTracker5xxResponses` alert.

### Root Cause
In `app/main.py`, line 123 in `order_detail`:
```python
# Fragile date calculation:
estimated_at = placed_at.replace(day=placed_at.day + 2)
```
Order `express-1002` was created at the end of the previous month (e.g. September 30). Adding `2` to the integer day produced day 32, raising `ValueError: day is out of range for month`.

### Remediation
Codex automatically diagnosed the trace exception and applied the minimal robust fix:
```python
# Safe calendar arithmetic:
estimated_at = placed_at + timedelta(days=2)
```
Using `timedelta` correctly rolls across month and year boundaries (e.g., September 30 + 2 days = October 2).

---

## Security Architecture & Least Privilege

The incident responder is designed defensively according to least-privilege principles:

- **Read-Only Default**: The responder defaults strictly to read-only sandbox mode (`-s read-only`). In default operation, Codex performs read-only triage, logs diagnosis, and proposes patches in text without filesystem write permissions.
- **Explicit Write Gating**: Automated file modifications require setting `RESPONDER_ALLOW_WRITE_REMEDIATION=true` and matching an allowlisted alert name in `ALLOWED_WRITE_ALERTS`.
- **Dangerous Bypass Safeguard**: `RESPONDER_ALLOW_DANGEROUS_BYPASS=false` by default.
- **Platform Constraint Note**: On Windows platforms without OS-level container isolation (cgroups / seatbelt), Codex CLI rejects headless file writes under `-s workspace-write` unless bypassed. In production, read-only diagnostic triage is enforced by default rather than retaining an unsafe bypass.
- **Strict Workspace Boundary**: Execution is strictly pinned to the `order-tracker` repository (`assert REPO_ROOT.name == "order-tracker"`). Other workspaces (such as TaskLane) are strictly isolated and inaccessible.
- **No Git State Mutation**: Automatic git commits, pushes, and credential retrievals are strictly forbidden.

---

## Incident Evidence & Artifact Storage

Canonical incident evidence is preserved under `incident-response/incidents/`:

- **Q5 Synthetic Alert (`ResponderTest`)**: `incident-response/incidents/inc-20261006-002153-d6d6ae/`
- **Q6 Real Production Alert (`express-1002`)**: `incident-response/incidents/inc-20261006-055456-14bbcd/`

Each directory preserves:
- `alert.json`: Raw Grafana alert webhook payload.
- `evidence.json`: Correlated telemetry metadata and log snippets.
- `trace.json`: Tempo distributed trace tree with exception stack trace.
- `agent-prompt.txt`: Bounded prompt supplied to the assistant.
- `agent-final.txt`: Complete assistant diagnostic response and root cause summary.
- `metadata.json`: Invocation runtime parameters, execution duration, and exit codes.
- `git-diff.txt`: Repository patch diff generated by the assistant.

Future runtime incidents are ignored by default via `incident-response/incidents/.gitignore` to prevent repository artifact sprawl.

---

## Automated Tests

Run the complete test suite with `uv`:
```bash
uv run pytest -v
```

The test suite covers:
- Core API health checks and CRUD order operations.
- Regression test for `express-1002` seeded order delivery calculation.
- Parameterized edge case regression tests across 30-day, 31-day, leap February, non-leap February, and year-end boundaries.
- Incident Responder security policy unit tests verifying default read-only enforcement and bypass disabling.

---

## Homework 4 Reference Answers

| Question | Context / Telemetry Evidence | Verified Answer |
| :--- | :--- | :--- |
| **Q1** | What does the health check return?<br>• Endpoint: `GET /healthz` (`http://localhost:8000/healthz`) | `{"status":"ok"}` |
| **Q2** | Which HTTP status code does the metric record for the successful lookup?<br>• Metric: `http_requests_total{status_code="200", route="/api/orders/{order_id}"}` | `200` |
| **Q3** | Which HTTP status code does the metric show in Grafana for the second lookup?<br>• Metric: `http_requests_total{status_code="404", route="/api/orders/{order_id}"}` | `404` |
| **Q4** | What state does Grafana show for the 5xx alert?<br>• Alert Rule: `OrderTracker5xxResponses` | `Normal` (state: `Normal (NoData)`) |
| **Q5** | What did the agent respond? Include the last line from its answer.<br>• Evidence: `incident-response/incidents/inc-20261006-002153-d6d6ae/agent-final.txt` | `Understood. I’ll investigate Order Tracker incidents, identify the cause, and report findings and remediation. This session has read-only filesystem access, so I can inspect the service but cannot apply changes.` |
| **Q6** | What was the root cause of the incident?<br>• Location: `app/main.py` (function `order_detail`)<br>• Failure: `placed_at.replace(day=placed_at.day + 2)`<br>• Fix: `placed_at + timedelta(days=2)` | **Option A**: *The express delivery date calculation tried to use a day that does not exist in that month.* |

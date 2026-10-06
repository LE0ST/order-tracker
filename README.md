# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

---

## Homework 4: Observability & Automated Incident Response

This repository has been extended to complete **Homework 4 (DevOps and Observability for AI-Built Apps)** of the *AI Dev Tools Zoomcamp 2026*.

### Verified Answers to Course Questions

| Question | Question Prompt | Verified Answer |
| :--- | :--- | :--- |
| **Q1** | What does the health check return? | `{"status":"ok"}` |
| **Q2** | Which HTTP status code does the metric record for the successful lookup? | `200` |
| **Q3** | Which HTTP status code does the metric show in Grafana for the second lookup? | `404` |
| **Q4** | What state does Grafana show for the 5xx alert? | `Normal` (or `Normal (NoData)`) |
| **Q5** | What did the agent respond? Include the last line from its answer. | `Understood. I’ll investigate Order Tracker incidents, identify the cause, and report findings and remediation. This session has read-only filesystem access, so I can inspect the service but cannot apply changes.` |
| **Q6** | What was the root cause of the incident? | **Option A**: *The express delivery date calculation tried to use a day that does not exist in that month.* |

---

### Observability Pipeline Architecture

The full telemetry stack is orchestrated via Docker Compose:

1. **Instrumentation (`app/main.py`)**:
   - OpenTelemetry Python SDK instrumented for traces, metrics, and structured logs.
   - Pushes OTLP HTTP payloads to `http://otel-collector:4318`.
   - Records `http_requests_total` counter with route and status code attributes.
   - Propagates distributed trace context (`trace_id`, `span_id`) through logs and spans.
2. **OpenTelemetry Collector (`otel-collector`)**:
   - Receives OTLP traces, metrics, and logs on port 4318 (HTTP) and 4317 (gRPC).
   - Exports metrics via Prometheus exporter on port 8889.
   - Exports logs to Loki (`http://loki:3100/otlp`).
   - Exports traces to Tempo (`http://tempo:4317`).
3. **Storage & Engines**:
   - **Prometheus** (`:9090`): Scrapes OpenTelemetry Collector every 15s.
   - **Grafana Loki** (`:3100`): Ingests and indexes structured application logs.
   - **Grafana Tempo** (`:3200`): Stores trace spans and dependency graphs.
4. **Grafana Dashboards & Alerting (`:3000`)**:
   - Provisioned Prometheus, Loki, and Tempo data sources with trace-to-logs and logs-to-traces correlation.
   - Pre-provisioned `OrderTracker` dashboard.
   - Alert Rule `OrderTracker5xxResponses` evaluating `http_requests_total{status_code=~"5.."}`.
   - Notification policy directing alerts to the automated incident responder webhook (`http://host.docker.internal:8001/alerts`).

---

### Incident Responder Architecture & Security Policy

The automated incident responder is located in `incident-response/main.py` and listens on port 8001:

- **Webhook Ingestion**: Exposes `POST /alerts` accepting Grafana alert webhooks.
- **Evidence Gathering**: Queries Loki for recent log lines and Tempo for trace details matching the incident.
- **Bounded Assistant Invocation**: Runs OpenAI Codex headlessly with strict execution boundaries:
  - Ephemeral execution (`--ephemeral`), non-daemon mode (`--no-daemon`).
  - Strict timeout (180 seconds).
  - Explicit capture of exit codes, stdout, stderr, and working tree git diff.
- **Security & Least-Privilege Policy Controls**:
  - **Default Mode: Read-Only**: The responder operates by default in `read-only` sandbox mode (`-s read-only`), diagnosing root causes and proposing patch diffs without modifying files.
  - **Explicit Write Gating**: Automated write remediation is strictly gated by `RESPONDER_ALLOW_WRITE_REMEDIATION=false` (default) and an allowlist of permitted alert names (`ALLOWED_WRITE_ALERTS`).
  - **Dangerous Sandbox Bypass Disabled**: `RESPONDER_ALLOW_DANGEROUS_BYPASS=false` by default.
  - **Platform Constraint Note**: On Windows platforms without OS-level container sandboxing (bubblewrap/cgroups), Codex CLI rejects headless file writes under `-s workspace-write` unless `--dangerously-bypass-approvals-and-sandbox` is supplied. Rather than retaining an unsafe bypass in production, the responder enforces safe read-only operation by default.
  - **Workspace Isolation**: Execution is strictly pinned to `order-tracker`. External directories (e.g., TaskLane) are strictly isolated and inaccessible.
  - **No Automatic Commit/Push**: Automatic commits or pushes to Git repositories are strictly prohibited.

---

### Incident Investigation Summary (Question 6 Root Cause)

- **Incident Trigger**: `GET /api/orders/express-1002` returned `HTTP 500 Internal Server Error`.
- **Seeded Data**: Order `express-1002` was created with `created_at` set to the previous month's end date (e.g. September 30).
- **Failure Mechanism**: In `app/main.py:123`, the delivery calculation was implemented as:
  ```python
  estimated_at = placed_at.replace(day=placed_at.day + 2)
  ```
  Adding `2` to the day integer on month-end dates attempted to construct an invalid date (e.g. day 32), raising `ValueError: day is out of range for month`.
- **Remediation**:
  ```python
  estimated_at = placed_at + timedelta(days=2)
  ```
  Using `timedelta` safely rolls across calendar month and year boundaries.
- **Verification**: Post-fix lookup of `express-1002` returned `HTTP 200 OK` with `estimated_delivery: "2026-10-02"`, pytest passed with 3/3 tests, and the Grafana alert returned to `Normal`.

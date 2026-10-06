import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("incident-responder")

REPO_ROOT = Path(__file__).resolve().parent.parent
# Workspace isolation enforcement: TaskLane and other external directories are strictly out of scope.
assert REPO_ROOT.name == "order-tracker", f"Unexpected repo root: {REPO_ROOT}"

INCIDENT_RESPONSE_DIR = Path(__file__).resolve().parent
INCIDENTS_DIR = INCIDENT_RESPONSE_DIR / "incidents"
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)

# Security & Remediation Policy Controls:
# By default, automatic write-capable remediation is disabled.
# The responder defaults to read-only incident investigation and diagnosis.
RESPONDER_ALLOW_WRITE_REMEDIATION = os.getenv("RESPONDER_ALLOW_WRITE_REMEDIATION", "false").lower() in ("true", "1", "yes")

# Dangerous sandbox bypass is disabled by default. On Windows, Codex CLI lacks OS-level sandboxing
# (bubblewrap/cgroups are Linux-only), so write remediation in headless mode would fail without a bypass.
# Rather than keeping an unsafe bypass enabled by default, the responder defaults to read-only investigation.
RESPONDER_ALLOW_DANGEROUS_BYPASS = os.getenv("RESPONDER_ALLOW_DANGEROUS_BYPASS", "false").lower() in ("true", "1", "yes")

# Explicit allowlist of alert names permitted for write remediation (if enabled by policy)
ALLOWED_WRITE_ALERTS = {"OrderTracker5xxResponses"}


def determine_sandbox_mode(alert_name: str, is_test_alert: bool) -> tuple[str, list[str]]:
    """
    Determine the sandbox mode and CLI arguments based on security policy.
    Defaults to read-only mode. Write remediation is explicitly gated,
    and dangerous bypass is disabled by default.
    """
    if is_test_alert:
        return "read-only", ["-s", "read-only"]

    can_write = RESPONDER_ALLOW_WRITE_REMEDIATION and (alert_name in ALLOWED_WRITE_ALERTS)
    if can_write:
        if RESPONDER_ALLOW_DANGEROUS_BYPASS:
            return "workspace-write-bypassed", ["-s", "workspace-write", "--dangerously-bypass-approvals-and-sandbox"]
        return "workspace-write", ["-s", "workspace-write"]

    return "read-only", ["-s", "read-only"]


# Locate Codex executable
NATIVE_CODEX = Path(r"C:\Users\RENEC\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe")
if NATIVE_CODEX.exists():
    CODEX_EXE = str(NATIVE_CODEX)
else:
    CODEX_EXE = shutil.which("codex") or r"C:\Users\RENEC\AppData\Roaming\npm\codex.cmd"

app = FastAPI(title="Incident Responder", description="Automatic Incident Responder for Order Tracker")


def get_git_status() -> str:
    """Safely capture short git status without modifying repository."""
    try:
        res = subprocess.run(
            ["git", "status", "--short"],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return res.stdout.strip()
    except Exception as exc:
        return f"Error capturing git status: {exc}"


def get_git_diff() -> str:
    """Safely capture git diff without modifying repository."""
    try:
        res = subprocess.run(
            ["git", "diff"],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return res.stdout.strip()
    except Exception as exc:
        return f"Error capturing git diff: {exc}"


def fetch_bounded_loki_logs(service_name: str = "order-tracker", limit: int = 20) -> List[Dict[str, Any]]:
    """Query Loki for recent logs in a bounded, non-blocking way."""
    try:
        url = "http://localhost:3100/loki/api/v1/query_range"
        params = {
            "query": f'{{service_name="{service_name}"}}',
            "limit": limit,
        }
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("result", [])
                logs = []
                for stream_item in data:
                    stream_labels = stream_item.get("stream", {})
                    for val in stream_item.get("values", []):
                        logs.append({
                            "timestamp_ns": val[0],
                            "message": val[1],
                            "trace_id": stream_labels.get("trace_id"),
                            "span_id": stream_labels.get("span_id"),
                            "level": stream_labels.get("detected_level", stream_labels.get("severity_text", "INFO")),
                        })
                return logs
    except Exception as exc:
        logger.warning(f"Could not query Loki: {exc}")
    return []


def fetch_bounded_tempo_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    """Query Tempo for trace data in a bounded, non-blocking way."""
    if not trace_id:
        return None
    try:
        url = f"http://localhost:3200/api/traces/{trace_id}"
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.warning(f"Could not query Tempo for trace {trace_id}: {exc}")
    return None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "incident-responder", "port": 8001}


@app.post("/alerts")
async def receive_alerts(request: Request):
    """
    Accept Grafana-style alert webhook payloads, collect evidence,
    and invoke the headless coding assistant.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}")

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    incident_id = f"inc-{timestamp_str}-{uuid4().hex[:6]}"
    incident_dir = INCIDENTS_DIR / incident_id
    incident_dir.mkdir(parents=True, exist_ok=True)

    # 1. Persist raw alert payload
    alert_json_path = incident_dir / "alert.json"
    with open(alert_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    # Extract primary alert details
    alerts_list = payload.get("alerts", [])
    primary_alert = alerts_list[0] if alerts_list else {}
    alert_name = primary_alert.get("labels", {}).get("alertname", "UnknownAlert")
    alert_status = primary_alert.get("status", "unknown")
    labels = primary_alert.get("labels", {})
    annotations = primary_alert.get("annotations", {})
    summary = annotations.get("summary", "")
    is_test_alert = labels.get("test") == "true" or alert_name == "ResponderTest"
    affected_endpoint = labels.get("route") or labels.get("endpoint") or annotations.get("endpoint") or "N/A"

    # 2. Collect bounded evidence
    loki_logs = fetch_bounded_loki_logs(limit=15)
    trace_id_found = None
    for log_entry in loki_logs:
        if log_entry.get("trace_id"):
            trace_id_found = log_entry["trace_id"]
            break

    trace_data = None
    if trace_id_found:
        trace_data = fetch_bounded_tempo_trace(trace_id_found)
        if trace_data:
            with open(incident_dir / "trace.json", "w", encoding="utf-8") as f:
                json.dump(trace_data, f, indent=2)

    evidence = {
        "incident_id": incident_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alert_name": alert_name,
        "status": alert_status,
        "is_test_alert": is_test_alert,
        "affected_endpoint": affected_endpoint if not is_test_alert else "N/A (synthetic test notification)",
        "labels": labels,
        "annotations": annotations,
        "summary": summary,
        "evidence_summary": (
            "Synthetic test alert: no real service incident or failure to fix."
            if is_test_alert
            else f"Real alert for endpoint: {affected_endpoint}"
        ),
        "recent_logs": loki_logs,
        "trace_id": trace_id_found,
        "has_trace_data": trace_data is not None,
    }

    evidence_json_path = incident_dir / "evidence.json"
    with open(evidence_json_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2)

    # 3. Build agent prompt & configure sandbox mode
    logs_snippet = "\n".join([f"[{l.get('level', 'INFO')}] {l.get('message')}" for l in loki_logs[:10]]) if loki_logs else "No recent application logs recorded."

    if is_test_alert:
        sandbox_mode = "read-only"
        sandbox_args = ["-s", "read-only"]
        agent_prompt = f"""You are the automated incident responder for the Order Tracker service.
An alert was received from the monitoring system.

Alert Details:
- Incident ID: {incident_id}
- Alert Name: {alert_name}
- Status: {alert_status}
- Summary: {summary}
- Labels: {json.dumps(labels)}
- Annotations: {json.dumps(annotations)}
- Affected Endpoint: {evidence['affected_endpoint']}
- Synthetic / Test Notification: True

Recent Application Logs:
{logs_snippet}

Instructions:
1. Analyze this alert and the evidence provided above.
2. If this is a synthetic test notification stating "Test notification; no incident to fix", confirm whether there is an actual active service incident.
3. State clearly that this is a test notification requiring no remediation or code modifications.
4. Mode: READ-ONLY. Do not edit files, do not commit, do not push, and do not make changes to the repository.
5. Provide a clear, concise final summary. Conclude with a single clear final line stating your conclusion.
"""
    else:
        # REAL PRODUCTION INCIDENT
        sandbox_mode, sandbox_args = determine_sandbox_mode(alert_name, is_test_alert=False)
        can_write = sandbox_mode.startswith("workspace-write")
        if can_write:
            task_instructions = f"""Your Task:
1. Investigate the root cause of the 5xx server error on the affected endpoint in the Order Tracker codebase at {REPO_ROOT} (specifically app/main.py).
   The application log reports: "Error looking up order express-1002: day is out of range for month".
2. Fix the bug directly in app/main.py by making the smallest safe, robust code change in the repository. In function order_detail, ensure the express delivery date calculation handles month boundaries safely using timedelta(days=2) (i.e. placed_at + timedelta(days=2)) instead of placed_at.replace(day=placed_at.day + 2).
3. Do NOT commit, do NOT push, and do NOT modify Git configuration or Docker infrastructure.
4. Report your findings:
   - What was the exact root cause of the incident?
   - What exact file(s) and lines were changed?
   - How does this fix prevent future recurrences?
5. Conclude your response with a single clear final summary line stating the root cause."""
        else:
            # Default safe production mode: READ-ONLY investigation and diagnosis
            task_instructions = f"""Your Task:
1. Investigate the root cause of the 5xx server error on the affected endpoint in the Order Tracker codebase at {REPO_ROOT} (specifically app/main.py).
   The application log reports: "Error looking up order express-1002: day is out of range for month".
2. Analyze the bug in app/main.py and diagnose the root cause. Formulate the exact code remediation needed.
3. Mode: READ-ONLY. Do NOT modify any files on disk, do NOT commit, and do NOT push.
4. Report your findings:
   - What was the exact root cause of the incident?
   - What exact file(s) and lines are responsible?
   - What is the proposed fix to prevent future recurrences?
5. Conclude your response with a single clear final summary line stating the root cause."""

        agent_prompt = f"""You are the automated incident responder for the Order Tracker service.
A REAL PRODUCTION INCIDENT ALERT has fired from the monitoring system.

Incident Details:
- Incident ID: {incident_id}
- Alert Name: {alert_name}
- Status: {alert_status}
- Summary: {summary}
- Affected Endpoint: {evidence['affected_endpoint']}
- Labels: {json.dumps(labels)}
- Annotations: {json.dumps(annotations)}
- Trace ID: {evidence.get('trace_id', 'N/A')}

Recent Application & Error Logs:
{logs_snippet}

{task_instructions}
"""

    prompt_path = incident_dir / "agent-prompt.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(agent_prompt)

    # 4. Record git status BEFORE
    git_status_before = get_git_status()

    # 5. Invoke Codex headlessly
    agent_final_path = incident_dir / "agent-final.txt"
    stdout_path = incident_dir / "agent-stdout.txt"
    stderr_path = incident_dir / "agent-stderr.txt"
    metadata_path = incident_dir / "metadata.json"

    cmd = [
        CODEX_EXE,
        "--no-daemon",
        "exec",
        "--ignore-user-config",
        *sandbox_args,
        "--ephemeral",
        "-o", str(agent_final_path),
        "-",
    ]

    timeout_seconds = 180
    timed_out = False
    exit_code = -1
    stdout_text = ""
    stderr_text = ""

    start_time = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            input=agent_prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = proc.returncode
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -9
        stdout_text = exc.stdout or ""
        stderr_text = exc.stderr or "Process timed out."
        logger.error(f"Codex execution timed out after {timeout_seconds} seconds")
    except Exception as exc:
        exit_code = -1
        stderr_text = str(exc)
        logger.error(f"Error executing Codex: {exc}")
    end_time = datetime.now(timezone.utc)

    # 6. Record git status & diff AFTER
    git_status_after = get_git_status()
    git_diff_after = get_git_diff()

    if git_diff_after:
        with open(incident_dir / "git-diff.txt", "w", encoding="utf-8") as f:
            f.write(git_diff_after)

    # 7. Persist outputs
    with open(stdout_path, "w", encoding="utf-8") as f:
        f.write(stdout_text)
    with open(stderr_path, "w", encoding="utf-8") as f:
        f.write(stderr_text)

    # Read final answer
    final_answer = ""
    if agent_final_path.exists():
        with open(agent_final_path, "r", encoding="utf-8") as f:
            final_answer = f.read().strip()
    if not final_answer and stdout_text:
        # Fallback to stdout
        final_answer = stdout_text.strip()

    # Identify exact last line
    non_empty_lines = [line.strip() for line in final_answer.splitlines() if line.strip()]
    last_line = non_empty_lines[-1] if non_empty_lines else ""

    metadata = {
        "incident_id": incident_id,
        "alert_name": alert_name,
        "sandbox_mode": sandbox_mode,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_seconds": (end_time - start_time).total_seconds(),
        "cli": "codex",
        "command": cmd,
        "cwd": str(REPO_ROOT),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "git_status_before": git_status_before,
        "git_status_after": git_status_after,
        "has_git_diff": bool(git_diff_after),
        "exact_last_line": last_line,
    }

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    logger.info(f"Processed incident {incident_id} (exit code: {exit_code}, sandbox: {sandbox_mode}, last line: {last_line})")

    return JSONResponse(
        status_code=200,
        content={
            "status": "handled",
            "incident_id": incident_id,
            "alertname": alert_name,
            "sandbox_mode": sandbox_mode,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "agent_final": final_answer,
            "last_line": last_line,
            "git_status_before": git_status_before,
            "git_status_after": git_status_after,
            "has_git_diff": bool(git_diff_after),
            "incident_dir": str(incident_dir),
        },
    )


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    """Retrieve stored incident artifacts."""
    incident_dir = INCIDENTS_DIR / incident_id
    if not incident_dir.exists():
        raise HTTPException(status_code=404, detail="Incident not found")

    result = {}
    for filename in ["alert.json", "evidence.json", "metadata.json", "agent-final.txt", "git-diff.txt"]:
        file_path = incident_dir / filename
        if file_path.exists():
            if filename.endswith(".json"):
                with open(file_path, "r", encoding="utf-8") as f:
                    result[filename] = json.load(f)
            else:
                with open(file_path, "r", encoding="utf-8") as f:
                    result[filename] = f.read()
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)

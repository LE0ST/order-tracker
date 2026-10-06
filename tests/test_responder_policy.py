import sys
from pathlib import Path

# Add incident-response directory to sys.path to test policy logic
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "incident-response"))

import main as responder_main


def test_default_policy_is_read_only(monkeypatch):
    """By default, without environment variables, all alerts execute in read-only sandbox mode."""
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_WRITE_REMEDIATION", False)
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_DANGEROUS_BYPASS", False)

    # Synthetic test alert
    mode, args = responder_main.determine_sandbox_mode("ResponderTest", is_test_alert=True)
    assert mode == "read-only"
    assert args == ["-s", "read-only"]

    # Real production alert
    mode, args = responder_main.determine_sandbox_mode("OrderTracker5xxResponses", is_test_alert=False)
    assert mode == "read-only"
    assert args == ["-s", "read-only"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_dangerous_bypass_disabled_by_default(monkeypatch):
    """Even if write remediation is enabled, dangerous bypass remains disabled by default."""
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_WRITE_REMEDIATION", True)
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_DANGEROUS_BYPASS", False)

    mode, args = responder_main.determine_sandbox_mode("OrderTracker5xxResponses", is_test_alert=False)
    assert mode == "workspace-write"
    assert args == ["-s", "workspace-write"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_unauthorized_alert_cannot_trigger_write(monkeypatch):
    """An alert not on ALLOWED_WRITE_ALERTS cannot trigger write remediation."""
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_WRITE_REMEDIATION", True)
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_DANGEROUS_BYPASS", True)

    mode, args = responder_main.determine_sandbox_mode("UnknownAlert", is_test_alert=False)
    assert mode == "read-only"
    assert args == ["-s", "read-only"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_authorized_write_when_both_flags_explicitly_enabled(monkeypatch):
    """Write remediation with bypass is only active when both flags are explicitly enabled."""
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_WRITE_REMEDIATION", True)
    monkeypatch.setattr(responder_main, "RESPONDER_ALLOW_DANGEROUS_BYPASS", True)

    mode, args = responder_main.determine_sandbox_mode("OrderTracker5xxResponses", is_test_alert=False)
    assert mode == "workspace-write-bypassed"
    assert "--dangerously-bypass-approvals-and-sandbox" in args

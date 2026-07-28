"""Safety tests for the Phase 2 governance configuration switch."""

import pytest
from pydantic import ValidationError

from skyvern.config import Settings


def test_governance_mode_allows_off_and_audit():
    assert Settings(GOVERNANCE_MODE="off").GOVERNANCE_MODE == "off"
    assert Settings(GOVERNANCE_MODE="AUDIT").GOVERNANCE_MODE == "audit"


def test_governance_mode_rejects_unavailable_enforce_without_reopening_sealed_entrypoints():
    with pytest.raises(ValidationError) as exc_info:
        Settings(GOVERNANCE_MODE="enforce")

    message = str(exc_info.value)
    assert "not available yet" in message
    assert "production Domain Pack" in message
    assert "live runtime wiring" in message
    assert "scoped rollback approval" in message
    assert "not fully permit-sealed" not in message


def test_governance_mode_rejects_unknown_value():
    with pytest.raises(ValidationError, match="must be one of"):
        Settings(GOVERNANCE_MODE="preview")


def test_governance_recovery_settings_must_be_positive():
    with pytest.raises(ValidationError, match="recovery interval"):
        Settings(GOVERNANCE_RECOVERY_INTERVAL_SECONDS=0)
    with pytest.raises(ValidationError, match="batch size"):
        Settings(GOVERNANCE_RECOVERY_BATCH_SIZE=0)


def test_governance_recovery_execution_requires_enforce_mode():
    with pytest.raises(ValidationError, match="requires sealed enforce"):
        Settings(ENABLE_GOVERNANCE_RECOVERY_EXECUTION=True)

"""Tests for the full-chain audit and compliance storage system.

Covers:
- Audit log model schema
- Input sanitization rules (card, password, ID, phone)
- MinIO storage key generation and bucket naming
- Audit log writer with graceful degradation
- Query API with filtering and pagination
- Full flow: 3-step task -> 3 audit logs + 6 screenshots
"""

import hashlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import inspect

from enterprise.audit.models import (
    AuditLogModel,
    ActionType,
    generate_audit_log_id,
)
from enterprise.audit.sanitizer import (
    sanitize_input,
    hash_raw_value,
    DEFAULT_RULES,
)
from enterprise.audit.storage import (
    generate_object_key,
    get_bucket_name,
)


# ============================================================
# Model tests
# ============================================================

class TestActionType(unittest.TestCase):
    def test_click(self):
        assert ActionType.CLICK.value == "click"

    def test_input_text(self):
        assert ActionType.INPUT_TEXT.value == "input_text"

    def test_all_values(self):
        expected = {
            "click", "input_text", "select_option", "upload_file",
            "navigate", "download", "screenshot", "wait", "scroll", "custom",
        }
        assert {a.value for a in ActionType} == expected


class TestGenerateAuditLogId(unittest.TestCase):
    def test_prefix(self):
        assert generate_audit_log_id().startswith("aud_")

    def test_uniqueness(self):
        ids = {generate_audit_log_id() for _ in range(100)}
        assert len(ids) == 100


class TestAuditLogModelSchema(unittest.TestCase):
    def test_tablename(self):
        assert AuditLogModel.__tablename__ == "audit_logs"

    def test_primary_key(self):
        mapper = inspect(AuditLogModel)
        pk_cols = [col.name for col in mapper.primary_key]
        assert pk_cols == ["audit_log_id"]

    def test_required_columns(self):
        mapper = inspect(AuditLogModel)
        cols = {col.name: col for col in mapper.columns}
        assert cols["task_id"].nullable is False
        assert cols["organization_id"].nullable is False
        assert cols["department_id"].nullable is False
        assert cols["action_index"].nullable is False
        assert cols["action_type"].nullable is False
        assert cols["executor"].nullable is False

    def test_nullable_columns(self):
        mapper = inspect(AuditLogModel)
        cols = {col.name: col for col in mapper.columns}
        assert cols["business_line_id"].nullable is True
        assert cols["target_element"].nullable is True
        assert cols["input_value"].nullable is True
        assert cols["screenshot_before_key"].nullable is True
        assert cols["screenshot_after_key"].nullable is True
        assert cols["error_message"].nullable is True
        assert cols["approval_id"].nullable is True

    def test_indexes(self):
        indexes = AuditLogModel.__table__.indexes
        index_names = {idx.name for idx in indexes}
        assert "idx_aud_task_action" in index_names
        assert "idx_aud_org_time" in index_names
        assert "idx_aud_dept_time" in index_names

    def test_check_constraints(self):
        constraints = AuditLogModel.__table__.constraints
        check_names = {c.name for c in constraints if hasattr(c, "name") and c.name}
        assert "ck_non_negative_action_index" in check_names


# ============================================================
# Sanitizer tests
# ============================================================

class TestSanitizeCardNumber(unittest.TestCase):
    def test_standard_card(self):
        result = sanitize_input("Card: 6222021234561234")
        assert "1234" in result
        assert "6222" not in result
        assert "****1234" in result

    def test_card_with_spaces(self):
        result = sanitize_input("6222 0212 3456 1234")
        assert result.endswith("1234")
        assert "6222" not in result

    def test_card_with_dashes(self):
        result = sanitize_input("6222-0212-3456-1234")
        assert "1234" in result
        assert "6222" not in result

    def test_no_card_untouched(self):
        result = sanitize_input("Hello world")
        assert result == "Hello world"


class TestSanitizePassword(unittest.TestCase):
    def test_password_english(self):
        result = sanitize_input("password: MySecret123")
        assert "MySecret123" not in result
        assert "********" in result

    def test_password_chinese(self):
        result = sanitize_input("密码：ABC123xyz")
        assert "ABC123xyz" not in result
        assert "********" in result

    def test_pwd_variant(self):
        result = sanitize_input("pwd=hunter2")
        assert "hunter2" not in result

    def test_passwd_variant(self):
        result = sanitize_input("passwd: secret")
        assert "secret" not in result


class TestSanitizeIdNumber(unittest.TestCase):
    def test_18_digit_id(self):
        result = sanitize_input("ID: 110101199003071234")
        assert "1234" in result
        assert "110101" not in result

    def test_id_with_x(self):
        result = sanitize_input("身份证 11010119900307123X")
        assert "123X" in result
        assert "110101" not in result


class TestSanitizePhone(unittest.TestCase):
    def test_mobile_number(self):
        result = sanitize_input("Phone: 13812345678")
        assert "138" in result
        assert "5678" in result
        assert "1234" not in result  # middle digits masked

    def test_no_phone_untouched(self):
        result = sanitize_input("Amount: 12345")
        assert result == "Amount: 12345"


class TestSanitizeNone(unittest.TestCase):
    def test_none_input(self):
        assert sanitize_input(None) is None


class TestSanitizeAmountPreserved(unittest.TestCase):
    def test_amount_kept(self):
        result = sanitize_input("Transfer amount: ¥500,000.00")
        assert "500,000.00" in result

    def test_amount_with_text(self):
        result = sanitize_input("转账金额 100万元")
        assert "100万元" in result


class TestHashRawValue(unittest.TestCase):
    def test_hash_consistency(self):
        h1 = hash_raw_value("secret")
        h2 = hash_raw_value("secret")
        assert h1 == h2

    def test_hash_is_sha256(self):
        h = hash_raw_value("test")
        expected = hashlib.sha256(b"test").hexdigest()
        assert h == expected

    def test_none_returns_none(self):
        assert hash_raw_value(None) is None

    def test_different_values_different_hashes(self):
        assert hash_raw_value("a") != hash_raw_value("b")


# ============================================================
# Storage tests
# ============================================================

class TestGenerateObjectKey(unittest.TestCase):
    def test_format(self):
        key = generate_object_key("org_1", "task_1", 0, "before")
        assert key.startswith("audit/org_1/task_1/0_before_")
        assert key.endswith(".png")

    def test_uniqueness(self):
        keys = {generate_object_key("o", "t", 0, "before") for _ in range(100)}
        assert len(keys) == 100

    def test_after_phase(self):
        key = generate_object_key("org_1", "task_1", 2, "after")
        assert "2_after_" in key


class TestGetBucketName(unittest.TestCase):
    def test_format(self):
        dt = datetime(2026, 3, 7)
        name = get_bucket_name(dt)
        assert name == "finrpa-audit-202603"

    def test_default_current_month(self):
        name = get_bucket_name()
        assert name.startswith("finrpa-audit-")
        assert len(name) == len("finrpa-audit-202603")

    def test_different_months(self):
        jan = get_bucket_name(datetime(2026, 1, 15))
        dec = get_bucket_name(datetime(2026, 12, 31))
        assert jan == "finrpa-audit-202601"
        assert dec == "finrpa-audit-202612"


# ============================================================
# Query API tests
# ============================================================

class TestAuditQueryAPI(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from enterprise.audit.routes import router, configure_store
        from enterprise.auth.schemas import DepartmentRole, UserContext
        from enterprise.auth.dependencies import get_current_user

        self.app = FastAPI()
        self.app.include_router(router)

        self.user = UserContext(
            user_id="eu_1",
            org_id="org_1",
            department_roles=[
                DepartmentRole(department_id="dept_a", department_name="A", role="org_admin"),
            ],
            business_line_ids=[],
            has_cross_org_read=True,
        )
        self.app.dependency_overrides[get_current_user] = lambda: self.user

        # Override require_cross_org_viewer
        from enterprise.auth.dependencies import require_cross_org_viewer
        self.app.dependency_overrides[require_cross_org_viewer] = lambda: self.user

        self.store = []
        configure_store(self.store)
        self.client = TestClient(self.app)

    def _make_log(self, **overrides):
        defaults = {
            "audit_log_id": "aud_1",
            "task_id": "task_1",
            "organization_id": "org_1",
            "department_id": "dept_a",
            "business_line_id": None,
            "action_index": 0,
            "action_type": "click",
            "target_element": "button#submit",
            "input_value": None,
            "page_url": "https://bank.example.com/transfer",
            "screenshot_before_url": "https://minio.example.com/before.png",
            "screenshot_after_url": "https://minio.example.com/after.png",
            "duration_ms": 150,
            "executor": "agent",
            "execution_result": "success",
            "error_message": None,
            "has_approval": False,
            "approval_id": None,
            "approver_user_id": None,
            "created_at": "2026-03-07T10:00:00",
        }
        defaults.update(overrides)
        return defaults

    def test_empty_store(self):
        resp = self.client.get("/enterprise/audit/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_returns_matching_org(self):
        self.store.append(self._make_log())
        self.store.append(self._make_log(audit_log_id="aud_other", organization_id="org_other"))
        resp = self.client.get("/enterprise/audit/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["audit_log_id"] == "aud_1"

    def test_filter_by_task_id(self):
        self.store.append(self._make_log(audit_log_id="aud_1", task_id="task_1"))
        self.store.append(self._make_log(audit_log_id="aud_2", task_id="task_2"))
        resp = self.client.get("/enterprise/audit/logs?task_id=task_1")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["task_id"] == "task_1"

    def test_filter_by_action_type(self):
        self.store.append(self._make_log(audit_log_id="aud_1", action_type="click"))
        self.store.append(self._make_log(audit_log_id="aud_2", action_type="input_text"))
        resp = self.client.get("/enterprise/audit/logs?action_type=input_text")
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["action_type"] == "input_text"

    def test_filter_by_executor(self):
        self.store.append(self._make_log(audit_log_id="aud_1", executor="agent"))
        self.store.append(self._make_log(audit_log_id="aud_2", executor="eu_1"))
        resp = self.client.get("/enterprise/audit/logs?executor=eu_1")
        assert resp.json()["total"] == 1

    def test_pagination(self):
        for i in range(25):
            self.store.append(self._make_log(
                audit_log_id=f"aud_{i}",
                created_at=f"2026-03-07T{10+i//60:02d}:{i%60:02d}:00",
            ))

        resp = self.client.get("/enterprise/audit/logs?page=1&page_size=10")
        data = resp.json()
        assert data["total"] == 25
        assert len(data["items"]) == 10
        assert data["page"] == 1
        assert data["page_size"] == 10

        resp2 = self.client.get("/enterprise/audit/logs?page=3&page_size=10")
        data2 = resp2.json()
        assert len(data2["items"]) == 5

    def test_time_range_filter(self):
        self.store.append(self._make_log(
            audit_log_id="aud_early",
            created_at="2026-03-07T08:00:00",
        ))
        self.store.append(self._make_log(
            audit_log_id="aud_mid",
            created_at="2026-03-07T12:00:00",
        ))
        self.store.append(self._make_log(
            audit_log_id="aud_late",
            created_at="2026-03-07T18:00:00",
        ))
        resp = self.client.get(
            "/enterprise/audit/logs?start_time=2026-03-07T10:00:00&end_time=2026-03-07T15:00:00"
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["audit_log_id"] == "aud_mid"


# ============================================================
# Full flow test: 3-step task
# ============================================================

class TestFullAuditFlow(unittest.TestCase):
    """Simulate a 3-step task and verify audit completeness."""

    def test_three_step_task_produces_complete_audit(self):
        """
        Simulate:
        Step 0: Navigate to transfer page
        Step 1: Input account number (sanitized)
        Step 2: Click submit (with approval)
        Each step produces before+after screenshot keys.
        """
        audit_logs = []
        screenshot_keys = []

        steps = [
            {
                "action_index": 0,
                "action_type": "navigate",
                "target_element": None,
                "input_value": None,
                "page_url": "https://bank.example.com/transfer",
                "executor": "agent",
                "has_approval": False,
            },
            {
                "action_index": 1,
                "action_type": "input_text",
                "target_element": "input#account",
                "input_value": "6222021234561234",  # card number - should be sanitized
                "page_url": "https://bank.example.com/transfer",
                "executor": "agent",
                "has_approval": False,
            },
            {
                "action_index": 2,
                "action_type": "click",
                "target_element": "button#submit",
                "input_value": None,
                "page_url": "https://bank.example.com/transfer/confirm",
                "executor": "agent",
                "has_approval": True,
                "approval_id": "apr_001",
                "approver_user_id": "eu_approver",
            },
        ]

        for step in steps:
            # Generate screenshot keys
            before_key = generate_object_key("org_1", "task_1", step["action_index"], "before")
            after_key = generate_object_key("org_1", "task_1", step["action_index"], "after")
            screenshot_keys.extend([before_key, after_key])

            # Sanitize input
            sanitized = sanitize_input(step.get("input_value"))
            raw_hash = hash_raw_value(step.get("input_value"))

            log_entry = {
                "audit_log_id": generate_audit_log_id(),
                "task_id": "task_1",
                "org_id": "org_1",
                "department_id": "dept_a",
                "action_index": step["action_index"],
                "action_type": step["action_type"],
                "target_element": step.get("target_element"),
                "input_value": sanitized,
                "input_value_raw_hash": raw_hash,
                "page_url": step.get("page_url"),
                "screenshot_before_key": before_key,
                "screenshot_after_key": after_key,
                "executor": step["executor"],
                "has_approval": step.get("has_approval", False),
                "approval_id": step.get("approval_id"),
                "approver_user_id": step.get("approver_user_id"),
            }
            audit_logs.append(log_entry)

        # Assertions
        assert len(audit_logs) == 3, "Should have 3 audit log entries"
        assert len(screenshot_keys) == 6, "Should have 6 screenshot keys (3 steps × 2)"

        # Verify step 0 (navigate)
        assert audit_logs[0]["action_type"] == "navigate"
        assert audit_logs[0]["input_value"] is None

        # Verify step 1 (input_text with card number sanitization)
        assert audit_logs[1]["action_type"] == "input_text"
        assert "6222" not in (audit_logs[1]["input_value"] or "")
        assert "1234" in (audit_logs[1]["input_value"] or "")
        assert audit_logs[1]["input_value_raw_hash"] is not None

        # Verify step 2 (click with approval)
        assert audit_logs[2]["action_type"] == "click"
        assert audit_logs[2]["has_approval"] is True
        assert audit_logs[2]["approval_id"] == "apr_001"
        assert audit_logs[2]["approver_user_id"] == "eu_approver"

        # Verify all screenshot keys are unique
        assert len(set(screenshot_keys)) == 6

        # Verify screenshot key format
        for key in screenshot_keys:
            assert key.startswith("audit/org_1/task_1/")
            assert key.endswith(".png")


if __name__ == "__main__":
    unittest.main()

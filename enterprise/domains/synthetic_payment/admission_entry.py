"""Audit-only synthetic caller for the governed Task admission boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from enterprise.agent.interactions import (
    CapabilityInputValidator,
    CapabilityRequest,
    CapabilityRequestKind,
    EntryMode,
    build_grant_projection,
)
from enterprise.agent.work_orders import BusinessPlan, BusinessPlanStep, ExecutionWorkOrder, RecoveryLevel
from enterprise.governance.admission import (
    GovernedTaskAdmissionService,
    GovernedTaskDraft,
    TaskAdmissionBundle,
    TaskAdmissionReceipt,
)
from enterprise.governance.capabilities import (
    CapabilityDataScope,
    CapabilityDefinition,
    CapabilityGrantSet,
    CapabilityRegistry,
    CapabilityResolutionContext,
    CapabilityResolver,
)
from enterprise.governance.contracts import GovernanceMode, TaskContract
from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot

from .accounts import require_synthetic_account
from .constants import (
    BUSINESS_LINE_ID,
    CAPABILITY_ID,
    PACK_ID,
    PACK_VERSION,
    PAYMENTS_DEPARTMENT_ID,
    POLICY_VERSION,
    RESULT_PROBE_REF,
    TENANT_ID,
)
from .definition import build_manifest
from .models import PaymentFacts

SYNTHETIC_ADMISSION_CALLER_ID = "synthetic_payment_admission_api"
SYNTHETIC_WORKLOAD_PRINCIPAL_ID = "synthetic_payment_admission_service"


class _PaymentFactsValidator(CapabilityInputValidator):
    def validate(self, definition: CapabilityDefinition, value: dict[str, object]) -> None:
        if definition.capability_id != CAPABILITY_ID:
            raise ValueError("Synthetic admission received an unsupported capability")
        PaymentFacts.model_validate(value)


class SyntheticPaymentTaskAdmissionEntry:
    """Build and persist a non-runnable synthetic Task admission aggregate."""

    def __init__(
        self,
        service: GovernedTaskAdmissionService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._registry = CapabilityRegistry(build_manifest().capabilities)
        self._input_validator = _PaymentFactsValidator()

    async def admit(
        self,
        *,
        request_id: str,
        facts: PaymentFacts,
        requester_account: str = "operator",
    ) -> TaskAdmissionReceipt:
        return await self._service.admit(
            **self._prepare_inputs(
                request_id=request_id,
                facts=facts,
                requester_account=requester_account,
            )
        )

    def prepare_bundle(
        self,
        *,
        request_id: str,
        facts: PaymentFacts,
        requester_account: str = "operator",
    ) -> TaskAdmissionBundle:
        """Expose the deterministic bundle for offline inspection and tests."""

        return self._service.prepare(
            **self._prepare_inputs(
                request_id=request_id,
                facts=facts,
                requester_account=requester_account,
            )
        )

    def _prepare_inputs(
        self,
        *,
        request_id: str,
        facts: PaymentFacts,
        requester_account: str,
    ) -> dict[str, object]:
        if not request_id:
            raise ValueError("Synthetic Task admission requires request_id")
        now = self._clock()
        requester = require_synthetic_account(requester_account)
        scope = CapabilityDataScope(
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            resource_ids={facts.payment_id},
        )
        resolved_grants = CapabilityResolver(self._registry).resolve(
            CapabilityResolutionContext(
                user=requester,
                tenant_id=TENANT_ID,
                data_scope=scope,
                installed_capability_ids={CAPABILITY_ID},
                policy_snapshot_version=POLICY_VERSION,
                resolved_at=now,
                workload_principal_id=SYNTHETIC_WORKLOAD_PRINCIPAL_ID,
                revocation_epoch="synthetic-epoch-1",
                purpose="synthetic_task_admission",
            )
        )
        resolved_grant = resolved_grants.grants[0]
        grant = resolved_grant.model_copy(
            update={"grant_id": _stable_id("grant", request_id)},
            deep=True,
        )
        grants = CapabilityGrantSet(grants=[grant])
        projection = build_grant_projection(grants=grants, registry=self._registry, now=now)

        task_id = _stable_id("task", request_id)
        contract_id = _stable_id("contract", request_id)
        plan_id = _stable_id("plan", request_id)
        step_id = _stable_id("plan_step", request_id)
        request = CapabilityRequest(
            request_id=request_id,
            submitted_at=now,
            entry_mode=EntryMode.UI,
            principal_ref=requester.user_id,
            session_ref=f"synthetic-session:{requester.user_id}",
            tenant_id=TENANT_ID,
            requested_scope=scope,
            capability_ref=CAPABILITY_ID,
            capability_version=PACK_VERSION,
            request_kind=CapabilityRequestKind.TRANSITION,
            typed_inputs=facts.model_dump(mode="json"),
            resource_refs={facts.payment_id},
            user_intent_summary="Admit one synthetic payment task for audit inspection",
            grant_ref=grant.grant_id,
            contract_versions={
                "pack": PACK_VERSION,
                "policy": POLICY_VERSION,
                "task_contract": "1",
            },
        )
        task = GovernedTaskDraft(
            task_id=task_id,
            organization_id=TENANT_ID,
            goal="Audit one synthetic payment submission request",
            mode=GovernanceMode.AUDIT,
        )
        creation_snapshot = TrustedTaskCreationSnapshot(
            task_id=task_id,
            organization_id=TENANT_ID,
            creation_path=TaskCreationPath.SDK_API,
            initiator_id=requester.user_id,
            service_principal_id=SYNTHETIC_WORKLOAD_PRINCIPAL_ID,
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            authorization_snapshot={
                "grant_id": grant.grant_id,
                "revocation_epoch": grant.revocation_epoch,
                "policy_snapshot_version": grant.policy_snapshot_version,
            },
            policy_version=POLICY_VERSION,
            contract_version=1,
            created_at=now,
            request_id=request_id,
            caller_id=SYNTHETIC_ADMISSION_CALLER_ID,
        )
        contract = TaskContract(
            contract_id=contract_id,
            task_id=task_id,
            organization_id=TENANT_ID,
            initiator_id=requester.user_id,
            service_principal_id=SYNTHETIC_WORKLOAD_PRINCIPAL_ID,
            department_id=PAYMENTS_DEPARTMENT_ID,
            business_line_id=BUSINESS_LINE_ID,
            goal=task.goal,
            allowed_operations={CAPABILITY_ID},
            data_scope=scope.model_dump(mode="json"),
            authorization_snapshot=creation_snapshot.model_dump(mode="json"),
            policy_profile=PACK_ID,
            policy_version=POLICY_VERSION,
            success_criteria=["Admission evidence is durably recorded without publishing a runnable Task"],
            expires_at=grant.expires_at,
            version=1,
            mode=GovernanceMode.AUDIT,
        )
        step = BusinessPlanStep(
            step_id=step_id,
            capability_id=CAPABILITY_ID,
            capability_version=PACK_VERSION,
            grant_id=grant.grant_id,
            contract_id=contract_id,
            inputs=request.typed_inputs,
            expected_transition={"from": "draft", "to": "submitted"},
            success_criteria=["No browser or business transition is executed by admission"],
        )
        plan = BusinessPlan(
            plan_id=plan_id,
            request_id=request_id,
            task_id=task_id,
            contract_id=contract_id,
            data_scope=scope,
            steps=[step],
        )
        work_order = ExecutionWorkOrder(
            work_order_id=_stable_id("work_order", request_id),
            business_plan_step_id=step_id,
            task_id=task_id,
            contract_id=contract_id,
            grant_id=grant.grant_id,
            navigation_goal="Inspect the synthetic payment page without executing its transition",
            allowed_operations={"read", "observe"},
            prohibited_operations={"click", "input", "submit", "javascript", "coordinate"},
            success_criteria=["Collect audit evidence only"],
            required_evidence=["synthetic semantic page marker"],
            max_recovery_level=RecoveryLevel.L0,
            result_probe_ref=RESULT_PROBE_REF,
        )
        return {
            "task": task,
            "creation_snapshot": creation_snapshot,
            "contract": contract,
            "request": request,
            "projection": projection,
            "grants": grants,
            "registry": self._registry,
            "plan": plan,
            "work_orders": (work_order,),
            "now": now,
            "input_validator": self._input_validator,
        }


def _stable_id(kind: str, request_id: str) -> str:
    key = json.dumps([TENANT_ID, request_id, kind], ensure_ascii=True, separators=(",", ":"))
    return f"synthetic_{kind}_{uuid5(NAMESPACE_URL, key).hex}"

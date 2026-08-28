"""Fixed test identities for the Stripe test-mode sandbox.

These accounts are never valid outside the sandbox; an adopting tenant must
replace them with its own identity authority (see PACK.md P3).
"""

from enterprise.auth.schemas import DepartmentRole, UserContext

from .constants import BUSINESS_LINE_ID, COMPLIANCE_DEPARTMENT_ID, PAYMENTS_DEPARTMENT_ID, TENANT_ID


def _account(user_id: str, department_id: str, role: str) -> UserContext:
    return UserContext(
        user_id=user_id,
        org_id=TENANT_ID,
        department_roles=[
            DepartmentRole(
                department_id=department_id,
                department_name=department_id,
                role=role,
            )
        ],
        business_line_ids=[BUSINESS_LINE_ID],
    )


STRIPE_ACCOUNTS = {
    "operator": _account("stripe_operator", PAYMENTS_DEPARTMENT_ID, "operator"),
    "approver": _account("stripe_approver", PAYMENTS_DEPARTMENT_ID, "approver"),
    "compliance": _account("stripe_compliance", COMPLIANCE_DEPARTMENT_ID, "approver"),
    "viewer": _account("stripe_viewer", PAYMENTS_DEPARTMENT_ID, "viewer"),
}


def require_stripe_account(account_name: str) -> UserContext:
    try:
        return STRIPE_ACCOUNTS[account_name].model_copy(deep=True)
    except KeyError as exc:
        raise ValueError(f"Unknown Stripe sandbox account: {account_name}") from exc

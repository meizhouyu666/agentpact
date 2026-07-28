"""Lightweight guards shared by execution paths that cannot consume permits yet."""

from enterprise.governance.execution_profiles import ExecutionProfile
from skyvern.config import settings


class GovernedScriptExecutionDisabled(RuntimeError):
    """Raised when a script path would bypass the ActionHandler permit boundary."""


def assert_script_execution_is_not_governed() -> None:
    """Disable direct script browser operations once enforce mode is introduced."""

    if settings.GOVERNANCE_MODE == "enforce":
        raise GovernedScriptExecutionDisabled(
            "Direct script browser actions are disabled in governance enforce mode"
        )


class MissingExecutionAuthorization(PermissionError):
    """Raised when an enforce-mode action reaches the browser ungoverned."""


def assert_execution_authorization_present(authorization: object | None) -> None:
    """Fail closed at the public ActionHandler entry in future enforce mode."""

    if settings.GOVERNANCE_MODE == "enforce" and authorization is None:
        raise MissingExecutionAuthorization(
            "ActionHandler requires ExecutionAuthorization in governance enforce mode"
        )


class MissingExecutionProfile(PermissionError):
    """Raised when only part of the governed handler context is supplied."""


def has_complete_governed_execution_context(
    authorization: object | None,
    profile: ExecutionProfile | None,
) -> bool:
    """Require authorization and profile together, while preserving off/audit."""

    if authorization is None:
        assert_execution_authorization_present(authorization)
        if profile is not None:
            raise MissingExecutionAuthorization(
                "ActionHandler cannot accept ExecutionProfile without ExecutionAuthorization"
            )
        return False
    if profile is None:
        raise MissingExecutionProfile(
            "ActionHandler requires ExecutionProfile with ExecutionAuthorization"
        )
    return True


def precomputed_action_reuse_is_allowed() -> bool:
    """Permit cached/speculative state only outside future enforce mode."""

    return settings.GOVERNANCE_MODE != "enforce"

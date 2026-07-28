"""Browser-backed, audit-only evidence collection.

This module is deliberately separated from Skyvern's execution handlers.  It
opens a page, captures a short-lived DOM/screenshot snapshot, and emits only a
redacted manifest.  No ActionHandler, Permit, or business-system call is
reachable from this collector.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .classification import hmac_fingerprint
from .contracts import PageReadiness, PolicyDecision
from .governor import build_governance_batch_plan


class BrowserSemanticFieldRef(BaseModel):
    """HMAC-bound reference to a semantic field without its value or name."""

    field_ref: str
    element_ref: str
    tag_name: str
    role: str | None = None
    value_present: bool = False


class BrowserSemanticActionRef(BaseModel):
    """HMAC-bound reference to an action affordance without DOM identifiers."""

    element_ref: str
    semantic_action: str
    action_type: Literal["click"] = "click"
    enabled: bool = True


class BrowserAuditEvidenceManifest(BaseModel):
    """Persistable audit evidence; raw browser artifacts are intentionally absent."""

    schema_version: Literal["phase2-browser-audit-v1"] = "phase2-browser-audit-v1"
    scenario_id: str
    task_id: str
    step_id: str
    page_url_fingerprint: str
    observation_hash: str
    readiness: PageReadiness
    readiness_confidence: float = Field(ge=0.0, le=1.0)
    network_idle_reached: bool = True
    dom_field_refs: list[BrowserSemanticFieldRef] = Field(default_factory=list)
    action_candidates: list[BrowserSemanticActionRef] = Field(default_factory=list)
    action_fingerprints: list[str] = Field(default_factory=list)
    screenshot_fingerprint: str
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    redaction_summary: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime


_ACTION_LABELS = {
    "create_challenge": "create challenge",
    "approve_payment": "approve payment",
    "execute_payment": "execute payment",
    "probe_payment_result": "probe result",
    "clear_probe_fault": "clear probe fault",
}
_SYNTHETIC_PAGE_MARKER = "synthetic-payment-console"
_SYNTHETIC_DOMAIN_PACK_MARKER = "synthetic.payment"


async def collect_browser_audit_evidence(
    *,
    page_url: str,
    scenario_id: str,
    task_id: str,
    step_id: str,
    hmac_secret: str | bytes,
    timeout_ms: int = 15_000,
    executable_path: str | None = None,
) -> BrowserAuditEvidenceManifest:
    """Open a browser page and return a redacted audit manifest.

    The browser and its bytes are closed/discarded before the manifest is
    returned.  Callers must not turn this function into an execution hook.
    """

    if not hmac_secret:
        raise ValueError("Browser audit collection requires a non-empty HMAC secret")
    _validate_synthetic_url(page_url)

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable_path)
        try:
            page = await browser.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            network_idle_reached = True
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                # A page that never reaches network-idle is evidence of an
                # unsettled observation, not a reason to execute anything.
                network_idle_reached = False
            return await collect_browser_page_audit(
                page=page,
                page_url=page_url,
                scenario_id=scenario_id,
                task_id=task_id,
                step_id=step_id,
                hmac_secret=hmac_secret,
                network_idle_reached=network_idle_reached,
            )
        finally:
            await browser.close()


async def collect_browser_page_audit(
    *,
    page: Any,
    page_url: str,
    scenario_id: str,
    task_id: str,
    step_id: str,
    hmac_secret: str | bytes,
    network_idle_reached: bool = True,
) -> BrowserAuditEvidenceManifest:
    """Collect from a Playwright-compatible page object.

    Keeping this small seam explicit makes the semantic contract unit-testable
    with a fake page while the end-to-end test uses a real Chromium page.
    """

    final_url = str(getattr(page, "url", page_url))
    _validate_synthetic_url(page_url)
    _validate_synthetic_url(final_url)
    if _canonical_url(page_url) != _canonical_url(final_url):
        raise ValueError("Synthetic browser audit rejected a redirected page")
    html = await page.content()
    screenshot = await page.screenshot(type="png")
    snapshot = await page.evaluate(_DOM_SNAPSHOT_SCRIPT)
    page_title = await page.title()
    return build_browser_audit_manifest(
        page_url=final_url,
        scenario_id=scenario_id,
        task_id=task_id,
        step_id=step_id,
        html=html,
        screenshot=screenshot,
        dom_snapshot=snapshot,
        page_title=page_title,
        hmac_secret=hmac_secret,
        network_idle_reached=network_idle_reached,
    )


def build_browser_audit_manifest(
    *,
    page_url: str,
    scenario_id: str,
    task_id: str,
    step_id: str,
    html: str,
    screenshot: bytes,
    dom_snapshot: dict[str, Any],
    hmac_secret: str | bytes,
    page_title: str | None = None,
    network_idle_reached: bool = True,
) -> BrowserAuditEvidenceManifest:
    """Build a manifest from already-captured browser artifacts.

    ``html`` and ``screenshot`` are inputs only.  They are hashed or reduced to
    stable references and never copied into the returned model.
    """

    _validate_synthetic_url(page_url)
    _require_trusted_page_marker(dom_snapshot)
    readiness, readiness_confidence = _parse_readiness(
        dom_snapshot,
        network_idle_reached=network_idle_reached,
    )
    raw_fields = _raw_semantic_fields(dom_snapshot.get("fields"))
    raw_actions = _raw_semantic_actions(dom_snapshot.get("actions"))
    action_models = [
        _BrowserActionCandidate(
            element_id=action["element_id"],
            semantic_action=action["semantic_action"],
            description=_ACTION_LABELS.get(action["semantic_action"], action["semantic_action"].replace("_", " ")),
        )
        for action in raw_actions
    ]
    element_lookup = {
        action.element_id: {
            "text": action.description,
            "attributes": {
                "aria-label": action.description,
                "data-governance-action": action.semantic_action,
            },
        }
        for action in action_models
    }
    plan = build_governance_batch_plan(
        task_id=task_id,
        step_id=step_id,
        actions=action_models,
        page_url=page_url,
        page_html=html,
        element_lookup=element_lookup,
        hmac_secret=hmac_secret,
        readiness=readiness,
        readiness_confidence=readiness_confidence,
    )
    screenshot_fingerprint = hmac_fingerprint(screenshot, hmac_secret)
    manifest = BrowserAuditEvidenceManifest(
        scenario_id=scenario_id,
        task_id=task_id,
        step_id=step_id,
        page_url_fingerprint=hmac_fingerprint(page_url, hmac_secret),
        observation_hash=plan.observation.snapshot_hash,
        readiness=readiness,
        readiness_confidence=readiness_confidence,
        network_idle_reached=network_idle_reached,
        dom_field_refs=_semantic_fields(raw_fields, hmac_secret),
        action_candidates=_semantic_actions(raw_actions, hmac_secret),
        action_fingerprints=[candidate.intent.action_fingerprint for candidate in plan.candidates],
        screenshot_fingerprint=screenshot_fingerprint,
        policy_decisions=[candidate.decision for candidate in plan.candidates],
        redaction_summary={
            "raw_html_persisted": False,
            "raw_screenshot_persisted": False,
            "raw_form_values_persisted": False,
            "page_url_persisted": False,
            "semantic_names_persisted": False,
            "trusted_page_marker_verified": True,
            "network_idle_reached": network_idle_reached,
            "page_title_observed": bool(page_title),
            "semantic_field_count": len(raw_fields),
            "semantic_action_count": len(raw_actions),
        },
        captured_at=datetime.now(timezone.utc),
    )
    return manifest


class _BrowserActionCandidate(BaseModel):
    action_type: Literal["click"] = "click"
    element_id: str
    semantic_action: str
    description: str


def _parse_readiness(
    snapshot: dict[str, Any],
    *,
    network_idle_reached: bool,
) -> tuple[PageReadiness, float]:
    marker = str(snapshot.get("readiness") or "").lower()
    if snapshot.get("aria_busy"):
        return PageReadiness.LOADING, 0.2 if not network_idle_reached else 0.95
    if not network_idle_reached:
        return PageReadiness.TRANSITIONING, 0.35
    try:
        readiness = PageReadiness(marker)
    except ValueError:
        return PageReadiness.UNKNOWN, 0.25
    return readiness, 0.95 if readiness is not PageReadiness.UNKNOWN else 0.25


def _raw_semantic_fields(raw_fields: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for raw in raw_fields or []:
        if not isinstance(raw, dict) or not raw.get("field_name") or not raw.get("element_id"):
            continue
        fields.append(
            {
                "field_name": str(raw["field_name"]),
                "element_id": str(raw["element_id"]),
                "tag_name": str(raw.get("tag_name") or "unknown"),
                "role": str(raw["role"]) if raw.get("role") else None,
                "value_present": bool(raw.get("value_present")),
            }
        )
    return sorted(fields, key=lambda field: (field["field_name"], field["element_id"]))


def _semantic_fields(raw_fields: list[dict[str, Any]], secret: str | bytes) -> list[BrowserSemanticFieldRef]:
    return [
        BrowserSemanticFieldRef(
            field_ref=_semantic_fingerprint("field-name", raw["field_name"], secret),
            element_ref=_semantic_fingerprint("field-element", raw["element_id"], secret),
            tag_name=raw["tag_name"],
            role=raw["role"],
            value_present=raw["value_present"],
        )
        for raw in raw_fields
    ]


def _raw_semantic_actions(raw_actions: Any) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for raw in raw_actions or []:
        if not isinstance(raw, dict) or not raw.get("semantic_action") or not raw.get("element_id"):
            continue
        if str(raw["semantic_action"]) not in _ACTION_LABELS:
            continue
        actions.append(
            {
                "semantic_action": str(raw["semantic_action"]),
                "element_id": str(raw["element_id"]),
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    return sorted(actions, key=lambda action: (action["semantic_action"], action["element_id"]))


def _semantic_actions(raw_actions: list[dict[str, Any]], secret: str | bytes) -> list[BrowserSemanticActionRef]:
    return [
        BrowserSemanticActionRef(
            element_ref=_semantic_fingerprint("action-element", raw["element_id"], secret),
            semantic_action=raw["semantic_action"],
            enabled=raw["enabled"],
        )
        for raw in raw_actions
    ]


def _semantic_fingerprint(kind: str, value: str, secret: str | bytes) -> str:
    return hmac_fingerprint(f"phase2-browser-{kind}-v1:{value}", secret)


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}{parsed.path or '/'}"


def _validate_synthetic_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.path or "/") != "/"
    ):
        raise ValueError("Browser audit only accepts the isolated synthetic localhost console")


def _require_trusted_page_marker(snapshot: dict[str, Any]) -> None:
    if (
        snapshot.get("page_marker") != _SYNTHETIC_PAGE_MARKER
        or snapshot.get("domain_pack") != _SYNTHETIC_DOMAIN_PACK_MARKER
    ):
        raise ValueError("Browser audit target is missing the trusted synthetic page marker")


_DOM_SNAPSHOT_SCRIPT = """
() => {
  const stableId = (el, fallback) =>
    el.getAttribute('data-testid') || el.id || el.getAttribute('name') || fallback;
  const fields = Array.from(document.querySelectorAll('[data-governance-field]')).map((el, index) => ({
    field_name: el.getAttribute('data-governance-field'),
    element_id: stableId(el, `field-${index}`),
    tag_name: el.tagName.toLowerCase(),
    role: el.getAttribute('role'),
    value_present: Boolean(('value' in el && el.value) || el.textContent?.trim()),
  }));
  const actions = Array.from(document.querySelectorAll('[data-governance-action]')).map((el, index) => ({
    semantic_action: el.getAttribute('data-governance-action'),
    element_id: stableId(el, `action-${index}`),
    enabled: !el.hasAttribute('disabled') && el.getAttribute('aria-disabled') !== 'true',
  }));
  const root = document.querySelector('[data-governance-page]') || document.documentElement;
  return {
    page_marker: root.getAttribute('data-governance-page'),
    domain_pack: root.getAttribute('data-governance-domain-pack'),
    readiness: root.getAttribute('data-governance-readiness') || root.getAttribute('data-governance-state'),
    aria_busy: root.getAttribute('aria-busy') === 'true',
    fields,
    actions,
  };
}
"""

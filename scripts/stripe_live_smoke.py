"""Explicit manual smoke for Stripe test-mode API and hosted Checkout.

This script is informational and manual ONLY: it contacts the real Stripe API
with test-mode credentials and never runs as part of CI or the release gate.

Usage (Windows PowerShell, from the repository root):

    $env:STRIPE_SECRET_KEY = "sk_test_..."
    & .venv\\Scripts\\python.exe scripts\\stripe_live_smoke.py --create
    & .venv\\Scripts\\python.exe scripts\\stripe_live_smoke.py --payment-intent-id pi_xxx
    & .venv\\Scripts\\python.exe scripts\\stripe_live_smoke.py --hosted-checkout

Safety rules (enforced):
- ``STRIPE_SECRET_KEY`` must be set and must start with ``sk_test_``; a live
  key is rejected immediately. The key is never printed or logged.
- Without ``--create`` or ``--hosted-checkout`` the script performs no network
  operation. ``--hosted-checkout`` is the only flag that opens a real Stripe
  hosted Checkout page; it uses the 4242 test card and then performs an
  independent PaymentIntent GET probe. ``--create`` remains an API-only probe
  smoke and creates/cancels test objects.

Exit code 0 on success, 2 when preconditions are unsafe, 3 on probe failure.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.domains.stripe_payment.result_probe import (
    STRIPE_API_BASE,
    StripeApiResultProbe,
)
from enterprise.domains.stripe_payment.live_browser import (
    StripeHostedCheckoutError,
    StripeHostedCheckoutFlow,
    derive_live_idempotency_key,
    stripe_test_key_from_environment,
)
from enterprise.domains.stripe_payment.models import StripePaymentFacts


def _secret_key() -> str:
    try:
        return stripe_test_key_from_environment()
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(2)


def _probe_one(probe: StripeApiResultProbe, payment_intent_id: str, idempotency_key: str) -> None:
    evidence = probe.probe(resource_id=payment_intent_id, idempotency_key=idempotency_key)
    print(json_round_trip({
        "status": evidence.status.value,
        "reason_codes": [evidence.metadata.get("reason_code")],
        "reasons": evidence.reasons,
        "stripe_status": evidence.metadata.get("stripe_status"),
    }))


def json_round_trip(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _create_payment_intent(key: str, amount_minor: int, currency: str, idempotency_key: str, *, confirmed: bool) -> str:
    data: dict[str, str] = {
        "amount": str(amount_minor),
        "currency": currency,
        "payment_method_types[]": "card",
        "confirm": "true" if confirmed else "false",
        "description": "AgentPact stripe.payment live smoke",
    }
    if confirmed:
        # pm_card_visa is Stripe's reusable test-mode card for immediate confirmation.
        data["payment_method"] = "pm_card_visa"
    response = httpx.post(
        f"{STRIPE_API_BASE}/payment_intents",
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": idempotency_key},
        data=data,
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    payment_intent_id = str(payload["id"])
    print(f"created test PaymentIntent (status={payload['status']})")
    return payment_intent_id


def _cancel_payment_intent(key: str, payment_intent_id: str) -> str:
    response = httpx.post(
        f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/cancel",
        headers={"Authorization": f"Bearer {key}"},
        timeout=15.0,
    )
    response.raise_for_status()
    return str(response.json()["status"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payment-intent-id", help="existing test PaymentIntent id to probe (read-only)")
    parser.add_argument("--create", action="store_true", help="create + cancel one test PaymentIntent to exercise mapping")
    parser.add_argument("--hosted-checkout", action="store_true", help="explicitly create and complete a real Stripe hosted test Checkout")
    parser.add_argument("--evidence-dir", help="optional directory for redacted hosted Checkout evidence")
    parser.add_argument("--amount-minor", type=int, default=5000, help="amount in minor units when --create")
    parser.add_argument("--currency", default="usd")
    args = parser.parse_args()

    if not args.create and not args.payment_intent_id and not args.hosted_checkout:
        print("nothing to probe: pass --payment-intent-id, --create, or --hosted-checkout", file=sys.stderr)
        return 2

    key = _secret_key()
    probe = StripeApiResultProbe(secret_key=key)
    print("probing stripe.payment authoritative channel (test mode only)")

    try:
        if args.hosted_checkout:
            facts = StripePaymentFacts(
                payment_intent_id=f"pi_live_smoke_{os.getpid()}",
                amount_minor=args.amount_minor,
                currency=args.currency,
                description="AgentPact Stripe hosted Checkout smoke",
            )
            idempotency_key = derive_live_idempotency_key(
                request_id=f"stripe-live-smoke-{os.getpid()}",
                payment_intent_id=facts.payment_intent_id,
            )
            result = asyncio.run(
                StripeHostedCheckoutFlow(evidence_dir=args.evidence_dir).execute(
                    facts=facts,
                    idempotency_key=idempotency_key,
                )
            )
            print(json_round_trip({
                "browser_state": result.browser_state,
                "browser_stage": result.evidence.browser_stage,
                "browser_reason_code": result.evidence.browser_reason_code,
                "browser_final_url_summary": result.evidence.browser_final_url_summary,
                "browser_error_type": result.evidence.browser_error_type,
                "session_status": result.session.status,
                "payment_status": result.session.payment_status,
                "payment_intent_present": result.session.payment_intent_id is not None,
                "probe_status": result.probe.status.value,
                "probe_reason_code": result.evidence.probe_reason_code,
                "probe_reasons": result.probe.reasons,
            }))
        elif args.create:
            # Phase 1: confirmed with the reusable test card -> probe must CONFIRM.
            confirmed_id = _create_payment_intent(
                key, args.amount_minor, args.currency, f"agentpact-live-smoke-confirmed-{os.getpid()}", confirmed=True
            )
            print("probe after confirmed create:")
            _probe_one(probe, confirmed_id, f"agentpact-live-smoke-confirmed-{os.getpid()}")
            # Phase 2: unconfirmed -> probe must be UNKNOWN; cancel -> NOT_CONFIRMED.
            pending_id = _create_payment_intent(
                key, args.amount_minor, args.currency, f"agentpact-live-smoke-pending-{os.getpid()}", confirmed=False
            )
            print("probe after unconfirmed create:")
            _probe_one(probe, pending_id, f"agentpact-live-smoke-pending-{os.getpid()}")
            status = _cancel_payment_intent(key, pending_id)
            print(f"canceled -> {status}; probe after cancel:")
            _probe_one(probe, pending_id, f"agentpact-live-smoke-pending-{os.getpid()}")
        elif args.payment_intent_id:
            _probe_one(probe, args.payment_intent_id, f"agentpact-live-smoke-read-{os.getpid()}")
    except StripeHostedCheckoutError as exc:
        diagnostic = exc.diagnostic
        print(json_round_trip({
            "failed": True,
            "error_type": type(exc).__name__,
            "stage": diagnostic.stage if diagnostic else "unknown",
            "reason_code": diagnostic.reason_code if diagnostic else "stripe_hosted_checkout_error",
            "final_url_summary": diagnostic.final_url_summary if diagnostic else None,
            "diagnostic_error_type": diagnostic.error_type if diagnostic else None,
            "browser_stage": diagnostic.browser_stage if diagnostic else None,
            "browser_reason_code": diagnostic.browser_reason_code if diagnostic else None,
            "browser_final_url_summary": diagnostic.browser_final_url_summary if diagnostic else None,
            "browser_error_type": diagnostic.browser_error_type if diagnostic else None,
            "session_status": diagnostic.session_status if diagnostic else None,
            "payment_status": diagnostic.payment_status if diagnostic else None,
            "payment_intent_present": diagnostic.payment_intent_present if diagnostic else None,
        }), file=sys.stderr)
        return 3
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(json_round_trip({"failed": True, "error_type": type(exc).__name__}), file=sys.stderr)
        return 3
    print("live smoke completed; no credentials were printed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

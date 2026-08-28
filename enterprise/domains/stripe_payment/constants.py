"""Pack identity for the Stripe Payment (test-mode) Domain Pack candidate.

``PACK_CONFORMANCE_MANIFEST_DIGEST`` pins the accepted offline SDK contract.
It must equal the digest of ``build_pack_sdk_manifest()``; regenerate it only
when the immutable contract intentionally changes (see ``PACK.md`` P0).
"""

PACK_ID = "stripe.payment"
PACK_VERSION = "0.1.0-draft.1"
PACK_DISPLAY_NAME = "Stripe Payment (Test Mode) Domain Pack"
READ_CAPABILITY_ID = "stripe.payment.read"
CAPABILITY_ID = "stripe.payment.submit"
PACK_CAPABILITY_IDS = (READ_CAPABILITY_ID, CAPABILITY_ID)
# Pinned after first build; verified by tests/unit/test_stripe_payment_pack_conformance.py
PACK_CONFORMANCE_MANIFEST_DIGEST = "7c6585a38c4bb6f617039ce1241b1dae9dda0aa7a1e72e66108f9e749c0e5366"
POLICY_VERSION = "stripe-payment-policy-v0.1.0-draft.1"
RESULT_PROBE_REF = "stripe.payment.submit.result-probe.v1"
WORK_ORDER_REF = "stripe.payment.submit.work-order.v1"
ACCESS_POLICY_REF = "stripe.payment.submit.access.v1"
RISK_POLICY_REF = "stripe.payment.submit.risk.v1"

# Authoritative read / probe evidence identity. The Stripe API (test mode) is
# the single authoritative source of truth for this pack; there is no local
# store, unlike the synthetic loopback console.
AUTHORITATIVE_READ_EVIDENCE_ID = "stripe.payment.authoritative-read.v1"
AUTHORITATIVE_SOURCE_REF = "stripe.api/v1"
RESULT_PROBE_SCHEMA_REF = "stripe.payment.result-probe/v1"

# Deployment wiring defaults, NOT part of the immutable Pack contract.
# An adopting tenant MUST replace these with its own identities at install
# time (see PACK.md P3). They are kept here only so deterministic unit tests
# and the recorded demo can run without a tenant database.
TENANT_ID = "stripe_sandbox_tenant"
PAYMENTS_DEPARTMENT_ID = "stripe_payments"
COMPLIANCE_DEPARTMENT_ID = "stripe_compliance"
BUSINESS_LINE_ID = "stripe_treasury"

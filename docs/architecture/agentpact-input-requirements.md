# AgentPact input-contract requirements

AgentPact separates semantic business inputs from platform and adapter details.
Business slots (for example, `beneficiary_id` or `amount`) are declared by a
Pack as `InputSlotSpec`. System fields (request IDs, permits, idempotency keys,
and runtime metadata) are not business slots and must not be inferred from page
observations. An adapter may add `AdapterRequirement` values for wiring or
environment prerequisites without changing the Pack's business vocabulary.

`FieldBinding` is the versioned translation from a semantic slot to an
adapter-owned field. Bindings carry an explicit target kind, source, and
sensitivity and are immutable (`v1` today). A binding may change when an
adapter version changes; the semantic slot name remains stable.

Adapters report input readiness through a structured reverse status map with
only `missing`, `invalid`, or `ready`. This status is diagnostic feedback. It
does not promote observed values into canonical request inputs: canonical
`values` are supplied separately and remain unchanged by status reflection.

Sensitive and secret slots must reject model-generated sources. They may be
populated only by explicitly allowed user, system, or adapter sources. A
`reject_model` source is available for adapters that need to represent a
deliberate refusal without exposing a value.

Input recovery is encoded as `pre_effect_only`. Once an external effect may
have started, a request cannot be recovered by replaying inputs; the owner must
use its result-probe/UNKNOWN flow instead. Permit and Attempt boundaries follow
the same rule: after either boundary, unresolved execution is `UNKNOWN`, never a
new canonical input request.

One Pack can expose multiple adapters. Each adapter owns its own
`FieldBinding`/`AdapterRequirement` set and is selected by an explicit adapter
identity; the platform contracts remain generic and import no Stripe or
Synthetic implementation.

Stripe's hosted Checkout adapter declares the semantic `payment_intent_id` and
`amount_minor` slots locally and maps them to adapter-owned `payment_intent`
and `amount` fields only at the adapter edge. Those hosted names are never
returned in the platform pause contract. The declaration is pre-effect only;
after Permit/Attempt execution, recovery follows the existing UNKNOWN and
authoritative probe path.

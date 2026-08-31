# AgentPact-Owned Browser Operation Loop

## Scope

This slice owns layer 3 only: execute one already-authorized browser task goal.
It does not interpret the original user query, derive capability grants, or
plan/orchestrate business work. Those inputs arrive through
`BrowserLoopRunContext` with the Agent Run, task, step, and optional immutable
Domain Pack binding already selected.

## Skyvern Slice Inspected

The smallest coherent reference loop at the migration baseline is spread
across these Skyvern modules:

| Phase | Skyvern reference | Relevant behavior |
| --- | --- | --- |
| Observe | `skyvern/webeye/scraper/scraper.py` and `scraped_page.py` | Capture interactable DOM, element-to-selector maps, page text, and screenshots. |
| Decide | `skyvern/forge/agent.py::ForgeAgent.agent_step` | Build a page/goal prompt, call a model, and parse typed actions. |
| Enforce/act | `skyvern/webeye/actions/handler.py::ActionHandler` | Dispatch typed actions and apply the existing governed handler checks. |
| Verify | `handle_complete_action` and completion checks in `ForgeAgent` | Treat a completion proposal as a claim that may require verification. |
| Retry/reobserve | `ForgeAgent.execute_step`, `agent_step`, and scrape retry handling | Bound steps/retries and acquire a new page snapshot after progress or failure. |
| Terminate/report | Skyvern Task/Step state and Forge persistence | Persist completed, failed, terminated, and retry-exhausted product states. |

AgentPact borrows the proven phase ordering, hybrid DOM/screenshot observation,
stable element references, explicit completion verification, and bounded retry
ideas. It does not reuse the Forge state machine, Task/Step persistence, model
handler registry, prompt service, routes, workflow shell, dashboard, cloud
services, or account infrastructure.

## New Ownership Boundary

`enterprise/browser_loop/` is owned by AgentPact:

- `contracts.py` defines observations, actions, decisions, authorization
  results, verification outcomes, redacted events, and terminal reports.
- `ports.py` defines injected browser runtime, model, policy, verifier, event
  sink, and deterministic Domain Pack action interfaces.
- `loop.py` owns the `observe -> decide -> enforce -> act -> reobserve ->
  verify` state machine, integrity bindings, retry budgets, approval pause, and
  terminal failure/unknown semantics.
- `runtime.py` provides a direct injected-Playwright adapter and an optional
  compatibility adapter around Skyvern's local scraper.
- `integrations.py` maps the existing Domain Pack `BusinessResultProbe`
  contract into authoritative loop verification.

The model receives only the `ModelInput` returned by the policy hook. There is
no built-in provider, permissive egress policy, persistence fallback, or demo
decision. Every action, including a deterministic Domain Pack proposal, must
receive a matching `ExecutionAuthorization` and policy-eligible
`ExecutionProfile` before the runtime is called.

The loop uses existing AgentPact governance contracts rather than duplicating
them:

- `ExecutionAuthorization` binds permit, action fingerprint, observation, one
  idempotency key, and authoritative effect;
- `ExecutionProfile` constrains the available browser mechanism;
- `ExecutionEffect` controls fail-closed replay and UNKNOWN behavior;
- `PackRuntimeBinding` matches a deterministic action provider to an immutable
  Pack version and capability.

`BrowserLoopReport` uses the existing Agent Run terminal vocabulary
(`SUCCEEDED`, `FAILED`, `UNKNOWN`, `AWAITING_APPROVAL`), and every event carries
the existing run/task/step correlation without raw DOM, screenshots, action
values, selectors, or model reasoning. The Agent Run owner supplies the durable
event sink and maps these results into its own transaction/journal boundary.

## Security and Failure Semantics

1. Observation identity is HMAC-bound to URL and page HTML. A separate raw
   snapshot digest is rechecked immediately inside the browser runtime.
2. A decision for another observation is rejected. Staleness causes fresh
   observation, decision, and authorization; a previous action is never reused.
3. Policy denial fails. Approval produces `AWAITING_APPROVAL` without a browser
   call. Policy/provider/event failures do not activate a fallback.
4. The runtime revalidates action, observation, profile, and page freshness at
   its public boundary.
5. A write with an uncertain execution or a verifier retry becomes `UNKNOWN`.
   It is never replayed by the generic retry loop; a Domain Pack result probe or
   owner-specific recovery path must resolve it.
6. A model `SUCCESS` is only a claim. The injected verifier must confirm it.

## Temporary Skyvern Dependency

`SkyvernScraperRuntimeAdapter` temporarily calls only these methods on an
injected local Skyvern `BrowserState`:

- `must_get_working_page()`;
- `scrape_website(...)`, returning the local `ScrapedPage` DOM, selector map,
  and screenshots.

Action execution is AgentPact-owned Playwright code even when this adapter is
used. The direct `PlaywrightPageRuntime` has no Skyvern dependency, so callers
can migrate immediately when they already own browser lifecycle/session setup.

The synthetic Agent Run now routes its `precheck` and `confirm` Domain Pack
steps through this loop with direct Playwright observation, deterministic Pack
decisions, independent verification, and durable redacted events. Browser
lifecycle still comes from the injected Skyvern browser manager, and the
state-changing `submit` step still uses the legacy `ActionHandler` because it
currently owns the proven Permit consumption and crash-safe Attempt boundary.
Removing Forge routes, workflows, Task/Step persistence, or `ActionHandler`
would therefore still be premature.

## Next Extraction Step

Move browser lifecycle/session ownership behind a new AgentPact runtime factory
and port the remaining Skyvern scraper behavior needed in production (iframe
enumeration, interactable-tree normalization, and split screenshots). Next,
add an AgentPact-owned persisted execution runtime that validates freshness,
consumes the Permit, commits the Attempt before the browser call, and exposes
the exact Attempt to result-probe recovery. Only then switch the synthetic
`submit` step away from `ActionHandler`. Once all callers use that path, remove
`SkyvernScraperRuntimeAdapter` and demonstrate that no AgentPact entrypoint
imports `skyvern.forge.agent` or `skyvern.webeye.actions.handler` before
deleting any legacy product shell.

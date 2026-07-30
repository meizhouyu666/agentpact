import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { GlassCard } from "@/components/enterprise/GlassCard";
import { authFetch } from "@/util/authFetch";

type RunState =
  | "PLANNING"
  | "AWAITING_APPROVAL"
  | "RUNNING"
  | "UNKNOWN"
  | "SUCCEEDED"
  | "REJECTED"
  | "CANCELLED"
  | "FAILED";

type LegalAction = "approve" | "reject" | "probe" | "cancel";

type PlanStep = {
  sequence: number;
  role: "precheck" | "submit" | "confirm";
  state: "pending" | "active" | "completed" | "blocked" | "terminal";
};

type AgentRun = {
  run_id: string;
  pack_id: string;
  pack_version: string;
  pack_display_name: string;
  provider_mode: "recorded" | "live";
  state: RunState;
  legal_actions: LegalAction[];
  plan: PlanStep[];
  completed_steps: number;
  total_steps: number;
  reason_code?: string | null;
};

type TimelineEvent = {
  sequence: number;
  stage: string;
  state: RunState;
  reason_code?: string | null;
};

const TERMINAL = new Set<RunState>([
  "SUCCEEDED",
  "REJECTED",
  "CANCELLED",
  "FAILED",
]);

function operationKey(action: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}`;
  return `console-${action}-${suffix}`;
}

async function redactedError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: { code?: string } };
    return payload.detail?.code ?? "REQUEST_FAILED";
  } catch {
    return "REQUEST_FAILED";
  }
}

export function AgentRunsPage() {
  const [requestId, setRequestId] = useState("");
  const [intent, setIntent] = useState("");
  const [businessInputs, setBusinessInputs] = useState("{}");
  const [run, setRun] = useState<AgentRun | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadRun = useCallback(async (runId: string) => {
    const [runResponse, eventsResponse] = await Promise.all([
      authFetch(`/api/v1/enterprise/agent-runs/${runId}`),
      authFetch(`/api/v1/enterprise/agent-runs/${runId}/events`),
    ]);
    if (!runResponse.ok || !eventsResponse.ok) {
      throw new Error(await redactedError(!runResponse.ok ? runResponse : eventsResponse));
    }
    const nextRun = (await runResponse.json()) as AgentRun;
    setRun(nextRun);
    setEvents((await eventsResponse.json()) as TimelineEvent[]);
    return nextRun;
  }, []);

  useEffect(() => {
    if (!run || TERMINAL.has(run.state)) return;
    const timer = window.setInterval(() => {
      void loadRun(run.run_id).catch((error: Error) => setErrorCode(error.message));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [loadRun, run]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setErrorCode(null);
    try {
      const parsed = JSON.parse(businessInputs) as unknown;
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error("BUSINESS_INPUTS_OBJECT_REQUIRED");
      }
      const response = await authFetch("/api/v1/enterprise/agent-runs/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, intent, business_inputs: parsed }),
      });
      if (!response.ok) throw new Error(await redactedError(response));
      const created = (await response.json()) as AgentRun;
      setBusinessInputs("{}");
      setIntent("");
      await loadRun(created.run_id);
    } catch (error) {
      setErrorCode(error instanceof Error ? error.message : "REQUEST_FAILED");
    } finally {
      setBusy(false);
    }
  }

  async function command(action: LegalAction) {
    if (!run || !run.legal_actions.includes(action)) return;
    setBusy(true);
    setErrorCode(null);
    try {
      const response = await authFetch(`/api/v1/enterprise/agent-runs/${run.run_id}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation_key: operationKey(action) }),
      });
      if (!response.ok) throw new Error(await redactedError(response));
      await loadRun(run.run_id);
    } catch (error) {
      setErrorCode(error instanceof Error ? error.message : "REQUEST_FAILED");
    } finally {
      setBusy(false);
    }
  }

  const legalActions = useMemo(() => new Set(run?.legal_actions ?? []), [run]);

  return (
    <div className="space-y-6 p-6">
      <header>
        <h1 className="text-xl font-bold text-slate-900">Agent Runs</h1>
        <p className="mt-1 text-sm text-slate-500">
          Submit a governed request and follow only the actions declared legal by the server.
        </p>
      </header>

      <GlassCard hoverable={false} padding="lg">
        <form className="grid gap-4" onSubmit={submit}>
          <label className="grid gap-1 text-sm font-medium">
            Request ID
            <input
              aria-label="Request ID"
              className="rounded border border-slate-300 px-3 py-2"
              required
              value={requestId}
              onChange={(event) => setRequestId(event.target.value)}
            />
          </label>
          <label className="grid gap-1 text-sm font-medium">
            Intent
            <textarea
              aria-label="Intent"
              className="rounded border border-slate-300 px-3 py-2"
              required
              rows={2}
              value={intent}
              onChange={(event) => setIntent(event.target.value)}
            />
          </label>
          <label className="grid gap-1 text-sm font-medium">
            Trusted business inputs (JSON)
            <textarea
              aria-label="Trusted business inputs"
              className="rounded border border-slate-300 px-3 py-2 font-mono text-xs"
              required
              rows={5}
              value={businessInputs}
              onChange={(event) => setBusinessInputs(event.target.value)}
            />
          </label>
          <button className="w-fit rounded bg-blue-700 px-4 py-2 text-sm font-semibold text-white" disabled={busy}>
            Create governed run
          </button>
        </form>
      </GlassCard>

      {errorCode && (
        <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {errorCode}
        </p>
      )}

      {run && (
        <>
          <GlassCard hoverable={false} padding="lg">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-wide text-slate-500">Current state</p>
                <h2 className="text-lg font-bold" data-testid="run-state">
                  {run.state}
                </h2>
                <p className="mt-1 text-sm text-slate-600">
                  <span className="font-medium">{run.pack_display_name}</span>
                  {` (${run.pack_id} v${run.pack_version})`}
                  <span className="ml-2 rounded border border-slate-300 px-2 py-0.5 text-xs uppercase">
                    {run.provider_mode}
                  </span>
                </p>
                {run.reason_code && <p className="text-sm text-slate-500">{run.reason_code}</p>}
              </div>
              <div className="flex flex-wrap gap-2">
                {(["approve", "reject", "probe", "cancel"] as LegalAction[]).map((action) => (
                  <button
                    key={action}
                    type="button"
                    disabled={busy || !legalActions.has(action)}
                    onClick={() => void command(action)}
                    className="rounded border border-slate-300 px-3 py-2 text-sm capitalize disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {action}
                  </button>
                ))}
              </div>
            </div>
            <ol className="mt-5 grid gap-2" aria-label="Governed plan">
              {run.plan.map((step) => (
                <li key={step.sequence} className="flex items-center justify-between rounded bg-slate-50 px-3 py-2 text-sm">
                  <span>
                    {step.sequence}. {step.role}
                  </span>
                  <span className="font-medium uppercase text-slate-500">{step.state}</span>
                </li>
              ))}
            </ol>
          </GlassCard>

          <GlassCard hoverable={false} padding="lg">
            <h2 className="font-bold">Timeline</h2>
            <ol className="mt-3 space-y-2" aria-label="Run timeline">
              {events.map((event) => (
                <li key={event.sequence} className="text-sm">
                  {event.sequence}. {event.stage} — {event.state}
                  {event.reason_code ? ` (${event.reason_code})` : ""}
                </li>
              ))}
            </ol>
            {TERMINAL.has(run.state) && (
              <a
                className="mt-4 inline-block text-sm font-semibold text-blue-700 underline"
                href={`/api/v1/enterprise/agent-runs/${run.run_id}/report`}
              >
                Download redacted report
              </a>
            )}
          </GlassCard>
        </>
      )}
    </div>
  );
}

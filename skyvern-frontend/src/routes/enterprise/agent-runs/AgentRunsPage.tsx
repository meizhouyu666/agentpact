import { DownloadIcon, ReloadIcon } from "@radix-ui/react-icons";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
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

type AgentRunSummary = Omit<AgentRun, "legal_actions" | "plan"> & {
  created_at: string;
  modified_at: string;
};

type AgentRunPage = {
  items: AgentRunSummary[];
  next_cursor?: string | null;
};

type TimelineEvent = {
  sequence: number;
  stage: string;
  state: RunState;
  reason_code?: string | null;
};

type DecisionTraceStage = {
  stage:
    | "provider"
    | "validation"
    | "compilation"
    | "admission"
    | "approval"
    | "execution"
    | "recovery";
  status:
    | "not_recorded"
    | "pending"
    | "active"
    | "completed"
    | "blocked"
    | "failed";
  reason_code?: string | null;
  timestamp?: string | null;
  duration_ms?: number | null;
  provider_calls?: number | null;
  repair_count?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
};

type DecisionTrace = {
  schema_version: "agentpact-agent-run-decision-trace/v1";
  run_id: string;
  non_authoritative: true;
  stages: DecisionTraceStage[];
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

function replaceRunQuery(runId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("run", runId);
  window.history.replaceState(
    null,
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

export function AgentRunsPage() {
  const [requestId, setRequestId] = useState("");
  const [intent, setIntent] = useState("");
  const [businessInputs, setBusinessInputs] = useState("{}");
  const [history, setHistory] = useState<AgentRunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [decisionTrace, setDecisionTrace] = useState<DecisionTrace | null>(
    null,
  );
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const selectRun = useCallback((runId: string) => {
    setSelectedRunId(runId);
    setRun(null);
    setEvents([]);
    setDecisionTrace(null);
    replaceRunQuery(runId);
  }, []);

  const loadRun = useCallback(async (runId: string) => {
    const [runResponse, eventsResponse, traceResponse] = await Promise.all([
      authFetch(`/api/v1/enterprise/agent-runs/${runId}`),
      authFetch(`/api/v1/enterprise/agent-runs/${runId}/events`),
      authFetch(`/api/v1/enterprise/agent-runs/${runId}/decision-trace`),
    ]);
    if (!runResponse.ok || !eventsResponse.ok || !traceResponse.ok) {
      const failed = !runResponse.ok
        ? runResponse
        : !eventsResponse.ok
          ? eventsResponse
          : traceResponse;
      throw new Error(await redactedError(failed));
    }
    const nextRun = (await runResponse.json()) as AgentRun;
    setRun(nextRun);
    setEvents((await eventsResponse.json()) as TimelineEvent[]);
    setDecisionTrace((await traceResponse.json()) as DecisionTrace);
    return nextRun;
  }, []);

  const loadHistory = useCallback(
    async (preferredRunId?: string) => {
      const response = await authFetch(
        "/api/v1/enterprise/agent-runs/?limit=20",
      );
      if (!response.ok) throw new Error(await redactedError(response));
      const page = (await response.json()) as AgentRunPage;
      setHistory(page.items);
      const requested =
        preferredRunId ??
        new URLSearchParams(window.location.search).get("run") ??
        undefined;
      const visible =
        requested && page.items.some((item) => item.run_id === requested)
          ? requested
          : page.items[0]?.run_id;
      if (visible) selectRun(visible);
    },
    [selectRun],
  );

  useEffect(() => {
    void loadHistory().catch((error: Error) => setErrorCode(error.message));
  }, [loadHistory]);

  useEffect(() => {
    if (!selectedRunId) return;
    void loadRun(selectedRunId).catch((error: Error) =>
      setErrorCode(error.message),
    );
  }, [loadRun, selectedRunId]);

  useEffect(() => {
    if (!run || run.run_id !== selectedRunId || TERMINAL.has(run.state)) return;
    let timer: number | undefined;
    const synchronize = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
      if (document.visibilityState === "visible") {
        timer = window.setInterval(() => {
          void loadRun(run.run_id).catch((error: Error) =>
            setErrorCode(error.message),
          );
        }, 2000);
      }
    };
    synchronize();
    document.addEventListener("visibilitychange", synchronize);
    return () => {
      if (timer !== undefined) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", synchronize);
    };
  }, [loadRun, run, selectedRunId]);

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
        body: JSON.stringify({
          request_id: requestId,
          intent,
          business_inputs: parsed,
        }),
      });
      if (!response.ok) throw new Error(await redactedError(response));
      const created = (await response.json()) as AgentRun;
      setBusinessInputs("{}");
      setIntent("");
      selectRun(created.run_id);
      await loadHistory(created.run_id);
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
      const response = await authFetch(
        `/api/v1/enterprise/agent-runs/${run.run_id}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operation_key: operationKey(action) }),
        },
      );
      if (!response.ok) throw new Error(await redactedError(response));
      await loadRun(run.run_id);
      await loadHistory(run.run_id);
    } catch (error) {
      setErrorCode(error instanceof Error ? error.message : "REQUEST_FAILED");
    } finally {
      setBusy(false);
    }
  }

  const legalActions = useMemo(() => run?.legal_actions ?? [], [run]);

  return (
    <div className="min-h-full bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white px-4 py-3 sm:px-6">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-lg font-semibold">Agent Runs</h1>
          <button
            aria-label="Refresh run history"
            className="grid size-9 place-items-center rounded border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-40"
            disabled={busy}
            title="Refresh run history"
            type="button"
            onClick={() =>
              void loadHistory(selectedRunId ?? undefined).catch(
                (error: Error) => setErrorCode(error.message),
              )
            }
          >
            <ReloadIcon className="size-4" />
          </button>
        </div>
      </header>

      {errorCode && (
        <p
          role="alert"
          className="border-b border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700 sm:px-6"
        >
          {errorCode}
        </p>
      )}

      <div className="grid min-h-[calc(100vh-3.75rem)] md:grid-cols-[17rem_minmax(0,1fr)]">
        <aside className="border-b border-slate-200 bg-white md:border-b-0 md:border-r">
          <div className="border-b border-slate-200 px-4 py-3 text-xs font-semibold uppercase text-slate-500">
            Run history
          </div>
          <ol
            aria-label="Run history"
            className="max-h-64 overflow-y-auto md:max-h-[calc(100vh-7rem)]"
          >
            {history.map((item) => (
              <li key={item.run_id}>
                <button
                  className={`w-full border-b border-slate-100 px-4 py-3 text-left hover:bg-slate-50 ${
                    selectedRunId === item.run_id ? "bg-blue-50" : "bg-white"
                  }`}
                  type="button"
                  onClick={() => selectRun(item.run_id)}
                >
                  <span className="block truncate text-sm font-medium">
                    {item.run_id}
                  </span>
                  <span className="mt-1 flex items-center justify-between gap-2 text-xs text-slate-500">
                    <span>{item.state}</span>
                    <span>{item.provider_mode}</span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </aside>

        <main className="min-w-0">
          <section className="border-b border-slate-200 bg-white px-4 py-4 sm:px-6">
            <form
              className="grid gap-3 lg:grid-cols-[12rem_minmax(14rem,1fr)_minmax(14rem,1fr)_auto]"
              onSubmit={submit}
            >
              <label className="grid gap-1 text-xs font-medium text-slate-600">
                Request ID
                <input
                  aria-label="Request ID"
                  className="h-9 rounded border border-slate-300 px-3 text-sm"
                  required
                  value={requestId}
                  onChange={(event) => setRequestId(event.target.value)}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-slate-600">
                Intent
                <input
                  aria-label="Intent"
                  className="h-9 rounded border border-slate-300 px-3 text-sm"
                  required
                  value={intent}
                  onChange={(event) => setIntent(event.target.value)}
                />
              </label>
              <label className="grid gap-1 text-xs font-medium text-slate-600">
                Trusted business inputs
                <input
                  aria-label="Trusted business inputs"
                  className="h-9 rounded border border-slate-300 px-3 font-mono text-xs"
                  required
                  value={businessInputs}
                  onChange={(event) => setBusinessInputs(event.target.value)}
                />
              </label>
              <button
                className="self-end rounded bg-blue-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
                disabled={busy}
              >
                Create run
              </button>
            </form>
          </section>

          {run && (
            <div className="grid gap-0 xl:grid-cols-[minmax(0,1fr)_22rem]">
              <section className="min-w-0 border-b border-slate-200 px-4 py-5 sm:px-6 xl:border-b-0 xl:border-r">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <p className="text-xs font-semibold uppercase text-slate-500">
                      Current state
                    </p>
                    <h2
                      className="mt-1 text-xl font-semibold"
                      data-testid="run-state"
                    >
                      {run.state}
                    </h2>
                    <p className="mt-1 text-sm text-slate-600">
                      <span className="font-medium">
                        {run.pack_display_name}
                      </span>
                      {` (${run.pack_id} v${run.pack_version})`}
                      <span className="ml-2 rounded border border-slate-300 px-2 py-0.5 text-xs uppercase">
                        {run.provider_mode}
                      </span>
                    </p>
                    {run.reason_code && (
                      <p className="mt-1 text-sm text-slate-500">
                        {run.reason_code}
                      </p>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {legalActions.map((action) => (
                      <button
                        key={action}
                        type="button"
                        disabled={busy}
                        onClick={() => void command(action)}
                        className="rounded border border-slate-300 bg-white px-3 py-2 text-sm capitalize disabled:opacity-40"
                      >
                        {action}
                      </button>
                    ))}
                  </div>
                </div>
                <ol
                  className="mt-6 divide-y divide-slate-200 border-y border-slate-200"
                  aria-label="Governed plan"
                >
                  {run.plan.map((step) => (
                    <li
                      key={step.sequence}
                      className="flex items-center justify-between gap-3 py-3 text-sm"
                    >
                      <span>
                        {step.sequence}. {step.role}
                      </span>
                      <span className="font-medium uppercase text-slate-500">
                        {step.state}
                      </span>
                    </li>
                  ))}
                </ol>
              </section>

              <section className="px-4 py-5 sm:px-6">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="text-sm font-semibold">Timeline</h2>
                  {TERMINAL.has(run.state) && (
                    <a
                      aria-label="Download redacted report"
                      className="grid size-9 place-items-center rounded border border-slate-300 bg-white hover:bg-slate-50"
                      href={`/api/v1/enterprise/agent-runs/${run.run_id}/report`}
                      title="Download redacted report"
                    >
                      <DownloadIcon className="size-4" />
                    </a>
                  )}
                </div>
                <ol className="mt-3 space-y-2" aria-label="Run timeline">
                  {events.map((event) => (
                    <li key={event.sequence} className="text-sm text-slate-700">
                      {event.sequence}. {event.stage} - {event.state}
                      {event.reason_code ? ` (${event.reason_code})` : ""}
                    </li>
                  ))}
                </ol>
                {decisionTrace && (
                  <div className="mt-6 border-t border-slate-200 pt-4">
                    <h2 className="text-sm font-semibold">Decision trace</h2>
                    <p className="mt-1 text-xs text-slate-500">
                      Non-authoritative evidence; actions come only from
                      legal_actions.
                    </p>
                    <ol className="mt-3 space-y-2" aria-label="Decision trace">
                      {decisionTrace.stages.map((stage) => (
                        <li
                          key={stage.stage}
                          className="rounded border border-slate-200 px-3 py-2 text-xs text-slate-700"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-medium capitalize">
                              {stage.stage}
                            </span>
                            <span className="uppercase text-slate-500">
                              {stage.status}
                            </span>
                          </div>
                          {stage.reason_code && (
                            <div className="mt-1 text-slate-500">
                              {stage.reason_code}
                            </div>
                          )}
                          {(stage.provider_calls != null ||
                            stage.repair_count != null) && (
                            <div className="mt-1 text-slate-500">
                              Calls {stage.provider_calls ?? 0} · Repairs{" "}
                              {stage.repair_count ?? 0}
                            </div>
                          )}
                          {(stage.duration_ms != null ||
                            stage.total_tokens != null) && (
                            <div className="mt-1 text-slate-500">
                              {stage.duration_ms != null
                                ? `${stage.duration_ms.toFixed(1)} ms`
                                : "Timing not recorded"}
                              {stage.total_tokens != null
                                ? ` · ${stage.total_tokens} tokens`
                                : ""}
                            </div>
                          )}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
              </section>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

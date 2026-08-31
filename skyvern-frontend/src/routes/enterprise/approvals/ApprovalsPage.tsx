import {
  CheckCircledIcon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useCallback, useEffect, useState } from "react";

import { useI18n } from "@/i18n/useI18n";
import type { MessageKey } from "@/i18n/locales";
import { authFetch } from "@/util/authFetch";

type ApprovalRequest = {
  approval_id: string;
  task_id: string;
  risk_level: string;
  risk_reason: string;
  operation_description: string | null;
  department_id: string;
  business_line_id: string | null;
  screenshot_path: string | null;
  requested_at: string;
  status: string;
};

const riskLabels: Record<string, MessageKey> = {
  low: "common.riskLow",
  medium: "common.riskMedium",
  high: "common.riskHigh",
  critical: "common.riskCritical",
};

const riskStyles: Record<string, string> = {
  low: "border-emerald-200 bg-emerald-50 text-emerald-700",
  medium: "border-amber-200 bg-amber-50 text-amber-700",
  high: "border-orange-200 bg-orange-50 text-orange-700",
  critical: "border-red-200 bg-red-50 text-red-700",
};

function ApprovalCard({
  item,
  deciding,
  onDecision,
}: {
  item: ApprovalRequest;
  deciding: boolean;
  onDecision: (id: string, approved: boolean, note: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const [note, setNote] = useState("");
  const riskLabel = riskLabels[item.risk_level];

  return (
    <article className="rounded-md border bg-background p-4 shadow-sm">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_12rem]">
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded border px-2 py-0.5 text-xs font-medium ${
                riskStyles[item.risk_level] ?? "border-gray-200 bg-gray-50 text-gray-700"
              }`}
            >
              {riskLabel ? t(riskLabel) : item.risk_level}
            </span>
            <span className="text-xs text-muted-foreground">
              {item.department_id}
              {item.business_line_id ? ` / ${item.business_line_id}` : ""}
            </span>
          </div>

          <div>
            <h2 className="text-sm font-semibold">
              {item.operation_description ?? item.task_id}
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {item.risk_reason}
            </p>
          </div>

          <dl className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
            <div>
              <dt className="inline font-medium">{t("approvals.task")}: </dt>
              <dd className="inline">{item.task_id}</dd>
            </div>
            <div>
              <dt className="inline font-medium">{t("approvals.requested")}: </dt>
              <dd className="inline">
                {new Date(item.requested_at).toLocaleString()}
              </dd>
            </div>
          </dl>

          {item.screenshot_path ? (
            <img
              src={item.screenshot_path}
              alt=""
              className="max-h-64 rounded border object-contain"
            />
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <input
            className="h-9 rounded-md border bg-background px-3 text-sm"
            placeholder={t("approvals.remarkPlaceholder")}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            disabled={deciding}
          />
          <button
            type="button"
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-primary-foreground disabled:opacity-50"
            onClick={() => void onDecision(item.approval_id, true, note)}
            disabled={deciding}
          >
            {deciding ? (
              <ReloadIcon className="size-4 animate-spin" />
            ) : (
              <CheckCircledIcon className="size-4" />
            )}
            {t("approvals.approve")}
          </button>
          <button
            type="button"
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-red-300 px-3 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            onClick={() => void onDecision(item.approval_id, false, note)}
            disabled={deciding}
          >
            <CrossCircledIcon className="size-4" />
            {t("approvals.reject")}
          </button>
        </div>
      </div>
    </article>
  );
}

export function ApprovalsPage() {
  const { t } = useI18n();
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await authFetch(
        "/api/v1/enterprise/approvals/pending",
      );
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      setApprovals((await response.json()) as ApprovalRequest[]);
    } catch (loadError) {
      setApprovals([]);
      setError(
        loadError instanceof Error ? loadError.message : t("common.unknownError"),
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(
    approvalId: string,
    approved: boolean,
    note: string,
  ) {
    setDecidingId(approvalId);
    setError(null);
    try {
      const action = approved ? "approve" : "reject";
      const response = await authFetch(
        `/api/v1/enterprise/approvals/${approvalId}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note }),
        },
      );
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as
          | { detail?: string }
          | null;
        throw new Error(body?.detail ?? `HTTP ${response.status}`);
      }
      setApprovals((current) =>
        current.filter((item) => item.approval_id !== approvalId),
      );
    } catch (decisionError) {
      setError(
        decisionError instanceof Error
          ? decisionError.message
          : t("common.unknownError"),
      );
    } finally {
      setDecidingId(null);
    }
  }

  return (
    <div className="space-y-4 p-6">
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold">{t("approvals.title")}</h1>
          <span className="rounded-full border px-2 py-0.5 text-xs font-medium">
            {approvals.length}
          </span>
        </div>
        <button
          type="button"
          className="inline-flex size-9 items-center justify-center rounded-md border hover:bg-muted"
          onClick={() => void load()}
          disabled={loading}
          title={t("common.refresh")}
        >
          <ReloadIcon className={`size-4 ${loading ? "animate-spin" : ""}`} />
        </button>
      </header>

      {error ? (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {t("common.error")}: {error}
        </div>
      ) : null}

      {loading ? (
        <div className="py-12 text-center text-sm text-muted-foreground">
          {t("common.loading")}
        </div>
      ) : approvals.length === 0 ? (
        <div className="rounded-md border border-dashed py-12 text-center text-sm text-muted-foreground">
          {t("approvals.allCaughtUp")}
        </div>
      ) : (
        <div className="space-y-3">
          {approvals.map((item) => (
            <ApprovalCard
              key={item.approval_id}
              item={item}
              deciding={decidingId === item.approval_id}
              onDecision={decide}
            />
          ))}
        </div>
      )}
    </div>
  );
}

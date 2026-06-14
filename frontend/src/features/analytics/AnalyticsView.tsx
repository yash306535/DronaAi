import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";
import { fetchExamAnalytics, type AnalyticsApi } from "@/features/analytics/api";
import { isReportPending } from "@/features/analytics/analyticsView";
import { ScoreDistributionChart } from "@/features/analytics/ScoreDistributionChart";
import { DifficultyHeatmap } from "@/features/analytics/DifficultyHeatmap";
import { AnomalySummary } from "@/features/analytics/AnomalySummary";
import { StudentReports } from "@/features/analytics/StudentReports";
import type { AnalyticsView as AnalyticsViewData } from "@/features/analytics/types";

export interface AnalyticsExamOption {
  id: string;
  title: string;
}

export interface AnalyticsViewProps {
  /** Admin access token. Defaults to the stored token; injectable for tests. */
  token?: string;
  /** Token store used to resolve the default token. */
  store?: TokenStore;
  /** REST client for the analytics fetch (mocked in tests). */
  api?: AnalyticsApi;
  /** Exams shown in the selector. */
  exams?: AnalyticsExamOption[];
  /** Pre-selected exam id (skips the picker; used by tests/previews). */
  initialExamId?: string;
  /**
   * Pre-resolved analytics payload. When provided the internal fetch is
   * bypassed — used by tests/storybook to render deterministic snapshots
   * without a server.
   */
  data?: AnalyticsViewData;
}

/**
 * Analyst post-exam analytics dashboard (task 22, Requirements 10.1, 10.2,
 * 10.3, 12.1).
 *
 * The admin picks (or is given) an exam id, which drives a
 * `GET /analytics/exams/{id}` fetch. The resolved {@link AnalyticsViewData}
 * (the backend `ExamAnalyticsRead` shape) is folded into four sections:
 *   - a {@link ScoreDistributionChart} over the fixed score bands (10.2)
 *   - a {@link DifficultyHeatmap} of per-topic accuracy/difficulty (10.3)
 *   - an {@link AnomalySummary} of flagged-anomaly counts (10.2)
 *   - a {@link StudentReports} list of per-student reports + suggestions (10.4)
 *
 * A Refresh action re-fetches the report, which is how a `report.ready` event
 * (or a partial → complete transition, 10.6/10.7) is reflected without a live
 * socket dependency.
 */
export function AnalyticsView({
  token,
  store = tokenStore,
  api,
  exams = [],
  initialExamId,
  data,
}: AnalyticsViewProps) {
  // Resolve token for parity with the other feature views (auth header is
  // injected by the shared apiClient; this keeps the surface consistent).
  void (token ?? store.getAccessToken());

  const [examId, setExamId] = useState<string>(
    initialExamId ?? exams[0]?.id ?? "",
  );
  const [draftExamId, setDraftExamId] = useState<string>(examId);

  return (
    <div
      data-theme="dashboard"
      className="min-h-screen bg-surface-0 text-on-surface"
    >
      <header className="flex flex-wrap items-center gap-4 border-b border-hairline bg-surface-1 px-6 py-3">
        <span className="text-lg font-bold tracking-wider">DRONA AI</span>
        <span className="text-sm text-on-surface-muted">Exam Analytics</span>

        <form
          className="ml-auto flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setExamId(draftExamId.trim());
          }}
        >
          <label className="flex items-center gap-2 text-xs text-on-surface-muted">
            <span className="sr-only">Exam</span>
            {exams.length > 0 ? (
              <select
                value={draftExamId}
                onChange={(e) => setDraftExamId(e.target.value)}
                aria-label="Select exam"
                className="focus-ring rounded-md border border-hairline bg-surface-2 px-2 py-1 text-xs text-on-surface"
              >
                <option value="">Choose an exam…</option>
                {exams.map((exam) => (
                  <option key={exam.id} value={exam.id}>
                    {exam.title}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={draftExamId}
                onChange={(e) => setDraftExamId(e.target.value)}
                placeholder="Exam id"
                aria-label="Exam id"
                className="focus-ring rounded-md border border-hairline bg-surface-2 px-2 py-1 text-xs text-on-surface"
              />
            )}
          </label>
          <Button type="submit" disabled={draftExamId.trim().length === 0}>
            Load
          </Button>
        </form>
      </header>

      {examId.length === 0 && !data ? (
        <main className="p-6">
          <p className="text-sm text-on-surface-muted">
            Enter an exam id to view its analytics.
          </p>
        </main>
      ) : (
        <AnalyticsReport key={examId} examId={examId} api={api} data={data} />
      )}
    </div>
  );
}

interface AnalyticsReportProps {
  examId: string;
  api?: AnalyticsApi;
  data?: AnalyticsViewData;
}

/**
 * The report body: fetches `GET /analytics/exams/{id}` for the active exam
 * (unless a pre-resolved payload is injected) and renders the four analytics
 * sections. Split out so the fetch effect remounts cleanly when the exam id
 * changes (via `key` on the parent).
 */
function AnalyticsReport({ examId, api, data }: AnalyticsReportProps) {
  const [report, setReport] = useState<AnalyticsViewData | undefined>(data);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (data) {
      setReport(data);
      return () => {};
    }
    if (examId.length === 0) return () => {};
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchExamAnalytics(examId, api)
      .then((view) => {
        if (!cancelled) setReport(view);
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load analytics.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [examId, api, data]);

  useEffect(() => load(), [load]);

  const pending = isReportPending(report);

  return (
    <main className="flex flex-col gap-6 p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Analytics</h1>
        {report && (
          <span className="font-mono text-xs text-on-surface-muted">
            mean {report.summary?.mean ?? 0}
          </span>
        )}
        {pending && (
          <span className="rounded bg-surface-2 px-2 py-0.5 text-xs text-on-surface-muted">
            Partial report — some sections pending
          </span>
        )}
        {!data && (
          <Button
            className="ml-auto"
            onClick={load}
            disabled={loading}
            aria-label="Refresh analytics"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </Button>
        )}
      </div>

      {error && (
        <p role="alert" className="text-sm text-crimson-400">
          {error}
        </p>
      )}

      {loading && !report ? (
        <p className="text-sm text-on-surface-muted">Loading analytics…</p>
      ) : !report ? (
        <p className="text-sm text-on-surface-muted">
          No analytics available for this exam yet.
        </p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          <section
            aria-label="Score distribution"
            className="rounded-md border border-hairline bg-surface-1 p-4"
          >
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
              Score distribution
            </h2>
            <ScoreDistributionChart summary={report.summary} />
          </section>

          <section
            aria-label="Anomaly summary"
            className="rounded-md border border-hairline bg-surface-1 p-4"
          >
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
              Anomaly summary
            </h2>
            <AnomalySummary summary={report.summary} />
          </section>

          <section
            aria-label="Difficulty heatmap"
            className="rounded-md border border-hairline bg-surface-1 p-4"
          >
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
              Difficulty heatmap
            </h2>
            <DifficultyHeatmap heatmap={report.difficulty_heatmap} />
          </section>

          <section
            aria-label="Student reports"
            className="rounded-md border border-hairline bg-surface-1 p-4 lg:row-span-2"
          >
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
              Student reports
            </h2>
            <StudentReports perStudent={report.per_student} />
          </section>
        </div>
      )}
    </main>
  );
}

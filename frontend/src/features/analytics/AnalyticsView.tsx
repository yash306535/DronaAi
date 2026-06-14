import { useCallback, useEffect, useState } from "react";
import {
  BarChart3,
  Download,
  Grid3x3,
  RefreshCw,
  Search,
  ShieldAlert,
  Users,
} from "lucide-react";
import { Button } from "@/components";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";
import {
  downloadExamReportPdf,
  fetchExamAnalytics,
  type AnalyticsApi,
} from "@/features/analytics/api";
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
    <div className="flex flex-col gap-6 text-on-surface">
      <header className="flex flex-wrap items-center gap-4 rounded-lg border border-[#e3e8ee] bg-white px-4 py-3 shadow-sm">
        <span className="flex items-center gap-2 text-sm font-semibold text-navy-900">
          <BarChart3 className="h-4 w-4 text-crimson-600" aria-hidden="true" />
          Exam Analytics
        </span>

        <form
          className="ml-auto flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setExamId(draftExamId.trim());
          }}
        >
          <label className="flex items-center gap-2 text-xs text-[#5a6270]">
            <span className="sr-only">Exam</span>
            {exams.length > 0 ? (
              <select
                value={draftExamId}
                onChange={(e) => setDraftExamId(e.target.value)}
                aria-label="Select exam"
                className="focus-ring rounded-md border border-[#cfd6e0] bg-white px-2 py-1.5 text-xs text-[#1a1d24]"
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
                className="focus-ring rounded-md border border-[#cfd6e0] bg-white px-2 py-1.5 text-xs text-[#1a1d24]"
              />
            )}
          </label>
          <Button type="submit" disabled={draftExamId.trim().length === 0}>
            <Search className="h-4 w-4" aria-hidden="true" />
            Load
          </Button>
        </form>
      </header>

      {examId.length === 0 && !data ? (
        <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-8 text-center text-sm text-[#8a93a2]">
          Choose an exam to view its analytics.
        </p>
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
  const [downloading, setDownloading] = useState(false);

  const handleDownloadPdf = useCallback(async () => {
    if (examId.length === 0) return;
    setDownloading(true);
    setError(null);
    try {
      const blob = await downloadExamReportPdf(examId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `exam-${examId}-report.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch {
      setError("Failed to download the PDF report.");
    } finally {
      setDownloading(false);
    }
  }, [examId]);

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
    <main className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Analytics</h1>
        {report && (
          <span className="rounded-full bg-bg-info px-2.5 py-0.5 font-mono text-xs text-info">
            mean {report.summary?.mean ?? 0}
          </span>
        )}
        {pending && (
          <span className="rounded-full bg-bg-warning px-2.5 py-0.5 text-xs text-warning">
            Partial report — some sections pending
          </span>
        )}
        {!data && (
          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="secondary"
              onClick={() => void handleDownloadPdf()}
              disabled={downloading || examId.length === 0}
              aria-label="Download PDF report"
            >
              <Download className="h-4 w-4" aria-hidden="true" />
              {downloading ? "Preparing…" : "PDF"}
            </Button>
            <Button onClick={load} disabled={loading} aria-label="Refresh analytics">
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {loading ? "Refreshing…" : "Refresh"}
            </Button>
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="rounded-md bg-bg-danger px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {loading && !report ? (
        <p className="text-sm text-[#5a6270]">Loading analytics…</p>
      ) : !report ? (
        <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-8 text-center text-sm text-[#8a93a2]">
          No analytics available for this exam yet.
        </p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          <section
            aria-label="Score distribution"
            className="rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm"
          >
            <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
              <BarChart3 className="h-4 w-4" aria-hidden="true" />
              Score distribution
            </h2>
            <ScoreDistributionChart summary={report.summary} />
          </section>

          <section
            aria-label="Anomaly summary"
            className="rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm"
          >
            <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
              <ShieldAlert className="h-4 w-4" aria-hidden="true" />
              Anomaly summary
            </h2>
            <AnomalySummary summary={report.summary} />
          </section>

          <section
            aria-label="Difficulty heatmap"
            className="rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm"
          >
            <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
              <Grid3x3 className="h-4 w-4" aria-hidden="true" />
              Difficulty heatmap
            </h2>
            <DifficultyHeatmap heatmap={report.difficulty_heatmap} />
          </section>

          <section
            aria-label="Student reports"
            className="rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm lg:row-span-2"
          >
            <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
              <Users className="h-4 w-4" aria-hidden="true" />
              Student reports
            </h2>
            <StudentReports perStudent={report.per_student} />
          </section>
        </div>
      )}
    </main>
  );
}

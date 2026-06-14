// Analytics REST actions (task 22).
//
// Thin wrapper over the shared apiClient for the admin analytics endpoint:
//   - fetch an exam's full analytics  (GET /analytics/exams/{id})
//
// Accepts an injectable client (defaulting to the shared apiClient) so it can
// be unit-tested with a mocked client and no real backend.

import { apiClient, type ApiClient } from "@/lib/apiClient";
import { tokenStore } from "@/lib/tokenStore";
import type { AnalyticsView } from "@/features/analytics/types";

/** The subset of ApiClient these helpers depend on (eases mocking in tests). */
export type AnalyticsApi = Pick<ApiClient, "get">;

/**
 * Fetch the full analytics report for an exam (Requirement 10.1).
 * `GET /analytics/exams/{id}` — admin-scoped on the backend. Returns the
 * `ExamAnalyticsRead` shape (summary, difficulty_heatmap, per_student).
 */
export function fetchExamAnalytics(
  examId: string,
  api: AnalyticsApi = apiClient,
): Promise<AnalyticsView> {
  return api.get<AnalyticsView>(
    `/analytics/exams/${encodeURIComponent(examId)}`,
  );
}

/**
 * Download an exam's analytics as a PDF (Requirement 10.1).
 * `GET /analytics/exams/{id}/report.pdf` — admin-scoped. Returns a Blob so the
 * caller can trigger a browser download. Uses a direct authenticated fetch
 * since the shared apiClient parses JSON/text rather than binary bodies.
 */
export async function downloadExamReportPdf(examId: string): Promise<Blob> {
  const base = (
    (import.meta as unknown as { env?: Record<string, string | undefined> }).env
      ?.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1"
  ).replace(/\/+$/, "");
  const token = tokenStore.getAccessToken();
  const response = await fetch(
    `${base}/analytics/exams/${encodeURIComponent(examId)}/report.pdf`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    },
  );
  if (!response.ok) {
    throw new Error(`Report download failed with status ${response.status}`);
  }
  return response.blob();
}

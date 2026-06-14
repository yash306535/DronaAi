// Analytics REST actions (task 22).
//
// Thin wrapper over the shared apiClient for the admin analytics endpoint:
//   - fetch an exam's full analytics  (GET /analytics/exams/{id})
//
// Accepts an injectable client (defaulting to the shared apiClient) so it can
// be unit-tested with a mocked client and no real backend.

import { apiClient, type ApiClient } from "@/lib/apiClient";
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

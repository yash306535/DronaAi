// Invigilator REST actions (task 18).
//
// Thin wrappers over the shared apiClient for the two invigilator-scoped
// session operations the console needs:
//   - fetch a session's anomalies   (GET  /sessions/{id}/anomalies)
//   - terminate a session           (POST /sessions/{id}/terminate)
//
// Both accept an injectable client (defaulting to the shared apiClient) so they
// can be unit-tested with a mocked client and no real backend.

import { apiClient, type ApiClient } from "@/lib/apiClient";
import type { Anomaly, SessionRead } from "@/types";

/** The subset of ApiClient these helpers depend on (eases mocking in tests). */
export type InvigilatorApi = Pick<ApiClient, "get" | "post">;

/**
 * Fetch the anomalies recorded for a session (Req 12.1 detail panel).
 * `GET /sessions/{id}/anomalies` — invigilator/admin scoped on the backend.
 */
export function fetchSessionAnomalies(
  sessionId: string,
  api: InvigilatorApi = apiClient,
): Promise<Anomaly[]> {
  return api.get<Anomaly[]>(
    `/sessions/${encodeURIComponent(sessionId)}/anomalies`,
  );
}

/**
 * Force-end a session (Req 5.10). `POST /sessions/{id}/terminate` — available
 * to invigilators/admins; the backend sets status to `terminated` and rejects
 * subsequent answer submissions. Returns the updated session state.
 */
export function terminateSession(
  sessionId: string,
  api: InvigilatorApi = apiClient,
): Promise<SessionRead> {
  return api.post<SessionRead>(
    `/sessions/${encodeURIComponent(sessionId)}/terminate`,
  );
}

// Student exam-session REST actions (task 16.4).
//
// Thin, injectable wrappers over the shared apiClient for the four session
// operations the exam portal needs:
//   - start a session + fetch the student's own paper (POST /sessions/{exam_id}/start)
//   - save/update an answer                            (POST /sessions/{id}/answers)
//   - submit the exam                                  (POST /sessions/{id}/submit)
//   - ingest a batch of session events                 (POST /sessions/{id}/events)
//
// Every helper accepts an injectable client (defaulting to the shared
// apiClient) so the portal can be unit-tested with a mocked client and no real
// backend. The returned paper NEVER contains answer keys — the backend strips
// them and a pre-transmission guard blocks any answer-key field (Req 5.3/14.2).

import { apiClient, type ApiClient } from "@/lib/apiClient";
import type {
  AnswerRead,
  SessionEventBatch,
  SessionEventRead,
  SessionRead,
  StudentPaper,
} from "@/types";

/** The subset of ApiClient the exam portal depends on (eases mocking). */
export type ExamApi = Pick<ApiClient, "post">;

/**
 * Response of `POST /sessions/{exam_id}/start`: the created session state plus
 * the student's own answer-key-free paper. The backend serializes
 * `SessionRead` fields alongside a nested `paper`. `duration_minutes` is
 * surfaced when the backend includes it so the portal can drive the countdown.
 */
export interface StartSessionResponse extends SessionRead {
  paper: StudentPaper;
  duration_minutes?: number;
}

/**
 * Start a session for an exam and return the student's own paper (Req 5.1).
 * The backend rejects a duplicate active/submitted session (Req 5.2) — the
 * resulting ApiError surfaces to the caller.
 */
export function startSession(
  examId: string,
  api: ExamApi = apiClient,
): Promise<StartSessionResponse> {
  return api.post<StartSessionResponse>(
    `/sessions/${encodeURIComponent(examId)}/start`,
  );
}

/**
 * Persist or update an answer for an active session (Req 5.4). The backend
 * records the authoritative server-side time spent; the session must be active
 * or the request is rejected (Req 5.5).
 */
export function saveAnswer(
  sessionId: string,
  questionId: string,
  response: string,
  api: ExamApi = apiClient,
): Promise<AnswerRead> {
  return api.post<AnswerRead>(
    `/sessions/${encodeURIComponent(sessionId)}/answers`,
    { question_id: questionId, response },
  );
}

/** Finalize an active session → `submitted` (Req 5.6). */
export function submitExam(
  sessionId: string,
  api: ExamApi = apiClient,
): Promise<SessionRead> {
  return api.post<SessionRead>(
    `/sessions/${encodeURIComponent(sessionId)}/submit`,
  );
}

/**
 * Ingest a batch of 1-100 session events (Req 5.8). Batches larger than 100
 * are rejected by the backend (Req 5.9); the client-side {@link EventBatcher}
 * chunks the queue so each POST stays at or below the limit.
 */
export function postEvents(
  sessionId: string,
  batch: SessionEventBatch,
  api: ExamApi = apiClient,
): Promise<SessionEventRead[]> {
  return api.post<SessionEventRead[]>(
    `/sessions/${encodeURIComponent(sessionId)}/events`,
    batch,
  );
}

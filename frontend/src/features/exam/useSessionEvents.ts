// Session-event capture hook for the student exam portal (task 16.4).
//
// Captures behavioral telemetry the Sentinel agent consumes — tab blur/focus,
// paste, copy, question view, and a periodic heartbeat — and feeds them into an
// {@link EventBatcher}. The batcher flushes to `POST /sessions/{id}/events` in
// chunks of at most 100 events (Requirement 5.8/5.9): on a timer, on tab blur,
// and on page hide so telemetry is not lost when the student leaves the tab.
//
// All effectful seams (the poster, document/window targets, timers) default to
// the real browser but are injectable so the wiring can be unit-tested.

import { useEffect, useMemo, useRef } from "react";
import { EventBatcher, type EventPoster } from "@/features/exam/eventBatcher";
import { postEvents, type ExamApi } from "@/features/exam/examApi";
import type { SessionEventKind } from "@/types";

/** Default interval between automatic batch flushes (ms). */
export const DEFAULT_FLUSH_INTERVAL_MS = 10_000;
/** Default interval between heartbeat events (ms). */
export const DEFAULT_HEARTBEAT_INTERVAL_MS = 15_000;

/** A minimal event-target seam (document / window implement this). */
export interface EventTargetLike {
  addEventListener(type: string, listener: (event: Event) => void): void;
  removeEventListener(type: string, listener: (event: Event) => void): void;
}

export interface UseSessionEventsOptions {
  /** The active session id; events are POSTed to this session. */
  sessionId: string;
  /** Whether capture is active (typically while the session status is active). */
  active: boolean;
  /** API client for the events POST (defaults to the shared client). */
  api?: ExamApi;
  /** Override the batch poster directly (takes precedence over `api`). */
  poster?: EventPoster;
  /** Document target for paste/copy/visibility (defaults to global document). */
  documentTarget?: EventTargetLike;
  /** Window target for tab blur/focus + pagehide (defaults to global window). */
  windowTarget?: EventTargetLike;
  /** Automatic flush interval in ms. */
  flushIntervalMs?: number;
  /** Heartbeat interval in ms. */
  heartbeatIntervalMs?: number;
}

/** Controls returned by {@link useSessionEvents}. */
export interface UseSessionEventsResult {
  /** Record that the student viewed a question (Req 5.8 `question_view`). */
  recordQuestionView: (questionId: string, index: number) => void;
  /** Record an answer change (drives the `answer_change` telemetry). */
  recordAnswerChange: (questionId: string) => void;
  /** Force-flush the queued events immediately (e.g. just before submit). */
  flush: () => Promise<number>;
  /** Number of events currently queued and not yet flushed. */
  pending: () => number;
}

function resolveDocument(override?: EventTargetLike): EventTargetLike | null {
  if (override) return override;
  return typeof document !== "undefined" ? (document as EventTargetLike) : null;
}

function resolveWindow(override?: EventTargetLike): EventTargetLike | null {
  if (override) return override;
  return typeof window !== "undefined" ? (window as EventTargetLike) : null;
}

/**
 * Wire DOM telemetry listeners to a batched events poster for one session.
 *
 * The batcher and listeners are created once per session id. While `active`
 * is true the hook attaches blur/focus/paste/copy/visibility listeners plus a
 * heartbeat + flush timer; on teardown it detaches everything and performs a
 * final flush so no queued telemetry is dropped.
 */
export function useSessionEvents(
  options: UseSessionEventsOptions,
): UseSessionEventsResult {
  const {
    sessionId,
    active,
    api,
    poster,
    documentTarget,
    windowTarget,
    flushIntervalMs = DEFAULT_FLUSH_INTERVAL_MS,
    heartbeatIntervalMs = DEFAULT_HEARTBEAT_INTERVAL_MS,
  } = options;

  // One batcher per session id. The poster is resolved from an explicit
  // override, then a provided api client, then the shared apiClient.
  const batcher = useMemo(() => {
    const post: EventPoster =
      poster ?? ((batch) => postEvents(sessionId, batch, api));
    return new EventBatcher(post);
    // A new session id means a fresh queue; api/poster identity is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const batcherRef = useRef(batcher);
  batcherRef.current = batcher;

  const enqueue = useRef((kind: SessionEventKind, payload?: Record<string, unknown>) => {
    batcherRef.current.enqueue(kind, payload);
  });

  useEffect(() => {
    if (!active) return;
    const doc = resolveDocument(documentTarget);
    const win = resolveWindow(windowTarget);

    const onBlur = () => {
      enqueue.current("tab_blur");
      // A blur often precedes leaving the tab; flush opportunistically.
      void batcherRef.current.flush();
    };
    const onFocus = () => enqueue.current("tab_focus");
    const onPaste = () => enqueue.current("paste");
    const onCopy = () => enqueue.current("copy");
    const onVisibility = () => {
      const hidden =
        typeof document !== "undefined" ? document.hidden : false;
      enqueue.current(hidden ? "tab_blur" : "tab_focus");
      if (hidden) void batcherRef.current.flush();
    };
    const onPageHide = () => {
      void batcherRef.current.flush();
    };

    win?.addEventListener("blur", onBlur);
    win?.addEventListener("focus", onFocus);
    win?.addEventListener("pagehide", onPageHide);
    doc?.addEventListener("paste", onPaste);
    doc?.addEventListener("copy", onCopy);
    doc?.addEventListener("visibilitychange", onVisibility);

    const flushTimer = setInterval(() => {
      void batcherRef.current.flush();
    }, flushIntervalMs);
    const heartbeatTimer = setInterval(() => {
      enqueue.current("heartbeat");
    }, heartbeatIntervalMs);

    return () => {
      win?.removeEventListener("blur", onBlur);
      win?.removeEventListener("focus", onFocus);
      win?.removeEventListener("pagehide", onPageHide);
      doc?.removeEventListener("paste", onPaste);
      doc?.removeEventListener("copy", onCopy);
      doc?.removeEventListener("visibilitychange", onVisibility);
      clearInterval(flushTimer);
      clearInterval(heartbeatTimer);
      // Final flush so queued telemetry is not dropped on teardown.
      void batcherRef.current.flush();
    };
  }, [
    active,
    documentTarget,
    windowTarget,
    flushIntervalMs,
    heartbeatIntervalMs,
  ]);

  return useMemo(
    () => ({
      recordQuestionView: (questionId: string, index: number) =>
        enqueue.current("question_view", { question_id: questionId, index }),
      recordAnswerChange: (questionId: string) =>
        enqueue.current("answer_change", { question_id: questionId }),
      flush: () => batcherRef.current.flush(),
      pending: () => batcherRef.current.pending,
    }),
    [],
  );
}

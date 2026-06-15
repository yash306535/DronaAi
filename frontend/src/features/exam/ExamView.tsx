import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, CheckCircle2, Clock, PlayCircle, Send } from "lucide-react";
import { Button } from "@/components";
import { apiClient, type ApiClient } from "@/lib/apiClient";
import type { ExamRead, StudentPaper } from "@/types";
import {
  saveAnswer,
  startSession,
  submitExam,
  type ExamApi,
  type StartSessionResponse,
} from "@/features/exam/examApi";
import { QuestionCard } from "@/features/exam/QuestionCard";
import { ProctoringOverlay } from "@/features/exam/ProctoringOverlay";
import { useCountdown, formatRemaining } from "@/features/exam/useCountdown";
import { useProctoring, type UseProctoringDeps } from "@/features/exam/useProctoring";
import { useSessionEvents } from "@/features/exam/useSessionEvents";

/** Default debounce (ms) before an edited answer is autosaved. */
export const DEFAULT_AUTOSAVE_DEBOUNCE_MS = 800;

export interface ExamViewProps {
  /** The exam to start a session for. Falls back to a `?exam=` query param. */
  examId?: string;
  /** API client (defaults to the shared instance); injectable for tests. */
  api?: ExamApi & Pick<ApiClient, "post">;
  /** Proctoring dependency overrides (injected fakes in tests). */
  proctoringDeps?: UseProctoringDeps;
  /** Autosave debounce window in ms. */
  autosaveDebounceMs?: number;
  /** Disable starting proctoring automatically (used in tests). */
  disableProctoring?: boolean;
}

/** Lifecycle phases of the exam portal screen. */
type Phase = "choosing" | "loading" | "active" | "submitting" | "submitted" | "error";

function resolveExamId(explicit?: string): string | null {
  if (explicit) return explicit;
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  return params.get("exam");
}

/**
 * Student exam portal (Requirements 5.1, 5.3, 5.4, 5.6, 5.8).
 *
 * On mount it starts a session (`POST /sessions/{exam_id}/start`) and renders
 * the student's own {@link StudentPaper}, which never includes answer keys
 * (Req 5.3). Answers autosave on a short debounce (`POST /sessions/{id}/answers`,
 * Req 5.4). A countdown timer auto-submits at expiry, and the submit button
 * finalizes the session (`POST /sessions/{id}/submit`, Req 5.6). Behavioral
 * telemetry is captured and batched (≤100) to `POST /sessions/{id}/events`
 * (Req 5.8) and live webcam proctoring runs alongside via {@link useProctoring}.
 */
export function ExamView({
  examId,
  api = apiClient,
  proctoringDeps,
  autosaveDebounceMs = DEFAULT_AUTOSAVE_DEBOUNCE_MS,
  disableProctoring = false,
}: ExamViewProps) {
  const [chosenExamId, setChosenExamId] = useState<string | null>(() =>
    resolveExamId(examId),
  );
  const [phase, setPhase] = useState<Phase>(() =>
    resolveExamId(examId) ? "loading" : "choosing",
  );
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [paper, setPaper] = useState<StudentPaper | null>(null);
  const [durationSeconds, setDurationSeconds] = useState(0);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const isActive = phase === "active";

  // --- Session-event capture (batched ≤100) -------------------------------
  const events = useSessionEvents({
    sessionId: sessionId ?? "",
    active: isActive && sessionId !== null,
    api,
  });

  // --- Live proctoring -----------------------------------------------------
  const proctoring = useProctoring({
    sessionId: sessionId ?? "",
    autoStart: false,
    api,
    ...proctoringDeps,
  });
  const { start: startProctoring, stop: stopProctoring } = proctoring;

  // --- Start the session once an exam is chosen ---------------------------
  useEffect(() => {
    if (!chosenExamId) return;
    let cancelled = false;
    setPhase("loading");
    void (async () => {
      try {
        const res: StartSessionResponse = await startSession(chosenExamId, api);
        if (cancelled) return;
        setSessionId(res.id);
        setPaper(res.paper);
        setDurationSeconds((res.duration_minutes ?? 0) * 60);
        setPhase("active");
      } catch {
        if (cancelled) return;
        setPhase("error");
        setError("Unable to start your exam session. Please try again.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chosenExamId, api]);

  // Start/stop proctoring with the active session.
  useEffect(() => {
    if (disableProctoring) return;
    if (isActive && sessionId) startProctoring();
    return () => stopProctoring();
  }, [isActive, sessionId, disableProctoring, startProctoring, stopProctoring]);

  // --- Autosave (debounced per question) ----------------------------------
  const saveTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const flushSave = useCallback(
    (questionId: string, response: string) => {
      const sid = sessionId;
      if (!sid) return;
      void saveAnswer(sid, questionId, response, api).catch(() => {
        // Autosave failures are transient; the next edit retries. The student
        // is not interrupted mid-exam by a save hiccup.
      });
    },
    [api, sessionId],
  );

  const handleAnswerChange = useCallback(
    (questionId: string, value: string) => {
      setAnswers((prev) => ({ ...prev, [questionId]: value }));
      events.recordAnswerChange(questionId);

      const timers = saveTimers.current;
      const existing = timers.get(questionId);
      if (existing) clearTimeout(existing);
      timers.set(
        questionId,
        setTimeout(() => {
          timers.delete(questionId);
          flushSave(questionId, value);
        }, autosaveDebounceMs),
      );
    },
    [autosaveDebounceMs, events, flushSave],
  );

  // Clear any pending autosave timers on unmount.
  useEffect(() => {
    const timers = saveTimers.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  // --- Submit --------------------------------------------------------------
  const handleSubmit = useCallback(async () => {
    const sid = sessionId;
    if (!sid) return;
    setPhase("submitting");
    // Flush any debounced saves + queued telemetry before finalizing.
    for (const [, timer] of saveTimers.current) clearTimeout(timer);
    saveTimers.current.clear();
    await events.flush().catch(() => undefined);
    try {
      await submitExam(sid, api);
      stopProctoring();
      setPhase("submitted");
    } catch {
      setPhase("active");
      setError("Unable to submit your exam. Please try again.");
    }
  }, [api, events, sessionId, stopProctoring]);

  // Auto-submit when the countdown expires (one-shot).
  const remaining = useCountdown(durationSeconds, isActive && durationSeconds > 0, () => {
    void handleSubmit();
  });

  // --- Render --------------------------------------------------------------
  if (phase === "choosing") {
    return <ExamPicker onSelect={setChosenExamId} />;
  }

  if (phase === "loading") {
    return (
      <section className="mx-auto max-w-2xl">
        <h1 className="text-2xl font-semibold">Preparing your exam…</h1>
        <p className="mt-2 text-[#5a6270]">Starting your session.</p>
      </section>
    );
  }

  if (phase === "error") {
    return (
      <section className="mx-auto max-w-2xl">
        <h1 className="text-2xl font-semibold">Exam unavailable</h1>
        <p role="alert" className="mt-2 rounded-md bg-bg-danger px-3 py-2 text-danger">
          {error}
        </p>
      </section>
    );
  }

  if (phase === "submitted") {
    return (
      <section className="mx-auto max-w-2xl rounded-lg border border-[#e3e8ee] bg-white p-8 text-center shadow-sm">
        <CheckCircle2 className="mx-auto h-12 w-12 text-success" aria-hidden="true" />
        <h1 className="mt-3 text-2xl font-semibold">Exam submitted</h1>
        <p className="mt-2 text-[#5a6270]">
          Your responses have been recorded. You may close this window.
        </p>
      </section>
    );
  }

  const questions = paper?.questions ?? [];

  return (
    <section className="mx-auto grid max-w-5xl gap-6 lg:grid-cols-[1fr_18rem]">
      <div className="flex flex-col gap-4">
        <header className="flex items-center justify-between gap-4">
          <h1 className="text-2xl font-semibold">Your Exam</h1>
          {durationSeconds > 0 && (
            <span
              className="inline-flex items-center gap-2 rounded-md bg-navy-800 px-3 py-1 font-mono text-lg font-semibold tabular-nums text-white"
              aria-label="Time remaining"
              role="timer"
            >
              <Clock className="h-4 w-4" aria-hidden="true" />
              {formatRemaining(remaining)}
            </span>
          )}
        </header>

        {error && (
          <p role="alert" className="rounded-md bg-bg-danger px-3 py-2 text-sm font-medium text-danger">
            {error}
          </p>
        )}

        <div className="flex flex-col gap-4">
          {questions.map((question) => (
            <QuestionCard
              key={question.id}
              question={question}
              value={answers[question.id] ?? ""}
              disabled={phase === "submitting"}
              onView={() => events.recordQuestionView(question.id, question.index)}
              onChange={(value) => handleAnswerChange(question.id, value)}
            />
          ))}
        </div>

        <div className="flex justify-end">
          <Button
            onClick={() => void handleSubmit()}
            disabled={phase === "submitting"}
          >
            <Send className="h-4 w-4" aria-hidden="true" />
            {phase === "submitting" ? "Submitting…" : "Submit exam"}
          </Button>
        </div>
      </div>

      <div className="lg:sticky lg:top-6 lg:self-start">
        <ProctoringOverlay
          status={proctoring.status}
          escalating={proctoring.escalating}
          error={proctoring.error}
          videoRef={proctoring.videoRef}
        />
      </div>
    </section>
  );
}

/**
 * Exam chooser shown when the student arrives at the portal without a specific
 * exam id. Lists the exams currently open to sit (`GET /exams/available`) and
 * lets the student start one. Selecting an exam drives the session start in the
 * parent {@link ExamView}.
 */
function ExamPicker({ onSelect }: { onSelect: (examId: string) => void }) {
  const [exams, setExams] = useState<ExamRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiClient
      .listAvailableExams()
      .then((rows) => {
        if (!cancelled) setExams(rows);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load available exams.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="mx-auto max-w-2xl">
      <header className="mb-4 flex items-center gap-2">
        <BookOpen className="h-6 w-6 text-navy-800" aria-hidden="true" />
        <h1 className="text-2xl font-semibold">Available exams</h1>
      </header>

      {error && (
        <p role="alert" className="rounded-md bg-bg-danger px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {exams === null && !error ? (
        <p className="text-sm text-[#5a6270]">Loading exams…</p>
      ) : exams && exams.length === 0 ? (
        <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-8 text-center text-sm text-[#8a93a2]">
          No exams are open to sit right now.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {(exams ?? []).map((exam) => (
            <li
              key={exam.id}
              className="flex items-center justify-between gap-4 rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm"
            >
              <div className="min-w-0">
                <div className="truncate font-semibold text-[#1a1d24]">
                  {exam.title}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-xs text-[#5a6270]">
                  <span>{exam.subject}</span>
                  <span aria-hidden="true">·</span>
                  <span className="inline-flex items-center gap-1">
                    <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                    {exam.duration_minutes} min
                  </span>
                </div>
              </div>
              <Button onClick={() => onSelect(exam.id)}>
                <PlayCircle className="h-4 w-4" aria-hidden="true" />
                Start
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

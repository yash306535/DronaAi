// Unit tests for the student exam portal (task 16.5).
//
// These run under jsdom with an injected fake API client and injected
// proctoring deps (fake detector/webcam) so no real backend, camera, or WASM
// is needed. They cover the three required behaviors:
//   - batched session-event submission stays at or below 100 per POST (Req 5.8)
//   - the rendered paper never exposes an answer key (Req 5.3)
//   - a webcam error is surfaced in the proctoring overlay (Req 6.8)

import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ExamView } from "./ExamView";
import { MAX_EVENTS_PER_BATCH } from "./eventBatcher";
import type { FrameDetector, FrameMetrics } from "@/lib/mediapipe";
import type { StudentPaper } from "@/types";

const PAPER: StudentPaper = {
  id: "paper-1",
  exam_id: "exam-1",
  questions: [
    {
      id: "q1",
      index: 0,
      type: "mcq",
      prompt: "What is 2 + 2?",
      options: ["3", "4", "5"],
      topic: "Arithmetic",
      max_marks: 1,
    },
    {
      id: "q2",
      index: 1,
      type: "short",
      prompt: "Name a primary color.",
      options: null,
      topic: "Color",
      max_marks: 2,
    },
  ],
};

const START_RESPONSE = {
  id: "sess-1",
  exam_id: "exam-1",
  student_id: "stu-1",
  paper_id: "paper-1",
  status: "active" as const,
  started_at: "2026-06-13T00:00:00.000Z",
  submitted_at: null,
  integrity_score: 1,
  duration_minutes: 30,
  paper: PAPER,
};

const BENIGN: FrameMetrics = { faceCount: 1, gazeOffset: 0, headYawDeg: 0 };

/** A proctoring deps bundle whose webcam acquisition fails (Req 6.8). */
function deniedWebcamDeps() {
  const detector: FrameDetector = {
    detect: () => BENIGN,
    close: () => undefined,
  };
  return {
    createDetector: () => Promise.resolve(detector),
    getUserMedia: () => Promise.reject(new Error("denied")),
    captureFrame: () => "data:image/jpeg;base64,X",
    api: { post: vi.fn().mockResolvedValue({}) },
  };
}

/** A proctoring deps bundle that never starts a real loop (kept benign). */
function inertProctoringDeps() {
  const detector: FrameDetector = {
    detect: () => BENIGN,
    close: () => undefined,
  };
  const stream = {
    getTracks: () => [{ stop: () => undefined }],
  } as unknown as MediaStream;
  return {
    createDetector: () => Promise.resolve(detector),
    getUserMedia: () => Promise.resolve(stream),
    captureFrame: () => null,
    api: { post: vi.fn().mockResolvedValue({}) },
    scheduler: { start: () => () => undefined },
  };
}

function renderExam(api: { post: ReturnType<typeof vi.fn> }, proctoringDeps?: unknown) {
  return render(
    <MemoryRouter>
      <ExamView
        examId="exam-1"
        api={api as never}
        proctoringDeps={proctoringDeps as never}
        autosaveDebounceMs={1}
      />
    </MemoryRouter>,
  );
}

describe("ExamView", () => {
  it("renders the student's own paper with NO answer-key field (Req 5.3)", async () => {
    const post = vi.fn().mockResolvedValue(START_RESPONSE);
    renderExam({ post }, inertProctoringDeps());

    await screen.findByText("What is 2 + 2?");
    expect(screen.getByText("Name a primary color.")).toBeInTheDocument();

    // The rendered DOM must not leak any answer-key / solution text.
    const html = document.body.innerHTML;
    expect(html).not.toMatch(/answer_key/i);
    expect(html).not.toMatch(/correct/i);
    expect(html).not.toMatch(/solution/i);

    // The first POST was the session start; its response carries no key field.
    const startResult = await post.mock.results[0].value;
    expect(JSON.stringify(startResult)).not.toMatch(/answer_key/i);
  });

  it("batches session events into POSTs of at most 100 events (Req 5.8)", async () => {
    const post = vi.fn().mockResolvedValue(START_RESPONSE);
    renderExam({ post }, inertProctoringDeps());
    await screen.findByText("What is 2 + 2?");

    // Enqueue far more than one batch worth of telemetry, then flush.
    const total = MAX_EVENTS_PER_BATCH * 2 + 25;
    await act(async () => {
      for (let i = 0; i < total; i += 1) {
        window.dispatchEvent(new Event("blur"));
        window.dispatchEvent(new Event("focus"));
      }
    });

    await waitFor(() => {
      const eventCalls = post.mock.calls.filter(([path]) =>
        String(path).endsWith("/events"),
      );
      expect(eventCalls.length).toBeGreaterThan(0);
    });

    // Every events POST must carry between 1 and 100 events (Req 5.8/5.9).
    const eventCalls = post.mock.calls.filter(([path]) =>
      String(path).endsWith("/events"),
    );
    for (const [, body] of eventCalls) {
      const events = (body as { events: unknown[] }).events;
      expect(events.length).toBeGreaterThanOrEqual(1);
      expect(events.length).toBeLessThanOrEqual(MAX_EVENTS_PER_BATCH);
    }
  });

  it("surfaces a webcam-unavailable error in the proctoring overlay (Req 6.8)", async () => {
    const post = vi.fn().mockResolvedValue(START_RESPONSE);
    renderExam({ post }, deniedWebcamDeps());

    await screen.findByText("What is 2 + 2?");

    // The overlay surfaces the webcam error in an alert region.
    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((el) => /webcam/i.test(el.textContent ?? ""))).toBe(
        true,
      );
    });
  });
});

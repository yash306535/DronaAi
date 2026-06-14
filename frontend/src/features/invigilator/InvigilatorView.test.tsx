// Unit tests for the invigilator console (task 18).
//
// These render the InvigilatorView under jsdom with:
//   - an injected fake WebSocket (via the hook's `webSocketImpl` option) so a
//     live `session.update` can be folded into a clickable session tile with
//     no real server, and
//   - a mocked invigilator API client (`get`/`post`) so no real backend is hit.
//
// They cover the two required behaviors:
//   - selecting a session fetches `GET /sessions/{id}/anomalies` and renders
//     the results (Req 12.1)
//   - the Terminate action calls `POST /sessions/{id}/terminate` (Req 5.10)

import { act, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InvigilatorView } from "./InvigilatorView";
import type { InvigilatorSocketOptions } from "./useInvigilatorSocket";
import type { Anomaly, SessionRead, WSMessage } from "@/types";

// --- Fake WebSocket --------------------------------------------------------
//
// The wsClient attaches listeners via addEventListener and constructs the
// socket with `new WebSocketImpl(url)`. We capture instances so the test can
// drive open/message events deterministically.

type Listener = (event: unknown) => void;

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  readyState = 0;
  sent: unknown[] = [];
  private listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(listener);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  send(data: unknown): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
    this.emit("close", { code: 1006 });
  }

  emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  open(): void {
    this.readyState = 1;
    this.emit("open", {});
  }

  message(payload: unknown): void {
    this.emit("message", { data: JSON.stringify(payload) });
  }

  static reset(): void {
    FakeWebSocket.instances = [];
  }

  static last(): FakeWebSocket {
    const inst = FakeWebSocket.instances.at(-1);
    if (!inst) throw new Error("no FakeWebSocket instance created");
    return inst;
  }
}

const socketOptions: InvigilatorSocketOptions = {
  webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
  baseUrl: "ws://test.local",
};

function sessionUpdate(sessionId: string): WSMessage {
  return {
    type: "session.update",
    id: `u-${sessionId}`,
    ts: "2026-06-13T10:00:00.000Z",
    sessionId,
    source: "orchestrator",
    payload: { status: "active", integrityScore: 0.9 },
  };
}

const ANOMALIES: Anomaly[] = [
  {
    id: "an-1",
    session_id: "sess-1",
    source_agent: "guardian",
    category: "face_absent",
    score: 0.93,
    reasons: ["No face detected in frame"],
    evidence: {},
    detected_at: "2026-06-13T10:00:00.000Z",
    confirmed: true,
  },
];

const TERMINATED_SESSION: SessionRead = {
  id: "sess-1",
  exam_id: "exam-1",
  student_id: "stu-1",
  paper_id: "paper-1",
  status: "terminated",
  started_at: "2026-06-13T10:00:00.000Z",
  submitted_at: null,
  integrity_score: 0.9,
};

function renderConsole() {
  const get = vi.fn().mockResolvedValue(ANOMALIES);
  const post = vi.fn().mockResolvedValue(TERMINATED_SESSION);
  const api = { get, post };

  render(
    <InvigilatorView
      token="t"
      initialExamId="exam-1"
      socketOptions={socketOptions}
      api={api}
    />,
  );

  return { get, post };
}

describe("InvigilatorView", () => {
  beforeEach(() => {
    FakeWebSocket.reset();
  });

  it("fetches and renders a session's anomalies on selection (Req 12.1)", async () => {
    const { get } = renderConsole();

    // Drive a live session.update so a session tile appears.
    act(() => {
      FakeWebSocket.last().open();
      FakeWebSocket.last().message(sessionUpdate("sess-1"));
    });

    const tileButton = await screen.findByRole("button", {
      name: /inspect session sess-1/i,
    });
    act(() => {
      tileButton.click();
    });

    // The detail panel fetches GET /sessions/{id}/anomalies and renders rows.
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/sessions/sess-1/anomalies");
    });

    const detail = screen.getByLabelText("Session detail");
    expect(within(detail).getByText("face_absent")).toBeInTheDocument();
    expect(
      within(detail).getByText("• No face detected in frame"),
    ).toBeInTheDocument();
  });

  it("terminates the selected session via POST /sessions/{id}/terminate (Req 5.10)", async () => {
    const { post } = renderConsole();

    act(() => {
      FakeWebSocket.last().open();
      FakeWebSocket.last().message(sessionUpdate("sess-1"));
    });

    const tileButton = await screen.findByRole("button", {
      name: /inspect session sess-1/i,
    });
    act(() => {
      tileButton.click();
    });

    const terminateButton = await screen.findByRole("button", {
      name: /terminate session/i,
    });
    act(() => {
      terminateButton.click();
    });

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/sessions/sess-1/terminate");
    });

    // After termination the panel reflects the terminated status.
    await screen.findByRole("button", { name: /session terminated/i });
  });
});

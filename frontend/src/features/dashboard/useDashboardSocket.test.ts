import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BACKOFF_CAP_MS,
  DashboardSocketController,
  MAX_MESSAGES,
  MAX_RECONNECT_ATTEMPTS,
  backoffDelayMs,
  type DashboardSnapshot,
} from "@/features/dashboard/useDashboardSocket";
import type { WSMessage } from "@/types";

// --- Fake WebSocket --------------------------------------------------------
//
// Minimal stand-in for the browser WebSocket. The wsClient attaches listeners
// via addEventListener; the controller wires onOpen/onClose/onMessage through
// the wsClient. We drive open/message/close manually from the test.

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

  // --- test drivers ---
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

function makeController(
  overrides: {
    snapshot?: () => Promise<DashboardSnapshot>;
    maxReconnectAttempts?: number;
    snapshotRetryMs?: number;
  } = {},
): DashboardSocketController {
  return new DashboardSocketController("test-token", {
    webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    baseUrl: "ws://test.local",
    snapshot: overrides.snapshot ?? (async () => ({})),
    snapshotRetryMs: overrides.snapshotRetryMs,
    maxReconnectAttempts: overrides.maxReconnectAttempts,
  });
}

function agentMessage(id: string): WSMessage {
  return {
    type: "agent.message",
    id,
    ts: "2026-06-13T00:00:00.000Z",
    source: "Guardian",
    payload: { to: "Herald", text: `msg-${id}` },
  };
}

describe("backoffDelayMs", () => {
  it("follows 1s,2s,4s,...,30s cap schedule (Req 12.8)", () => {
    expect(backoffDelayMs(1)).toBe(1_000);
    expect(backoffDelayMs(2)).toBe(2_000);
    expect(backoffDelayMs(3)).toBe(4_000);
    expect(backoffDelayMs(4)).toBe(8_000);
    expect(backoffDelayMs(5)).toBe(16_000);
    // 2^5 * 1000 = 32000 -> capped at 30000
    expect(backoffDelayMs(6)).toBe(BACKOFF_CAP_MS);
    expect(backoffDelayMs(7)).toBe(BACKOFF_CAP_MS);
    expect(backoffDelayMs(20)).toBe(BACKOFF_CAP_MS);
  });
});

describe("DashboardSocketController", () => {
  beforeEach(() => {
    FakeWebSocket.reset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("caps the message ring buffer at 500, evicting the oldest (Req 12.7)", async () => {
    const controller = makeController();
    controller.start();
    FakeWebSocket.last().open();
    await vi.runOnlyPendingTimersAsync();

    const total = MAX_MESSAGES + 50;
    for (let i = 0; i < total; i += 1) {
      FakeWebSocket.last().message(agentMessage(String(i)));
    }

    const { messages } = controller.getSnapshot();
    expect(messages).toHaveLength(MAX_MESSAGES);
    // Oldest 50 evicted; first retained id is 50, last is total-1.
    expect(messages[0].id).toBe("50");
    expect(messages[messages.length - 1].id).toBe(String(total - 1));

    controller.stop();
  });

  it("reconnects with increasing exponential backoff delays (1s,2s,4s,...) (Req 12.8)", async () => {
    const controller = makeController();
    controller.start();
    // Initial connection (attempt 0). One socket created so far.
    expect(FakeWebSocket.instances).toHaveLength(1);

    // Drive consecutive *failed* reconnect attempts (the socket never opens),
    // so the attempt counter keeps climbing and the backoff delay grows.
    let sockets = 1;
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      FakeWebSocket.last().close();
      const delay = backoffDelayMs(attempt); // 1s,2s,4s,8s,16s
      // Just before the delay elapses, no new socket yet.
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(FakeWebSocket.instances).toHaveLength(sockets);
      // At the delay, a fresh connection attempt is made.
      await vi.advanceTimersByTimeAsync(1);
      sockets += 1;
      expect(FakeWebSocket.instances).toHaveLength(sockets);
    }

    controller.stop();
  });

  it("resets the backoff counter after a successful reconnect (Req 12.8)", async () => {
    const controller = makeController();
    controller.start();
    FakeWebSocket.last().open();
    await vi.runOnlyPendingTimersAsync();

    // Drop, then reconnect after the first backoff step (1s).
    FakeWebSocket.last().close();
    await vi.advanceTimersByTimeAsync(backoffDelayMs(1));
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.last().open();
    await vi.runOnlyPendingTimersAsync();

    // A subsequent drop must again reconnect after only the first step (1s),
    // proving the counter reset on the prior successful open.
    FakeWebSocket.last().close();
    await vi.advanceTimersByTimeAsync(backoffDelayMs(1) - 1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    controller.stop();
  });

  it("stops reconnecting after the max attempts when never reopened (Req 12.8)", async () => {
    const controller = makeController({ maxReconnectAttempts: 10 });
    controller.start();
    expect(FakeWebSocket.instances).toHaveLength(1);

    // First socket never opens; just keeps closing -> each close schedules the
    // next attempt without resetting the counter.
    let sockets = 1;
    for (let attempt = 1; attempt <= MAX_RECONNECT_ATTEMPTS; attempt += 1) {
      FakeWebSocket.last().close();
      await vi.advanceTimersByTimeAsync(BACKOFF_CAP_MS);
      sockets += 1;
      expect(FakeWebSocket.instances).toHaveLength(sockets);
    }

    // 11th close exceeds the budget -> no further socket created.
    FakeWebSocket.last().close();
    await vi.advanceTimersByTimeAsync(BACKOFF_CAP_MS);
    expect(FakeWebSocket.instances).toHaveLength(sockets);

    controller.stop();
  });

  it("sets degraded=true when the snapshot fails and keeps retrying (Req 12.9)", async () => {
    let shouldFail = true;
    let calls = 0;
    const snapshot = vi.fn(async (): Promise<DashboardSnapshot> => {
      calls += 1;
      if (shouldFail) throw new Error("snapshot failed");
      return {
        agents: { Guardian: { source: "Guardian", state: "active", load: 0.1, ts: "t" } },
      };
    });

    const controller = makeController({ snapshot, snapshotRetryMs: 2_000 });
    controller.start();
    FakeWebSocket.last().open();

    // First snapshot attempt rejects -> degraded.
    await vi.runOnlyPendingTimersAsync();
    expect(controller.getSnapshot().degraded).toBe(true);
    const callsAfterFirst = calls;

    // It keeps retrying on the snapshot-retry interval while failing (Req 12.9).
    await vi.advanceTimersByTimeAsync(2_000);
    expect(calls).toBeGreaterThan(callsAfterFirst);
    expect(controller.getSnapshot().degraded).toBe(true);

    // Once the snapshot recovers, the next retry clears degraded and resyncs.
    shouldFail = false;
    await vi.advanceTimersByTimeAsync(2_000);
    expect(controller.getSnapshot().degraded).toBe(false);
    expect(controller.getSnapshot().agents.Guardian.state).toBe("active");

    controller.stop();
  });

  it("resyncs state via the snapshot on reconnect and clears degraded", async () => {
    const snapshot = vi.fn(async (): Promise<DashboardSnapshot> => ({
      sessions: { s1: { sessionId: "s1", status: "active", integrityScore: 0.9, ts: "t" } },
    }));
    const controller = makeController({ snapshot });
    controller.start();
    FakeWebSocket.last().open();
    await vi.runOnlyPendingTimersAsync();

    expect(controller.getSnapshot().connected).toBe(true);
    expect(controller.getSnapshot().sessions.s1.status).toBe("active");
    expect(snapshot).toHaveBeenCalledTimes(1);

    // Drop -> reconnect -> snapshot called again.
    FakeWebSocket.last().close();
    expect(controller.getSnapshot().connected).toBe(false);
    await vi.advanceTimersByTimeAsync(backoffDelayMs(1));
    FakeWebSocket.last().open();
    await vi.runOnlyPendingTimersAsync();

    expect(controller.getSnapshot().connected).toBe(true);
    expect(snapshot).toHaveBeenCalledTimes(2);

    controller.stop();
  });
});

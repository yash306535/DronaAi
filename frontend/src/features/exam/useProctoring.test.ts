// Unit tests for the Stage-1 proctoring hook's escalation-gating wiring.
//
// These tests run under jsdom with NO real camera or WASM: the detector,
// webcam, frame capture, API client, clock, and frame scheduler are all
// injected fakes. We assert the two network-boundary invariants:
//   - normal frames make NO escalate call (Requirements 6.1, 7.2)
//   - a debounced signal triggers exactly one escalate POST carrying the
//     captured frame + the mapped snake_case local_signal (Requirement 7.2)

import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useProctoring, type FrameScheduler } from "./useProctoring";
import {
  createDefaultConfig,
  DEFAULT_MIN_DURATION_MS,
} from "./proctoring";
import type { FrameDetector, FrameMetrics } from "@/lib/mediapipe";

/** A scheduler that captures the tick so tests can step frames manually. */
function manualScheduler(): {
  scheduler: FrameScheduler;
  step: () => void;
  cancelled: () => boolean;
} {
  let registered: (() => void) | null = null;
  let cancelled = false;
  return {
    scheduler: {
      start(tick) {
        registered = tick;
        return () => {
          cancelled = true;
          registered = null;
        };
      },
    },
    step: () => registered?.(),
    cancelled: () => cancelled,
  };
}

/** A detector that returns whatever metrics the test currently dictates. */
function fakeDetector(getMetrics: () => FrameMetrics): {
  detector: FrameDetector;
  closed: () => boolean;
} {
  let closed = false;
  return {
    detector: {
      detect: () => getMetrics(),
      close: () => {
        closed = true;
      },
    },
    closed: () => closed,
  };
}

const BENIGN: FrameMetrics = { faceCount: 1, gazeOffset: 0, headYawDeg: 0 };
const FACE_ABSENT: FrameMetrics = { faceCount: 0, gazeOffset: 0, headYawDeg: 0 };

/** Common injected deps: fake webcam stream + JPEG capture + monotonic clock. */
function makeDeps(metrics: { current: FrameMetrics }) {
  const post = vi.fn().mockResolvedValue({
    anomaly_id: "a1",
    confirmed: true,
    category: "face_absent",
    score: 0.9,
    reasons: [],
    action: "alert_broadcast",
  });
  const { scheduler, step, cancelled } = manualScheduler();
  const { detector, closed } = fakeDetector(() => metrics.current);

  // A fake MediaStream whose tracks record stop() calls.
  const stop = vi.fn();
  const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream;

  let clock = 0;
  const frameInterval = createDefaultConfig().frameIntervalMs;

  return {
    post,
    step,
    cancelled,
    closed,
    stop,
    deps: {
      createDetector: () => Promise.resolve(detector),
      getUserMedia: () => Promise.resolve(stream),
      captureFrame: () => "data:image/jpeg;base64,FAKEFRAME",
      api: { post },
      scheduler,
      now: () => (clock += frameInterval),
      config: createDefaultConfig(),
    },
  };
}

describe("useProctoring escalation gating", () => {
  it("makes NO escalate call for normal (benign) frames", async () => {
    const metrics = { current: BENIGN };
    const { post, step, deps } = makeDeps(metrics);

    const { result } = renderHook(() =>
      useProctoring({ sessionId: "sess-1", ...deps }),
    );
    // Attach a stub video element so the loop has a source.
    act(() => {
      // @ts-expect-error assigning a minimal stub to the ref
      result.current.videoRef.current = { play: () => Promise.resolve() };
      result.current.start();
    });

    await waitFor(() => expect(result.current.status).toBe("running"));

    // Step many benign frames — far beyond any debounce window.
    for (let i = 0; i < 100; i++) act(() => step());

    expect(post).not.toHaveBeenCalled();
    expect(result.current.lastSignal).toBeNull();
  });

  it("triggers exactly one escalate POST when a debounced signal fires", async () => {
    const metrics = { current: FACE_ABSENT };
    const { post, step, deps } = makeDeps(metrics);

    const { result } = renderHook(() =>
      useProctoring({ sessionId: "sess-42", ...deps }),
    );
    act(() => {
      // @ts-expect-error minimal video stub
      result.current.videoRef.current = { play: () => Promise.resolve() };
      result.current.start();
    });
    await waitFor(() => expect(result.current.status).toBe("running"));

    // Face-absent must persist past the min duration before it escalates.
    const framesToPersist =
      DEFAULT_MIN_DURATION_MS / createDefaultConfig().frameIntervalMs;
    for (let i = 0; i < framesToPersist + 5; i++) act(() => step());

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/proctoring/sess-42/escalate");
    expect(body.frame).toBe("data:image/jpeg;base64,FAKEFRAME");
    expect(body.local_signal.kind).toBe("face_absent");
    expect(body.local_signal.duration_ms).toBeGreaterThanOrEqual(
      DEFAULT_MIN_DURATION_MS,
    );
    expect(body.local_signal.confidence_local).toBeGreaterThan(0);
    expect(body.local_signal.confidence_local).toBeLessThanOrEqual(1);

    // The condition clears: no further escalations for benign frames.
    metrics.current = BENIGN;
    for (let i = 0; i < 50; i++) act(() => step());
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("enters webcam_error and retains no escalation when getUserMedia rejects", async () => {
    const metrics = { current: BENIGN };
    const { post, deps } = makeDeps(metrics);
    deps.getUserMedia = () => Promise.reject(new Error("denied"));

    const { result } = renderHook(() =>
      useProctoring({ sessionId: "sess-2", ...deps }),
    );
    act(() => result.current.start());

    await waitFor(() => expect(result.current.status).toBe("webcam_error"));
    expect(result.current.error).toMatch(/webcam/i);
    expect(post).not.toHaveBeenCalled();
  });

  it("enters model_error when the detector fails to initialize", async () => {
    const metrics = { current: BENIGN };
    const { post, deps } = makeDeps(metrics);
    deps.createDetector = () => Promise.reject(new Error("wasm-fail"));

    const { result } = renderHook(() =>
      useProctoring({ sessionId: "sess-3", ...deps }),
    );
    act(() => result.current.start());

    await waitFor(() => expect(result.current.status).toBe("model_error"));
    expect(result.current.error).toMatch(/local screening is unavailable/i);
    expect(post).not.toHaveBeenCalled();
  });
});

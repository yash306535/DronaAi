// Guardian Stage 1 proctoring React hook.
//
// Orchestrates the local-first proctoring loop for a student session:
//   1. Initialize the MediaPipe FaceMesh detector (WASM). On load failure or a
//      >10s init timeout, enter `model_error` and surface that local screening
//      is unavailable, returning only benign signals (Requirement 6.9).
//   2. Request the webcam. On denial/unavailability, stop evaluation, enter
//      `webcam_error`, surface a webcam-unavailable error, and retain the
//      active session (Requirement 6.8).
//   3. Evaluate frames locally at ~10 FPS (>= the 5 FPS floor) feeding each to
//      the pure `evaluateFrame` with a persistent state/config. NO video,
//      frame, or pixel data leaves the device during normal screening (6.1).
//   4. ONLY when `evaluateFrame` returns a debounced signal (kind !== "none"),
//      capture a single downscaled JPEG and POST it to the escalation endpoint.
//      This is the hook's ONLY network call (Requirement 7.2).
//
// The MediaPipe and webcam APIs are injected via `UseProctoringDeps` so a
// vitest (jsdom) test can drive the escalation-gating wiring with a fake
// detector and frame clock — no real camera or WASM required.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  createDefaultConfig,
  createInitialState,
  evaluateFrame,
  type LocalSignal,
  type ProctorConfig,
  type ProctorState,
} from "@/features/exam/proctoring";
import {
  captureDownscaledJpeg,
  createFaceMeshDetector,
  type FrameDetector,
} from "@/lib/mediapipe";
import { apiClient, type ApiClient } from "@/lib/apiClient";
import type {
  EscalationRequest,
  EscalationResponse,
  LocalSignal as WireLocalSignal,
} from "@/types";

/** Lifecycle status surfaced to the exam UI. */
export type ProctoringStatus =
  | "initializing"
  | "running"
  | "webcam_error"
  | "model_error"
  | "stopped";

/** Public hook state. */
export interface ProctoringState {
  status: ProctoringStatus;
  /** Most recent debounced signal that triggered an escalation, if any. */
  lastSignal: LocalSignal | null;
  /** True while an escalation POST is in flight. */
  escalating: boolean;
  /** Human-readable error indication (webcam/model unavailable), else null. */
  error: string | null;
}

/** Controls + state returned by {@link useProctoring}. */
export interface UseProctoringResult extends ProctoringState {
  /** Begin initialization + evaluation. Idempotent while already running. */
  start: () => void;
  /** Stop evaluation and release the webcam/detector. Retains session state. */
  stop: () => void;
  /** Ref to attach to the <video> element the webcam stream feeds. */
  videoRef: React.RefObject<HTMLVideoElement>;
}

/** Injectable dependencies; defaults use the real MediaPipe/browser/API. */
export interface UseProctoringDeps {
  /** Create the frame detector (defaults to the real FaceMesh detector). */
  createDetector?: () => Promise<FrameDetector>;
  /** Acquire a webcam stream (defaults to navigator.mediaDevices.getUserMedia). */
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  /** Capture a JPEG from the video (defaults to the canvas-based capture). */
  captureFrame?: (video: HTMLVideoElement) => string | null;
  /** API client for the escalation POST (defaults to the shared client). */
  api?: Pick<ApiClient, "post">;
  /** Schedule the next evaluation tick; defaults to setInterval. */
  scheduler?: FrameScheduler;
  /** Monotonic clock in ms; defaults to performance.now/Date.now. */
  now?: () => number;
  /** Override the proctoring config (thresholds, durations, cooldowns). */
  config?: ProctorConfig;
  /** Model init timeout in ms (Requirement 6.9 default: 10s). */
  initTimeoutMs?: number;
}

/** Options to start a proctoring loop for a specific session. */
export interface UseProctoringOptions extends UseProctoringDeps {
  sessionId: string;
  /** Start automatically on mount. Defaults to false. */
  autoStart?: boolean;
}

/**
 * A frame scheduler seam. `start` runs `tick` repeatedly at `intervalMs` and
 * returns a cancel function. Defaults to setInterval; tests inject a manual
 * scheduler to step frames deterministically.
 */
export interface FrameScheduler {
  start: (tick: () => void, intervalMs: number) => () => void;
}

/** Default ~10 FPS evaluation interval (comfortably above the 5 FPS floor). */
export const DEFAULT_EVAL_INTERVAL_MS = 100;
/** Default model init timeout (Requirement 6.9). */
export const DEFAULT_INIT_TIMEOUT_MS = 10_000;

const defaultScheduler: FrameScheduler = {
  start(tick, intervalMs) {
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  },
};

function defaultNow(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/** Map the in-memory LocalSignal to the backend wire shape (snake_case). */
export function toWireSignal(signal: LocalSignal): WireLocalSignal {
  return {
    kind: signal.kind,
    duration_ms: signal.durationMs,
    confidence_local: signal.confidenceLocal,
  };
}

/**
 * React hook driving the two-stage proctoring loop for one session. See the
 * module header for the full lifecycle. All effectful dependencies are
 * injectable for testing.
 */
export function useProctoring(options: UseProctoringOptions): UseProctoringResult {
  const {
    sessionId,
    autoStart = false,
    createDetector = createFaceMeshDetector,
    getUserMedia,
    captureFrame = captureDownscaledJpeg,
    api = apiClient,
    scheduler = defaultScheduler,
    now = defaultNow,
    config,
    initTimeoutMs = DEFAULT_INIT_TIMEOUT_MS,
  } = options;

  const videoRef = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<ProctoringState>({
    status: "stopped",
    lastSignal: null,
    escalating: false,
    error: null,
  });

  // Mutable run-scoped refs (do not trigger re-renders).
  const detectorRef = useRef<FrameDetector | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const cancelLoopRef = useRef<(() => void) | null>(null);
  const proctorStateRef = useRef<ProctorState>(createInitialState());
  const configRef = useRef<ProctorConfig>(config ?? createDefaultConfig());
  const runningRef = useRef(false);
  const escalatingRef = useRef(false);

  // Keep the config ref in sync if the caller passes a new config object.
  useEffect(() => {
    configRef.current = config ?? createDefaultConfig();
  }, [config]);

  const resolveGetUserMedia = useCallback((): ((
    c: MediaStreamConstraints,
  ) => Promise<MediaStream>) | null => {
    if (getUserMedia) return getUserMedia;
    if (
      typeof navigator !== "undefined" &&
      navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function"
    ) {
      return navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    }
    return null;
  }, [getUserMedia]);

  const teardown = useCallback(() => {
    runningRef.current = false;
    cancelLoopRef.current?.();
    cancelLoopRef.current = null;
    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) track.stop();
      streamRef.current = null;
    }
    if (detectorRef.current) {
      try {
        detectorRef.current.close();
      } catch {
        // Closing a detector should never crash teardown.
      }
      detectorRef.current = null;
    }
  }, []);

  // One evaluation tick: detect → evaluate → escalate only on a debounced signal.
  const tick = useCallback(() => {
    if (!runningRef.current) return;
    const detector = detectorRef.current;
    const video = videoRef.current;
    if (!detector || !video) return;

    const ts = now();
    let metrics;
    try {
      metrics = detector.detect(video, ts);
    } catch {
      // A transient detection error degrades to a benign frame; keep screening.
      return;
    }

    const signal = evaluateFrame(
      metrics.faceCount,
      metrics.gazeOffset,
      metrics.headYawDeg,
      ts,
      proctorStateRef.current,
      configRef.current,
    );

    // Normal frames never leave the device (Req 6.1). Escalate ONLY on a
    // debounced signal (Req 7.2).
    if (signal.kind === "none") return;
    if (escalatingRef.current) return; // avoid overlapping escalations

    const frame = video ? captureFrame(video) : null;
    if (!frame) return;

    escalatingRef.current = true;
    setState((s) => ({ ...s, lastSignal: signal, escalating: true }));

    const body: EscalationRequest = {
      local_signal: toWireSignal(signal),
      frame,
    };
    void api
      .post<EscalationResponse>(`/proctoring/${sessionId}/escalate`, body)
      .catch(() => {
        // Escalation failures must not stop local screening; swallow + continue.
      })
      .finally(() => {
        escalatingRef.current = false;
        setState((s) => ({ ...s, escalating: false }));
      });
  }, [api, captureFrame, now, sessionId]);

  const start = useCallback(() => {
    if (runningRef.current) return;
    runningRef.current = true;
    proctorStateRef.current = createInitialState();
    setState({
      status: "initializing",
      lastSignal: null,
      escalating: false,
      error: null,
    });

    void (async () => {
      // 1. Initialize the detector, racing a hard init timeout (Req 6.9).
      let detector: FrameDetector;
      try {
        detector = await withTimeout(
          createDetector(),
          initTimeoutMs,
          () => now(),
        );
      } catch {
        if (!runningRef.current) return;
        runningRef.current = false;
        setState({
          status: "model_error",
          lastSignal: null,
          escalating: false,
          error: "Local screening is unavailable (model failed to load).",
        });
        return;
      }
      if (!runningRef.current) {
        detector.close();
        return;
      }
      detectorRef.current = detector;

      // 2. Request the webcam (Req 6.8 on failure).
      const gum = resolveGetUserMedia();
      if (!gum) {
        teardown();
        setState({
          status: "webcam_error",
          lastSignal: null,
          escalating: false,
          error: "Webcam is unavailable on this device.",
        });
        return;
      }
      let stream: MediaStream;
      try {
        stream = await gum({ video: true, audio: false });
      } catch {
        if (!runningRef.current) return;
        teardown();
        setState({
          status: "webcam_error",
          lastSignal: null,
          escalating: false,
          error: "Webcam access was denied or is unavailable.",
        });
        return;
      }
      if (!runningRef.current) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      streamRef.current = stream;
      const video = videoRef.current;
      if (video) {
        video.srcObject = stream;
        try {
          await video.play?.();
        } catch {
          // Autoplay rejection is non-fatal; frames still evaluate.
        }
      }

      // 3. Begin the local evaluation loop (~10 FPS).
      if (!runningRef.current) return;
      setState((s) => ({ ...s, status: "running", error: null }));
      cancelLoopRef.current = scheduler.start(tick, DEFAULT_EVAL_INTERVAL_MS);
    })();
  }, [
    createDetector,
    initTimeoutMs,
    now,
    resolveGetUserMedia,
    scheduler,
    teardown,
    tick,
  ]);

  const stop = useCallback(() => {
    teardown();
    setState((s) => ({
      ...s,
      status: "stopped",
      escalating: false,
    }));
  }, [teardown]);

  // Auto-start once on mount when requested; always tear down on unmount.
  useEffect(() => {
    if (autoStart) start();
    return () => teardown();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { ...state, start, stop, videoRef };
}

/**
 * Resolve `promise` but reject if it does not settle within `timeoutMs`. Used
 * to enforce the model init timeout (Requirement 6.9).
 */
function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  _now: () => number,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("init-timeout"));
    }, timeoutMs);
    promise.then(
      (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

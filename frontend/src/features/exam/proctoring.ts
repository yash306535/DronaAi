// Stage 1 — Local Gaze/Presence Screening (browser).
//
// Pure, framework-free logic for the Guardian agent's first proctoring stage.
// `evaluateFrame` classifies a single MediaPipe FaceMesh result into an
// instantaneous condition, debounces it across frames, and rate-limits
// escalations with a per-kind cooldown. No network, DOM, or camera access
// happens here — that keeps the function deterministic and trivially testable.
//
// Implements the design's "Stage 1 — Local Gaze/Presence Screening" algorithm
// and satisfies Requirements 6.2–6.7.

/** Candidate condition kinds that can escalate, plus the benign "none". */
export type LocalSignalKind =
  | "face_absent"
  | "multiple_faces"
  | "gaze_away"
  | "none";

/** The three debounceable/escalatable condition kinds. */
export type EscalatableKind = Exclude<LocalSignalKind, "none">;

/** All escalatable kinds, used to seed per-kind accumulator records. */
export const ESCALATABLE_KINDS: readonly EscalatableKind[] = [
  "face_absent",
  "multiple_faces",
  "gaze_away",
] as const;

/**
 * Debounced Stage-1 local signal.
 *
 * `kind` is `"none"` unless the active candidate has persisted beyond its
 * minimum duration AND its cooldown has elapsed. The shape is compatible with
 * the backend escalation payload (`LocalSignal` in `@/types`): map `durationMs`
 * → `duration_ms` and `confidenceLocal` → `confidence_local` at the network
 * boundary.
 */
export interface LocalSignal {
  kind: LocalSignalKind;
  durationMs: number;
  /** 0..1 local confidence, scaled by how far past the threshold we are. */
  confidenceLocal: number;
}

/**
 * Mutable per-session screening state. `consecutive` accumulates milliseconds
 * of continuous persistence per kind; `lastEscalationTs` records the most
 * recent escalation timestamp per kind for cooldown enforcement.
 */
export interface ProctorState {
  consecutive: Record<EscalatableKind, number>;
  lastEscalationTs: Record<EscalatableKind, number>;
}

/** Per-kind configuration of debounce and cooldown windows. */
export interface ProctorConfig {
  /** Head-yaw magnitude (degrees) above which a frame is "gaze_away". */
  yawThreshold: number;
  /** Normalized gaze offset (0..1) above which a frame is "gaze_away". */
  gazeThreshold: number;
  /** Minimum continuous persistence (ms) per kind before signaling. */
  minDurationMs: Record<EscalatableKind, number>;
  /** Minimum gap (ms) per kind between successive escalations. */
  cooldownMs: Record<EscalatableKind, number>;
  /** Nominal interval (ms) between evaluated frames; the debounce step size. */
  frameIntervalMs: number;
}

/** Default head-yaw threshold (degrees); valid range 10–60 (Req 6.4). */
export const DEFAULT_YAW_THRESHOLD_DEG = 25;
/** Default normalized gaze-offset threshold; valid range 0.10–0.90 (Req 6.4). */
export const DEFAULT_GAZE_THRESHOLD = 0.3;
/** Default per-kind minimum duration (ms); valid range 500–10000 (Req 6.5). */
export const DEFAULT_MIN_DURATION_MS = 2_000;
/** Default per-kind cooldown (ms); valid range 5000–300000 (Req 6.6). */
export const DEFAULT_COOLDOWN_MS = 30_000;
/** Default frame interval (ms): 10 FPS, comfortably above the 5 FPS floor (Req 6.1). */
export const DEFAULT_FRAME_INTERVAL_MS = 100;

/**
 * Build a default ProctorConfig. Pass overrides to tune individual fields;
 * per-kind records are filled for every escalatable kind from a single value.
 */
export function createDefaultConfig(
  overrides: Partial<ProctorConfig> = {},
): ProctorConfig {
  const minDurationMs = fillPerKind(DEFAULT_MIN_DURATION_MS);
  const cooldownMs = fillPerKind(DEFAULT_COOLDOWN_MS);
  return {
    yawThreshold: DEFAULT_YAW_THRESHOLD_DEG,
    gazeThreshold: DEFAULT_GAZE_THRESHOLD,
    minDurationMs: { ...minDurationMs, ...overrides.minDurationMs },
    cooldownMs: { ...cooldownMs, ...overrides.cooldownMs },
    frameIntervalMs: DEFAULT_FRAME_INTERVAL_MS,
    ...stripPerKind(overrides),
  };
}

/**
 * Build a fresh ProctorState with all accumulators zeroed. `lastEscalationTs`
 * is seeded to -Infinity so the first qualifying condition is never blocked by
 * a spurious cooldown.
 */
export function createInitialState(): ProctorState {
  return {
    consecutive: fillPerKind(0),
    lastEscalationTs: fillPerKind(Number.NEGATIVE_INFINITY),
  };
}

/**
 * Evaluate one FaceMesh result. Pure with respect to its inputs aside from the
 * documented mutation of `state`.
 *
 * Preconditions:  faceCount >= 0; gazeOffset in [0,1]; now is monotonic ms.
 * Postconditions: returns a debounced signal; mutates state's accumulators so
 *                 that exactly one accumulator (the active candidate's) is
 *                 non-decreasing while all others reset to 0; returns kind
 *                 "none" unless both the persistence and cooldown gates pass.
 *
 * Validates Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 6.7.
 */
export function evaluateFrame(
  faceCount: number,
  gazeOffset: number, // 0 = centered, 1 = fully off-screen
  headYawDeg: number,
  now: number,
  state: ProctorState,
  cfg: ProctorConfig,
): LocalSignal {
  // 1. Classify the instantaneous condition.
  const candidate = classify(faceCount, gazeOffset, headYawDeg, cfg);

  // 2. Accumulate / reset debounce timers. The active candidate's accumulator
  //    grows by one frame interval; every other accumulator resets to 0.
  for (const kind of ESCALATABLE_KINDS) {
    if (kind === candidate) {
      state.consecutive[kind] += cfg.frameIntervalMs;
    } else {
      state.consecutive[kind] = 0;
    }
  }

  if (candidate === "none") {
    return { kind: "none", durationMs: 0, confidenceLocal: 0 };
  }

  // 3. Require persistence beyond the per-kind minimum duration.
  const dur = state.consecutive[candidate];
  if (dur < cfg.minDurationMs[candidate]) {
    return { kind: "none", durationMs: dur, confidenceLocal: 0 };
  }

  // 4. Enforce the per-kind cooldown to avoid flooding the escalation endpoint.
  if (now - state.lastEscalationTs[candidate] < cfg.cooldownMs[candidate]) {
    return { kind: "none", durationMs: dur, confidenceLocal: 0 };
  }

  // Both gates passed: record the escalation timestamp and emit the signal.
  state.lastEscalationTs[candidate] = now;
  return {
    kind: candidate,
    durationMs: dur,
    confidenceLocal: clamp(dur / cfg.minDurationMs[candidate], 0, 1),
  };
}

/** Classify a single frame into a candidate condition kind. */
function classify(
  faceCount: number,
  gazeOffset: number,
  headYawDeg: number,
  cfg: ProctorConfig,
): LocalSignalKind {
  if (faceCount === 0) return "face_absent";
  if (faceCount > 1) return "multiple_faces";
  if (gazeOffset > cfg.gazeThreshold || Math.abs(headYawDeg) > cfg.yawThreshold) {
    return "gaze_away";
  }
  return "none";
}

/** Clamp `value` into the inclusive range [min, max]. */
function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/** Build a per-kind record where every escalatable kind maps to `value`. */
function fillPerKind(value: number): Record<EscalatableKind, number> {
  return {
    face_absent: value,
    multiple_faces: value,
    gaze_away: value,
  };
}

/** Drop the per-kind record fields so scalar overrides can spread cleanly. */
function stripPerKind(
  overrides: Partial<ProctorConfig>,
): Partial<ProctorConfig> {
  const { minDurationMs: _min, cooldownMs: _cool, ...rest } = overrides;
  return rest;
}

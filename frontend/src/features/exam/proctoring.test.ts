import { describe, expect, it } from "vitest";
import {
  createDefaultConfig,
  createInitialState,
  DEFAULT_COOLDOWN_MS,
  DEFAULT_FRAME_INTERVAL_MS,
  DEFAULT_GAZE_THRESHOLD,
  DEFAULT_MIN_DURATION_MS,
  DEFAULT_YAW_THRESHOLD_DEG,
  ESCALATABLE_KINDS,
  evaluateFrame,
  type ProctorConfig,
} from "./proctoring";

// A centered, single-face frame that classifies as "none".
const CLEAR = { faceCount: 1, gaze: 0, yaw: 0 };

describe("createDefaultConfig", () => {
  it("uses the requirement defaults (Req 6.4/6.5/6.6)", () => {
    const cfg = createDefaultConfig();
    expect(cfg.yawThreshold).toBe(DEFAULT_YAW_THRESHOLD_DEG);
    expect(cfg.gazeThreshold).toBe(DEFAULT_GAZE_THRESHOLD);
    expect(cfg.frameIntervalMs).toBe(DEFAULT_FRAME_INTERVAL_MS);
    for (const kind of ESCALATABLE_KINDS) {
      expect(cfg.minDurationMs[kind]).toBe(DEFAULT_MIN_DURATION_MS);
      expect(cfg.cooldownMs[kind]).toBe(DEFAULT_COOLDOWN_MS);
    }
  });

  it("applies scalar and per-kind overrides", () => {
    const cfg = createDefaultConfig({
      yawThreshold: 40,
      gazeThreshold: 0.5,
      frameIntervalMs: 200,
      minDurationMs: { face_absent: 500 } as ProctorConfig["minDurationMs"],
      cooldownMs: { gaze_away: 5_000 } as ProctorConfig["cooldownMs"],
    });
    expect(cfg.yawThreshold).toBe(40);
    expect(cfg.gazeThreshold).toBe(0.5);
    expect(cfg.frameIntervalMs).toBe(200);
    expect(cfg.minDurationMs.face_absent).toBe(500);
    // Untouched per-kind entries keep their default.
    expect(cfg.minDurationMs.multiple_faces).toBe(DEFAULT_MIN_DURATION_MS);
    expect(cfg.cooldownMs.gaze_away).toBe(5_000);
    expect(cfg.cooldownMs.face_absent).toBe(DEFAULT_COOLDOWN_MS);
  });
});

describe("evaluateFrame classification (Req 6.2/6.3/6.4)", () => {
  it("classifies zero faces as face_absent once debounced", () => {
    const cfg = createDefaultConfig({ frameIntervalMs: 1_000 });
    const state = createInitialState();
    // First frame: 1000ms accrued, still under the 2000ms minimum -> none.
    expect(evaluateFrame(0, 0, 0, 1_000, state, cfg).kind).toBe("none");
    // Second frame: 2000ms accrued, meets the 2000ms minimum -> escalates.
    expect(evaluateFrame(0, 0, 0, 2_000, state, cfg).kind).toBe("face_absent");
  });

  it("classifies more than one face as multiple_faces", () => {
    const cfg = createDefaultConfig({ frameIntervalMs: 2_000 });
    const state = createInitialState();
    const sig = evaluateFrame(3, 0, 0, 10_000, state, cfg);
    expect(sig.kind).toBe("multiple_faces");
  });

  it("classifies gaze offset above threshold as gaze_away", () => {
    const cfg = createDefaultConfig({ frameIntervalMs: 2_000 });
    const state = createInitialState();
    const sig = evaluateFrame(1, DEFAULT_GAZE_THRESHOLD + 0.01, 0, 10_000, state, cfg);
    expect(sig.kind).toBe("gaze_away");
  });

  it("classifies yaw magnitude above threshold as gaze_away (both signs)", () => {
    const cfg = createDefaultConfig({ frameIntervalMs: 2_000 });
    const statePos = createInitialState();
    expect(evaluateFrame(1, 0, 90, 10_000, statePos, cfg).kind).toBe("gaze_away");
    const stateNeg = createInitialState();
    expect(evaluateFrame(1, 0, -90, 10_000, stateNeg, cfg).kind).toBe("gaze_away");
  });

  it("treats a centered single face as none", () => {
    const cfg = createDefaultConfig({ frameIntervalMs: 2_000 });
    const state = createInitialState();
    const sig = evaluateFrame(CLEAR.faceCount, CLEAR.gaze, CLEAR.yaw, 10_000, state, cfg);
    expect(sig).toEqual({ kind: "none", durationMs: 0, confidenceLocal: 0 });
  });
});

describe("evaluateFrame debounce (Req 6.5)", () => {
  it("returns none until persistence reaches the minimum duration", () => {
    const cfg = createDefaultConfig({
      frameIntervalMs: 500,
      minDurationMs: { face_absent: 2_000 } as ProctorConfig["minDurationMs"],
    });
    const state = createInitialState();
    let now = 0;
    // 500, 1000, 1500 -> still under 2000.
    for (const expectedDur of [500, 1_000, 1_500]) {
      now += 500;
      const sig = evaluateFrame(0, 0, 0, now, state, cfg);
      expect(sig.kind).toBe("none");
      expect(sig.durationMs).toBe(expectedDur);
    }
    // 2000 -> meets minimum, escalates.
    now += 500;
    expect(evaluateFrame(0, 0, 0, now, state, cfg).kind).toBe("face_absent");
  });

  it("resets the accumulator when the condition lapses", () => {
    const cfg = createDefaultConfig({
      frameIntervalMs: 1_000,
      minDurationMs: { face_absent: 2_000 } as ProctorConfig["minDurationMs"],
    });
    const state = createInitialState();
    evaluateFrame(0, 0, 0, 1_000, state, cfg); // 1000ms accrued
    // A clear frame resets the face_absent accumulator.
    evaluateFrame(1, 0, 0, 2_000, state, cfg);
    expect(state.consecutive.face_absent).toBe(0);
    // Now it must accrue from scratch again.
    expect(evaluateFrame(0, 0, 0, 3_000, state, cfg).kind).toBe("none");
    expect(evaluateFrame(0, 0, 0, 4_000, state, cfg).kind).toBe("face_absent");
  });
});

describe("evaluateFrame cooldown (Req 6.6/6.7)", () => {
  it("suppresses re-escalation until the cooldown elapses", () => {
    const cfg = createDefaultConfig({
      frameIntervalMs: 2_000,
      minDurationMs: { face_absent: 2_000 } as ProctorConfig["minDurationMs"],
      cooldownMs: { face_absent: 30_000 } as ProctorConfig["cooldownMs"],
    });
    const state = createInitialState();
    // First escalation at t=2000.
    const first = evaluateFrame(0, 0, 0, 2_000, state, cfg);
    expect(first.kind).toBe("face_absent");
    expect(state.lastEscalationTs.face_absent).toBe(2_000);
    // t=4000: only 2000ms since last escalation < 30000ms cooldown -> none.
    expect(evaluateFrame(0, 0, 0, 4_000, state, cfg).kind).toBe("none");
    // t=32000: cooldown elapsed -> escalates again.
    const second = evaluateFrame(0, 0, 0, 32_000, state, cfg);
    expect(second.kind).toBe("face_absent");
    expect(state.lastEscalationTs.face_absent).toBe(32_000);
  });

  it("records the escalation timestamp and scales confidence with duration", () => {
    const cfg = createDefaultConfig({
      frameIntervalMs: 4_000,
      minDurationMs: { face_absent: 2_000 } as ProctorConfig["minDurationMs"],
    });
    const state = createInitialState();
    const sig = evaluateFrame(0, 0, 0, 5_000, state, cfg);
    expect(sig.kind).toBe("face_absent");
    expect(sig.durationMs).toBe(4_000);
    // dur/min = 4000/2000 = 2, clamped to 1.
    expect(sig.confidenceLocal).toBe(1);
    expect(state.lastEscalationTs.face_absent).toBe(5_000);
  });
});

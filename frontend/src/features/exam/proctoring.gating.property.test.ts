import { describe, expect, it } from "vitest";
import fc from "fast-check";
import {
  createDefaultConfig,
  createInitialState,
  ESCALATABLE_KINDS,
  evaluateFrame,
  type EscalatableKind,
  type LocalSignalKind,
  type ProctorConfig,
} from "./proctoring";

// A single synthetic frame fed to evaluateFrame.
interface Frame {
  faceCount: number;
  gazeOffset: number;
  headYawDeg: number;
}

// Mirror evaluateFrame's classification so the property can reason about the
// expected candidate independently of the implementation under test.
function classify(frame: Frame, cfg: ProctorConfig): LocalSignalKind {
  if (frame.faceCount === 0) return "face_absent";
  if (frame.faceCount > 1) return "multiple_faces";
  if (
    frame.gazeOffset > cfg.gazeThreshold ||
    Math.abs(frame.headYawDeg) > cfg.yawThreshold
  ) {
    return "gaze_away";
  }
  return "none";
}

// Generator producing frames biased to hit every condition kind, so the
// property exercises classification, accumulation, and reset paths.
const frameArb: fc.Arbitrary<Frame> = fc.oneof(
  // face_absent
  fc.record({
    faceCount: fc.constant(0),
    gazeOffset: fc.double({ min: 0, max: 1, noNaN: true }),
    headYawDeg: fc.double({ min: -90, max: 90, noNaN: true }),
  }),
  // multiple_faces
  fc.record({
    faceCount: fc.integer({ min: 2, max: 4 }),
    gazeOffset: fc.double({ min: 0, max: 1, noNaN: true }),
    headYawDeg: fc.double({ min: -90, max: 90, noNaN: true }),
  }),
  // single face, varied gaze/yaw -> gaze_away or none
  fc.record({
    faceCount: fc.constant(1),
    gazeOffset: fc.double({ min: 0, max: 1, noNaN: true }),
    headYawDeg: fc.double({ min: -90, max: 90, noNaN: true }),
  }),
);

// A config arbitrary that stays within the requirement-allowed ranges.
const configArb: fc.Arbitrary<ProctorConfig> = fc
  .record({
    yawThreshold: fc.integer({ min: 10, max: 60 }),
    gazeThreshold: fc.double({ min: 0.1, max: 0.9, noNaN: true }),
    frameIntervalMs: fc.integer({ min: 50, max: 200 }),
    minFaceAbsent: fc.integer({ min: 500, max: 10_000 }),
    minMultiple: fc.integer({ min: 500, max: 10_000 }),
    minGaze: fc.integer({ min: 500, max: 10_000 }),
    coolFaceAbsent: fc.integer({ min: 5_000, max: 300_000 }),
    coolMultiple: fc.integer({ min: 5_000, max: 300_000 }),
    coolGaze: fc.integer({ min: 5_000, max: 300_000 }),
  })
  .map((r) =>
    createDefaultConfig({
      yawThreshold: r.yawThreshold,
      gazeThreshold: r.gazeThreshold,
      frameIntervalMs: r.frameIntervalMs,
      minDurationMs: {
        face_absent: r.minFaceAbsent,
        multiple_faces: r.minMultiple,
        gaze_away: r.minGaze,
      },
      cooldownMs: {
        face_absent: r.coolFaceAbsent,
        multiple_faces: r.coolMultiple,
        gaze_away: r.coolGaze,
      },
    }),
  );

describe("Property 1: Stage-1 escalation gating", () => {
  // Validates: Requirements 6.5, 6.6, 6.7
  it("only escalates after persistence >= minDurationMs AND cooldown elapsed, and maintains the loop invariant", () => {
    fc.assert(
      fc.property(
        configArb,
        fc.array(frameArb, { minLength: 1, maxLength: 300 }),
        (cfg, frames) => {
          const state = createInitialState();

          // Track our own reference accumulators and last-escalation times to
          // independently verify the gating decision and the loop invariant.
          const refConsecutive: Record<EscalatableKind, number> = {
            face_absent: 0,
            multiple_faces: 0,
            gaze_away: 0,
          };
          const refLastEsc: Record<EscalatableKind, number> = {
            face_absent: Number.NEGATIVE_INFINITY,
            multiple_faces: Number.NEGATIVE_INFINITY,
            gaze_away: Number.NEGATIVE_INFINITY,
          };

          let now = 0;
          for (const frame of frames) {
            now += cfg.frameIntervalMs;
            const candidate = classify(frame, cfg);

            // Compute the expected reference state BEFORE calling the function.
            for (const kind of ESCALATABLE_KINDS) {
              if (kind === candidate) {
                refConsecutive[kind] += cfg.frameIntervalMs;
              } else {
                refConsecutive[kind] = 0;
              }
            }

            const sig = evaluateFrame(
              frame.faceCount,
              frame.gazeOffset,
              frame.headYawDeg,
              now,
              state,
              cfg,
            );

            // --- Loop invariant: accumulators match the reference, exactly one
            // (the candidate's) is non-zero for a sustained condition, the rest
            // are reset to 0.
            for (const kind of ESCALATABLE_KINDS) {
              expect(state.consecutive[kind]).toBe(refConsecutive[kind]);
              if (kind !== candidate) {
                expect(state.consecutive[kind]).toBe(0);
              }
            }

            // --- Gating: decide whether an escalation is expected.
            const persisted =
              candidate !== "none" &&
              refConsecutive[candidate] >= cfg.minDurationMs[candidate];
            const cooldownElapsed =
              candidate !== "none" &&
              now - refLastEsc[candidate] >= cfg.cooldownMs[candidate];
            const shouldEscalate = persisted && cooldownElapsed;

            if (shouldEscalate) {
              expect(sig.kind).toBe(candidate);
              expect(sig.durationMs).toBe(refConsecutive[candidate as EscalatableKind]);
              expect(sig.confidenceLocal).toBeGreaterThan(0);
              expect(sig.confidenceLocal).toBeLessThanOrEqual(1);
              // The escalation timestamp must be recorded for cooldown tracking.
              expect(state.lastEscalationTs[candidate as EscalatableKind]).toBe(now);
              refLastEsc[candidate as EscalatableKind] = now;
            } else {
              // No escalation: signal is none and no new timestamp was recorded.
              expect(sig.kind).toBe("none");
              if (candidate !== "none") {
                expect(state.lastEscalationTs[candidate as EscalatableKind]).toBe(
                  refLastEsc[candidate as EscalatableKind],
                );
              }
            }
          }
        },
      ),
      { numRuns: 500 },
    );
  });
});

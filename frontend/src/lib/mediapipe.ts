// MediaPipe FaceMesh wrapper for Guardian Stage 1 local screening.
//
// Thin, framework-free seam over `@mediapipe/tasks-vision`'s FaceLandmarker:
// - loads the FaceLandmarker WASM runtime + model (`createFaceMeshDetector`),
// - derives the lightweight metrics Stage 1 needs (`faceCount`, `gazeOffset`,
//   `headYawDeg`) from raw landmarks (`deriveFrameMetrics`),
// - captures a single downscaled JPEG from a <video> for escalation only
//   (`captureDownscaledJpeg`).
//
// Everything here runs locally in the browser. No network traffic originates
// from this module — the only bytes that ever leave the device are the single
// JPEG produced by `captureDownscaledJpeg`, and only when the hook decides to
// escalate (Requirements 6.1, 7.2).

import {
  FaceLandmarker,
  FilesetResolver,
  type FaceLandmarkerResult,
  type NormalizedLandmark,
} from "@mediapipe/tasks-vision";

/**
 * Lightweight per-frame metrics consumed by `evaluateFrame` in
 * `features/exam/proctoring.ts`. Derived entirely on-device from landmarks.
 */
export interface FrameMetrics {
  /** Number of detected faces (0, 1, or more). */
  faceCount: number;
  /** Normalized 0..1 estimate of how far the gaze is off-center. */
  gazeOffset: number;
  /** Estimated head yaw magnitude in degrees (signed: + = turned right). */
  headYawDeg: number;
}

/**
 * A frame detector seam. The real implementation wraps a MediaPipe
 * FaceLandmarker; tests can substitute a fake so the proctoring hook's
 * escalation-gating wiring can run under jsdom without WASM or a camera.
 */
export interface FrameDetector {
  /** Detect faces in the current frame and derive Stage-1 metrics. */
  detect(source: HTMLVideoElement, timestampMs: number): FrameMetrics;
  /** Release native resources. */
  close(): void;
}

/** Default CDN base path for the FaceLandmarker WASM fileset. */
export const DEFAULT_WASM_BASE_PATH =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm";

/** Default hosted FaceLandmarker model bundle (includes iris landmarks). */
export const DEFAULT_MODEL_ASSET_PATH =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

/** Options for initializing the MediaPipe FaceMesh detector. */
export interface FaceMeshDetectorOptions {
  /** Base path/URL for the WASM fileset. */
  wasmBasePath?: string;
  /** Model bundle path/URL. */
  modelAssetPath?: string;
  /** Maximum number of faces to detect (>=2 so "multiple faces" is observable). */
  numFaces?: number;
}

// --- Landmark indices (MediaPipe FaceMesh canonical topology) ---------------
// These are stable indices into the 468/478-point FaceMesh landmark array.
const NOSE_TIP = 1;
const LEFT_FACE_EDGE = 234;
const RIGHT_FACE_EDGE = 454;
// Eye corners (image-left eye and image-right eye).
const LEFT_EYE_OUTER = 33;
const LEFT_EYE_INNER = 133;
const RIGHT_EYE_INNER = 362;
const RIGHT_EYE_OUTER = 263;
// Iris centers (present only when the model emits 478 refined landmarks).
const LEFT_IRIS_CENTER = 468;
const RIGHT_IRIS_CENTER = 473;

/** Scale factor converting the normalized nose-offset ratio to degrees. */
const YAW_RATIO_TO_DEGREES = 180;

/**
 * Initialize a MediaPipe FaceLandmarker and wrap it in a {@link FrameDetector}.
 * Loads the WASM fileset then the model bundle in `VIDEO` running mode.
 *
 * Throws if the runtime or model fails to load; the proctoring hook races this
 * against a 10s timeout and surfaces a "local screening unavailable" error
 * (Requirement 6.9).
 */
export async function createFaceMeshDetector(
  options: FaceMeshDetectorOptions = {},
): Promise<FrameDetector> {
  const wasmBasePath = options.wasmBasePath ?? DEFAULT_WASM_BASE_PATH;
  const modelAssetPath = options.modelAssetPath ?? DEFAULT_MODEL_ASSET_PATH;
  const numFaces = options.numFaces ?? 2;

  const fileset = await FilesetResolver.forVisionTasks(wasmBasePath);
  const landmarker = await FaceLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetPath },
    runningMode: "VIDEO",
    numFaces,
    // Iris/refined landmarks improve the gaze estimate when available.
    outputFaceBlendshapes: false,
    outputFacialTransformationMatrixes: false,
  });

  return {
    detect(source: HTMLVideoElement, timestampMs: number): FrameMetrics {
      const result = landmarker.detectForVideo(source, timestampMs);
      return deriveFrameMetrics(result);
    },
    close(): void {
      landmarker.close();
    },
  };
}

/**
 * Derive Stage-1 metrics from a raw FaceLandmarker result. Pure and
 * defensively guarded so malformed/empty results degrade to a benign frame
 * rather than throwing.
 */
export function deriveFrameMetrics(result: FaceLandmarkerResult): FrameMetrics {
  const faces = result.faceLandmarks ?? [];
  const faceCount = faces.length;
  if (faceCount === 0) {
    return { faceCount: 0, gazeOffset: 0, headYawDeg: 0 };
  }
  const lm = faces[0];
  return {
    faceCount,
    gazeOffset: estimateGazeOffset(lm),
    headYawDeg: estimateHeadYawDeg(lm),
  };
}

/**
 * Estimate signed head yaw (degrees) from the horizontal offset of the nose
 * tip relative to the midpoint of the face edges, normalized by face width.
 */
function estimateHeadYawDeg(lm: NormalizedLandmark[]): number {
  const nose = lm[NOSE_TIP];
  const left = lm[LEFT_FACE_EDGE];
  const right = lm[RIGHT_FACE_EDGE];
  if (!nose || !left || !right) return 0;
  const centerX = (left.x + right.x) / 2;
  const width = Math.abs(right.x - left.x);
  if (width < 1e-6) return 0;
  const ratio = (nose.x - centerX) / width; // ~ -0.5..0.5 for frontal-ish poses
  return clamp(ratio * YAW_RATIO_TO_DEGREES, -90, 90);
}

/**
 * Estimate normalized gaze offset (0 = centered, 1 = fully off-center) from
 * each iris center's position within its eye's corner span, averaged across
 * both eyes. Falls back to 0 when refined iris landmarks are unavailable.
 */
function estimateGazeOffset(lm: NormalizedLandmark[]): number {
  const leftIris = lm[LEFT_IRIS_CENTER];
  const rightIris = lm[RIGHT_IRIS_CENTER];
  if (!leftIris || !rightIris) return 0;

  const leftOffset = eyeOffset(lm[LEFT_EYE_OUTER], lm[LEFT_EYE_INNER], leftIris);
  const rightOffset = eyeOffset(
    lm[RIGHT_EYE_INNER],
    lm[RIGHT_EYE_OUTER],
    rightIris,
  );
  const offsets = [leftOffset, rightOffset].filter(
    (v): v is number => v !== null,
  );
  if (offsets.length === 0) return 0;
  const avg = offsets.reduce((a, b) => a + b, 0) / offsets.length;
  return clamp(avg, 0, 1);
}

/**
 * How far the iris center sits from the midpoint of an eye's two corners,
 * expressed as a 0..1 fraction of eye width (0 = centered, 1 = at a corner).
 */
function eyeOffset(
  cornerA: NormalizedLandmark | undefined,
  cornerB: NormalizedLandmark | undefined,
  iris: NormalizedLandmark,
): number | null {
  if (!cornerA || !cornerB) return null;
  const midX = (cornerA.x + cornerB.x) / 2;
  const width = Math.abs(cornerB.x - cornerA.x);
  if (width < 1e-6) return null;
  // Distance from center is up to half the eye width, so scale by 2 → 0..1.
  return clamp((Math.abs(iris.x - midX) / width) * 2, 0, 1);
}

/**
 * Capture a single downscaled JPEG data URL from a <video> element. Used ONLY
 * when an escalation fires; never during normal frame screening. Returns null
 * if a 2D canvas context is unavailable.
 */
export function captureDownscaledJpeg(
  video: HTMLVideoElement,
  maxWidth = 320,
  quality = 0.6,
): string | null {
  const srcW = video.videoWidth || maxWidth;
  const srcH = video.videoHeight || Math.round((maxWidth * 3) / 4);
  const scale = Math.min(1, maxWidth / srcW);
  const width = Math.max(1, Math.round(srcW * scale));
  const height = Math.max(1, Math.round(srcH * scale));

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(video, 0, 0, width, height);
  return canvas.toDataURL("image/jpeg", quality);
}

/** Clamp `value` into the inclusive range [min, max]. */
function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

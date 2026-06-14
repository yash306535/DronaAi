import type { ProctoringStatus } from "@/features/exam/useProctoring";

/** Human-readable label + dot color for each proctoring lifecycle status. */
const STATUS_META: Record<
  ProctoringStatus,
  { label: string; dot: string; text: string }
> = {
  initializing: {
    label: "Starting camera…",
    dot: "bg-warning",
    text: "text-warning",
  },
  running: { label: "Proctoring active", dot: "bg-success", text: "text-success" },
  webcam_error: { label: "Webcam unavailable", dot: "bg-crimson-600", text: "text-danger" },
  model_error: {
    label: "Local screening unavailable",
    dot: "bg-crimson-600",
    text: "text-danger",
  },
  stopped: { label: "Proctoring stopped", dot: "bg-[#8a93a2]", text: "text-[#8a93a2]" },
};

export interface ProctoringOverlayProps {
  status: ProctoringStatus;
  /** True while an escalated frame POST is in flight. */
  escalating: boolean;
  /** Webcam/model error indication, if any (Requirement 6.8/6.9). */
  error: string | null;
  /** Ref bound to the <video> element the webcam stream feeds. */
  videoRef: React.RefObject<HTMLVideoElement>;
}

/**
 * Live webcam proctoring overlay with a webcam/model status indicator.
 *
 * Renders the local camera preview (frames never leave the device — see
 * `useProctoring`) plus a status pill. Webcam/model errors are surfaced in an
 * `role="alert"` region so they are announced and visible (Requirement 6.8).
 */
export function ProctoringOverlay({
  status,
  escalating,
  error,
  videoRef,
}: ProctoringOverlayProps) {
  const meta = STATUS_META[status];
  const showVideo = status === "running" || status === "initializing";

  return (
    <aside
      className="flex w-full flex-col gap-2 rounded-lg border border-[#e3e8ee] bg-white p-3 shadow-sm"
      aria-label="Proctoring status"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-[#8a93a2]">
          Live Proctoring
        </span>
        <span className={`flex items-center gap-1.5 text-xs font-medium ${meta.text}`}>
          <span
            className={`inline-block h-2 w-2 rounded-full ${meta.dot}`}
            aria-hidden="true"
          />
          {meta.label}
        </span>
      </div>

      <div className="relative overflow-hidden rounded bg-navy-900">
        <video
          ref={videoRef}
          className="aspect-video w-full object-cover"
          muted
          playsInline
          // The preview is decorative; the actual screening reads pixels in JS.
          aria-hidden="true"
        />
        {escalating && (
          <span className="absolute right-2 top-2 rounded bg-crimson-600 px-2 py-0.5 text-[10px] font-semibold text-white">
            Verifying…
          </span>
        )}
        {!showVideo && (
          <div className="absolute inset-0 flex items-center justify-center p-2 text-center text-xs text-white/60">
            Camera preview unavailable
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="text-xs font-medium text-danger">
          {error}
        </p>
      )}
    </aside>
  );
}

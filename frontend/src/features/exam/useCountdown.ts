import { useEffect, useRef, useState } from "react";

/** Format a whole-second remaining count as `HH:MM:SS` (or `MM:SS` under 1h). */
export function formatRemaining(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const seconds = s % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return hours > 0
    ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
}

/**
 * A one-shot countdown timer. Ticks down `durationSeconds` once `active`
 * becomes true, firing `onExpire` exactly once when it reaches zero. The
 * `onExpire` callback is held in a ref so a changing closure (e.g. a submit
 * handler) never restarts the timer.
 */
export function useCountdown(
  durationSeconds: number,
  active: boolean,
  onExpire?: () => void,
): number {
  const [remaining, setRemaining] = useState(durationSeconds);
  const onExpireRef = useRef(onExpire);
  onExpireRef.current = onExpire;
  const firedRef = useRef(false);

  // Reset when the duration changes (a new session paper loaded).
  useEffect(() => {
    setRemaining(durationSeconds);
    firedRef.current = false;
  }, [durationSeconds]);

  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => {
      setRemaining((prev) => {
        const next = prev - 1;
        if (next <= 0) {
          clearInterval(id);
          if (!firedRef.current) {
            firedRef.current = true;
            onExpireRef.current?.();
          }
          return 0;
        }
        return next;
      });
    }, 1_000);
    return () => clearInterval(id);
  }, [active]);

  return remaining;
}

import type { ReactNode } from "react";
import { cn } from "@/components/classNames";

export interface AlertFeedProps {
  /** AlertItem children (the live alert rows). */
  children?: ReactNode;
  /** Accessible label for the feed region. */
  label?: string;
  className?: string;
}

/**
 * Container for the live alert feed.
 *
 * Exposes the feed as an `aria-live="polite"` region (Requirement 16.7) so
 * assistive technologies announce newly inserted alerts without interrupting
 * the user's current task. Wrap a list of `AlertItem`s with this component on
 * the dashboard, or replicate the `aria-live="polite"` attribute on the
 * parent container.
 */
export function AlertFeed({
  children,
  label = "Live alert feed",
  className,
}: AlertFeedProps) {
  return (
    <section
      aria-live="polite"
      aria-label={label}
      className={cn("flex flex-col gap-2", className)}
    >
      {children}
    </section>
  );
}

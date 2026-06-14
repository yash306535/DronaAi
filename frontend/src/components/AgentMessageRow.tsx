import { cn } from "@/components/classNames";
import { brand } from "@/theme";

export interface AgentMessageRowProps {
  /** Emitting agent, e.g. "Guardian". Rendered in the navy brand color. */
  source: string;
  /** Receiving agent, e.g. "Herald". Rendered in the crimson brand color. */
  target: string;
  /** The inter-agent message text. */
  text: string;
  /** ISO-8601 (or pre-formatted) timestamp, right-aligned. */
  timestamp?: string;
  className?: string;
}

/**
 * A single row in the inter-agent communication log.
 *
 * Rendered in the monospace typeface (Requirement 16.5) with
 * `Source → Target` in brand colors (navy → crimson) and a right-aligned
 * timestamp. New rows fade + slide in via the `animate-message-in` utility.
 */
export function AgentMessageRow({
  source,
  target,
  text,
  timestamp,
  className,
}: AgentMessageRowProps) {
  return (
    <div
      className={cn(
        "agent-message-row flex items-baseline gap-2 font-mono text-xs text-on-surface",
        className,
      )}
    >
      <span className="whitespace-nowrap font-semibold">
        <span style={{ color: brand.navy[400] }}>{source}</span>
        <span className="mx-1 text-on-surface-muted" aria-hidden="true">
          →
        </span>
        <span style={{ color: brand.crimson[400] }}>{target}</span>
      </span>
      <span className="min-w-0 flex-1 break-words text-on-surface-muted">
        {text}
      </span>
      {timestamp && (
        <time
          dateTime={timestamp}
          className="ml-auto whitespace-nowrap text-[10px] text-on-surface-muted"
        >
          {timestamp}
        </time>
      )}
    </div>
  );
}

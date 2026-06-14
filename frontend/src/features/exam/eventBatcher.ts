// Session-event batching for the student exam portal.
//
// Captured telemetry (tab blur/focus, paste, copy, question view, heartbeat)
// is queued locally and flushed to `POST /sessions/{id}/events` in batches of
// at most 100 events — the server's hard batch ceiling (Requirement 5.8/5.9).
// The batcher is framework-free and effect-injected (the POST function) so the
// "<=100 per batch" invariant can be unit-tested without React or a network.

import type { SessionEventBatch, SessionEventIn, SessionEventKind } from "@/types";

/** Server-enforced maximum number of events per batch (Requirement 5.8/5.9). */
export const MAX_EVENTS_PER_BATCH = 100;

/** Sends one batch to the backend; resolves on success, rejects on failure. */
export type EventPoster = (batch: SessionEventBatch) => Promise<unknown>;

/**
 * Accumulates session events and flushes them in chunks of at most
 * {@link MAX_EVENTS_PER_BATCH}. On a failed flush the un-sent events are
 * returned to the front of the queue so no telemetry is silently dropped.
 */
export class EventBatcher {
  private queue: SessionEventIn[] = [];
  private flushing = false;

  constructor(
    private readonly post: EventPoster,
    private readonly maxBatch: number = MAX_EVENTS_PER_BATCH,
  ) {}

  /** Number of events currently queued and not yet flushed. */
  get pending(): number {
    return this.queue.length;
  }

  /** Queue a single event with a client-side timestamp (treated as untrusted). */
  enqueue(kind: SessionEventKind, payload: Record<string, unknown> = {}): void {
    this.queue.push({
      kind,
      payload,
      client_ts: new Date().toISOString(),
    });
  }

  /**
   * Flush all queued events in batches of at most `maxBatch`. Each network
   * call carries between 1 and `maxBatch` events. Returns the number of events
   * successfully sent. Re-entrant calls are coalesced (a flush already in
   * flight wins) so listeners and the interval never overlap POSTs.
   */
  async flush(): Promise<number> {
    if (this.flushing) return 0;
    this.flushing = true;
    let sent = 0;
    try {
      while (this.queue.length > 0) {
        const chunk = this.queue.splice(0, this.maxBatch);
        try {
          await this.post({ events: chunk });
          sent += chunk.length;
        } catch {
          // Put the un-sent chunk back at the front and stop; retry later.
          this.queue.unshift(...chunk);
          break;
        }
      }
    } finally {
      this.flushing = false;
    }
    return sent;
  }
}

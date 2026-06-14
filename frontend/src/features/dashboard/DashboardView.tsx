import { useEffect, useMemo, useState } from "react";
import {
  AgentCard,
  AgentMessageRow,
  AlertFeed,
  AlertItem,
  SessionTile,
} from "@/components";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";
import {
  useDashboardSocket,
  type DashboardSocketOptions,
  type DashboardState,
} from "@/features/dashboard/useDashboardSocket";
import {
  buildAgentSlots,
  connectionLabel,
  connectionSeverity,
  connectionStatus,
  formatClockTime,
  toAlertView,
  toSessionView,
  truncateMessageText,
  type ConnectionStatus,
} from "@/features/dashboard/dashboardView";
import { getSeverityColors } from "@/theme";

/** A selectable exam in the top-bar exam selector. */
export interface DashboardExamOption {
  id: string;
  title: string;
}

export interface DashboardViewProps {
  /**
   * Dashboard access token. Defaults to the stored access token so the view
   * works when mounted on the `/dashboard` route after login; injectable for
   * tests and previews.
   */
  token?: string;
  /** Token store used to resolve the default token. */
  store?: TokenStore;
  /** Live-socket option overrides (injected fakes in tests). */
  socketOptions?: DashboardSocketOptions;
  /** Exams shown in the top-bar selector. */
  exams?: DashboardExamOption[];
  /**
   * Pre-resolved live state. When provided the internal socket is bypassed —
   * used by tests/storybook to render deterministic snapshots without a server.
   */
  state?: DashboardState;
}

/** Live HH:MM:SS clock for the top bar, ticking once per second. */
function useLiveClock(): string {
  const [now, setNow] = useState(() => new Date().toISOString());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date().toISOString()), 1_000);
    return () => clearInterval(id);
  }, []);
  return formatClockTime(now);
}

/** The connection status dot — color + visible text label (never color alone). */
function ConnectionDot({ status }: { status: ConnectionStatus }) {
  const colors = getSeverityColors(connectionSeverity(status));
  const label = connectionLabel(status);
  return (
    <span className="flex items-center gap-2 text-xs font-medium text-on-surface-muted">
      <span
        aria-hidden="true"
        className="inline-block h-2.5 w-2.5 rounded-full"
        style={{ backgroundColor: colors.text }}
      />
      <span>{label}</span>
    </span>
  );
}

/**
 * Live "mission control" admin dashboard (Requirements 12.3–12.6, 16.4/16.5/16.7).
 *
 * Renders under `data-theme="dashboard"` (16.4) and consumes
 * {@link useDashboardSocket} for live state. Each region folds a specific
 * WebSocket event into a shared presentational component:
 *   - Agent Status Strip — `agent.status` → {@link AgentCard} (12.4)
 *   - Inter-Agent Communication Log — `agent.message` → {@link AgentMessageRow},
 *     mono (16.5), source→target, text truncated at 2,000 chars (12.3)
 *   - Live Alert Feed — `alert.broadcast` → {@link AlertItem} inside an
 *     `aria-live="polite"` {@link AlertFeed} (12.5, 16.7), severity-colored
 *   - Session Grid — `session.update` → {@link SessionTile} integrity ring +
 *     status (12.6)
 *
 * All folding latency (≤2s) is inherent: state updates synchronously as
 * envelopes arrive via the socket and React re-renders immediately.
 */
export function DashboardView({
  token,
  store = tokenStore,
  socketOptions,
  exams = [],
  state,
}: DashboardViewProps) {
  const resolvedToken = token ?? store.getAccessToken() ?? "";

  // When an explicit `state` is provided we bypass the socket entirely. The
  // hook is still called unconditionally (Rules of Hooks); its result is just
  // ignored in that mode.
  const live = useDashboardSocket(resolvedToken, socketOptions);
  const dash = state ?? live;

  const [examId, setExamId] = useState<string>(exams[0]?.id ?? "");
  const clock = useLiveClock();

  const status = connectionStatus(dash.connected, dash.degraded);
  const agentSlots = useMemo(() => buildAgentSlots(dash.agents), [dash.agents]);
  const alertViews = useMemo(() => dash.alerts.map(toAlertView), [dash.alerts]);
  const sessionViews = useMemo(
    () => Object.values(dash.sessions).map(toSessionView),
    [dash.sessions],
  );

  return (
    <div
      data-theme="dashboard"
      className="min-h-screen bg-surface-0 text-on-surface"
    >
      {/* Top bar: wordmark · exam selector · live clock · connection dot */}
      <header className="flex flex-wrap items-center gap-4 border-b border-hairline bg-surface-1 px-6 py-3">
        <span className="text-lg font-bold tracking-wider">DRONA AI</span>

        <label className="flex items-center gap-2 text-xs text-on-surface-muted">
          <span className="sr-only">Exam</span>
          <select
            value={examId}
            onChange={(e) => setExamId(e.target.value)}
            aria-label="Select exam"
            className="focus-ring rounded-md border border-hairline bg-surface-2 px-2 py-1 text-xs text-on-surface"
          >
            {exams.length === 0 ? (
              <option value="">All exams</option>
            ) : (
              exams.map((exam) => (
                <option key={exam.id} value={exam.id}>
                  {exam.title}
                </option>
              ))
            )}
          </select>
        </label>

        <div className="ml-auto flex items-center gap-4">
          <time
            className="font-mono text-sm tabular-nums text-on-surface-muted"
            aria-label="Current time"
          >
            {clock}
          </time>
          <ConnectionDot status={status} />
        </div>
      </header>

      <main className="flex flex-col gap-6 p-6">
        {/* Agent Status Strip (12.4) */}
        <section aria-label="Agent status">
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
            Agents
          </h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {agentSlots.map((slot) => (
              <AgentCard
                key={slot.name}
                name={slot.name}
                role={slot.role}
                state={slot.state}
                load={slot.load}
              />
            ))}
          </div>
        </section>

        <div className="grid gap-6 lg:grid-cols-[1fr_1fr]">
          {/* Session Grid (12.6) */}
          <section aria-label="Session grid">
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
              Sessions
            </h2>
            {sessionViews.length === 0 ? (
              <p className="text-sm text-on-surface-muted">
                No active sessions.
              </p>
            ) : (
              <div className="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-5">
                {sessionViews.map((session) => (
                  <SessionTile
                    key={session.sessionId}
                    name={session.name}
                    integrityScore={session.integrityPercent}
                    status={session.status}
                  />
                ))}
              </div>
            )}
          </section>

          {/* Inter-Agent Communication Log (12.3, 16.5) */}
          <section aria-label="Inter-agent communication log">
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
              Inter-Agent Communication
            </h2>
            <div className="flex max-h-96 flex-col gap-1.5 overflow-y-auto rounded-md border border-hairline bg-surface-1 p-3">
              {dash.messages.length === 0 ? (
                <p className="font-mono text-xs text-on-surface-muted">
                  Waiting for agent activity…
                </p>
              ) : (
                dash.messages.map((message) => (
                  <AgentMessageRow
                    key={message.id}
                    source={message.source}
                    target={message.to}
                    text={truncateMessageText(message.text)}
                    timestamp={formatClockTime(message.ts)}
                  />
                ))
              )}
            </div>
          </section>
        </div>

        {/* Live Alert Feed (12.5, 16.7) */}
        <section aria-label="Alerts">
          <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
            Live Alerts
          </h2>
          {alertViews.length === 0 ? (
            <p className="text-sm text-on-surface-muted">No alerts yet.</p>
          ) : (
            <AlertFeed>
              {alertViews.map((alert) => (
                <AlertItem
                  key={alert.id}
                  severity={alert.severity}
                  title={alert.title}
                  reasons={alert.reasons}
                  timestamp={alert.timestamp}
                />
              ))}
            </AlertFeed>
          )}
        </section>
      </main>
    </div>
  );
}

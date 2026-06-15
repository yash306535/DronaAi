import { useEffect, useMemo, useState } from "react";
import {
  AgentCard,
  AgentMessageRow,
  AlertFeed,
  AlertItem,
  SessionTile,
} from "@/components";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";
import { apiClient, type ApiClient } from "@/lib/apiClient";
import {
  BellRing,
  Bot,
  Clock,
  MessagesSquare,
  Radio,
  Users,
} from "lucide-react";
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
  restAlertToView,
  restSessionToView,
  toAlertView,
  toSessionView,
  truncateMessageText,
  type AlertView,
  type ConnectionStatus,
  type SessionView,
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
  /**
   * Exams shown in the top-bar selector. When omitted, the view fetches the
   * caller's visible exams from the API on mount.
   */
  exams?: DashboardExamOption[];
  /** API client used to fetch exams when `exams` is not supplied. */
  api?: Pick<ApiClient, "listExams" | "listExamSessions" | "listExamAlerts">;
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
  exams,
  api = apiClient,
  state,
}: DashboardViewProps) {
  const resolvedToken = token ?? store.getAccessToken() ?? "";

  // When an explicit `state` is provided we bypass the socket entirely. The
  // hook is still called unconditionally (Rules of Hooks); its result is just
  // ignored in that mode.
  const live = useDashboardSocket(resolvedToken, socketOptions);
  const dash = state ?? live;

  // Exams for the top-bar selector. If the caller passed an explicit list we
  // use it; otherwise fetch the caller's visible exams once on mount.
  const [fetchedExams, setFetchedExams] = useState<DashboardExamOption[]>([]);
  useEffect(() => {
    if (exams !== undefined) {
      return;
    }
    let cancelled = false;
    api
      .listExams()
      .then((list) => {
        if (!cancelled) {
          setFetchedExams(
            list.map((exam) => ({ id: exam.id, title: exam.title })),
          );
        }
      })
      .catch(() => {
        // Leave the selector empty on failure; the dashboard still renders.
      });
    return () => {
      cancelled = true;
    };
  }, [exams, api]);
  const examOptions = exams ?? fetchedExams;

  const [examId, setExamId] = useState<string>("");
  // Default the selection to the first exam once options are available.
  useEffect(() => {
    if (!examId && examOptions.length > 0) {
      setExamId(examOptions[0].id);
    }
  }, [examOptions, examId]);
  const clock = useLiveClock();

  // Seeded/historical data for the selected exam, loaded via REST so the
  // dashboard is populated immediately (the live socket only streams new
  // events). Live state is overlaid on top of this baseline.
  const [restSessions, setRestSessions] = useState<SessionView[]>([]);
  const [restAlerts, setRestAlerts] = useState<AlertView[]>([]);
  useEffect(() => {
    if (state !== undefined || !examId) return;
    let cancelled = false;
    if (api.listExamSessions) {
      api
        .listExamSessions(examId)
        .then((rows) => {
          if (!cancelled) setRestSessions(rows.map(restSessionToView));
        })
        .catch(() => undefined);
    }
    if (api.listExamAlerts) {
      api
        .listExamAlerts(examId)
        .then((rows) => {
          if (!cancelled) setRestAlerts(rows.map(restAlertToView));
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [examId, api, state]);

  const status = connectionStatus(dash.connected, dash.degraded);
  const agentSlots = useMemo(() => buildAgentSlots(dash.agents), [dash.agents]);

  // Merge live + seeded alerts (live first), de-duplicated by id.
  const alertViews = useMemo(() => {
    const live = dash.alerts.map(toAlertView);
    const seen = new Set(live.map((a) => a.id));
    return [...live, ...restAlerts.filter((a) => !seen.has(a.id))];
  }, [dash.alerts, restAlerts]);

  // Merge live + seeded sessions; a live update overrides the seeded baseline.
  const sessionViews = useMemo(() => {
    const merged = new Map<string, SessionView>();
    for (const s of restSessions) merged.set(s.sessionId, s);
    for (const s of Object.values(dash.sessions).map(toSessionView)) {
      merged.set(s.sessionId, s);
    }
    return Array.from(merged.values());
  }, [dash.sessions, restSessions]);

  return (
    <div className="flex flex-col gap-6 text-on-surface">
      {/* Toolbar: exam selector · live clock · connection dot */}
      <header className="flex flex-wrap items-center gap-4 rounded-lg border border-[#e3e8ee] bg-white px-4 py-3 shadow-sm">
        <span className="flex items-center gap-2 text-sm font-semibold text-navy-900">
          <Radio className="h-4 w-4 text-crimson-600" aria-hidden="true" />
          Mission Control
        </span>

        <label className="flex items-center gap-2 text-xs text-[#5a6270]">
          <span className="sr-only">Exam</span>
          <select
            value={examId}
            onChange={(e) => setExamId(e.target.value)}
            aria-label="Select exam"
            className="focus-ring rounded-md border border-[#cfd6e0] bg-white px-2 py-1.5 text-xs text-[#1a1d24]"
          >
            {examOptions.length === 0 ? (
              <option value="">All exams</option>
            ) : (
              examOptions.map((exam) => (
                <option key={exam.id} value={exam.id}>
                  {exam.title}
                </option>
              ))
            )}
          </select>
        </label>

        <div className="ml-auto flex items-center gap-4">
          <time
            className="flex items-center gap-1.5 font-mono text-sm tabular-nums text-[#5a6270]"
            aria-label="Current time"
          >
            <Clock className="h-4 w-4" aria-hidden="true" />
            {clock}
          </time>
          <ConnectionDot status={status} />
        </div>
      </header>

      <div className="flex flex-col gap-6">
        {/* Agent Status Strip (12.4) */}
        <section aria-label="Agent status">
          <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
            <Bot className="h-4 w-4" aria-hidden="true" />
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
            <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
              <Users className="h-4 w-4" aria-hidden="true" />
              Sessions
            </h2>
            {sessionViews.length === 0 ? (
              <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-6 text-center text-sm text-[#8a93a2]">
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
            <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
              <MessagesSquare className="h-4 w-4" aria-hidden="true" />
              Inter-Agent Communication
            </h2>
            <div className="flex max-h-96 flex-col gap-1.5 overflow-y-auto rounded-lg border border-[#e3e8ee] bg-white p-3 shadow-sm">
              {dash.messages.length === 0 ? (
                <p className="font-mono text-xs text-[#8a93a2]">
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
          <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
            <BellRing className="h-4 w-4" aria-hidden="true" />
            Live Alerts
          </h2>
          {alertViews.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-6 text-center text-sm text-[#8a93a2]">
              No alerts yet.
            </p>
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
      </div>
    </div>
  );
}

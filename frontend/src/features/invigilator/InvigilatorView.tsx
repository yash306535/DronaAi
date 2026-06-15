import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BellRing,
  Eye,
  MonitorCheck,
  Users,
  X,
} from "lucide-react";
import {
  AlertFeed,
  AlertItem,
  Button,
  SessionTile,
  StatPill,
} from "@/components";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";
import { apiClient } from "@/lib/apiClient";
import {
  restAlertToView,
  restSessionToView,
  toAlertView,
  toSessionView,
} from "@/features/dashboard";
import {
  fetchSessionAnomalies,
  terminateSession,
  type InvigilatorApi,
} from "@/features/invigilator/api";
import {
  useInvigilatorSocket,
  type InvigilatorSocketOptions,
  type InvigilatorState,
} from "@/features/invigilator/useInvigilatorSocket";
import type { Anomaly, SessionStatus } from "@/types";

/** A selectable exam in the exam selector. */
export interface InvigilatorExamOption {
  id: string;
  title: string;
}

export interface InvigilatorViewProps {
  /**
   * Invigilator access token. Defaults to the stored access token so the view
   * works when mounted on the `/invigilator` route after login; injectable for
   * tests and previews.
   */
  token?: string;
  /** Token store used to resolve the default token. */
  store?: TokenStore;
  /** Live-socket option overrides (injected fake socket in tests). */
  socketOptions?: InvigilatorSocketOptions;
  /** REST client for anomaly fetch + terminate (mocked in tests). */
  api?: InvigilatorApi;
  /** Exams shown in the selector. */
  exams?: InvigilatorExamOption[];
  /** Pre-selected exam id (skips the picker; used by tests/previews). */
  initialExamId?: string;
  /**
   * Pre-resolved live state. When provided the internal socket is bypassed —
   * used by tests/storybook to render deterministic snapshots without a server.
   */
  state?: InvigilatorState;
}

/**
 * Invigilator console (task 18, Requirements 5.10, 12.1).
 *
 * The operator picks (or is given) an exam id, which binds a live
 * {@link useInvigilatorSocket} connection to `/ws/invigilator/{exam_id}`. The
 * console folds that exam-scoped feed into two regions:
 *   - a Session Grid of {@link SessionTile}s reflecting live `session.update`
 *     integrity/status changes
 *   - a Live Alert Feed of {@link AlertItem}s driven by `alert.broadcast`
 *
 * Selecting a session opens a detail panel that fetches the session's anomalies
 * (`GET /sessions/{id}/anomalies`) and offers a Terminate action
 * (`POST /sessions/{id}/terminate`, Req 5.10).
 *
 * Hooks must run unconditionally, so the live console is always rendered once an
 * exam id is chosen; the picker simply gates which exam id is active.
 */
export function InvigilatorView({
  token,
  store = tokenStore,
  socketOptions,
  api,
  exams,
  initialExamId,
  state,
}: InvigilatorViewProps) {
  const resolvedToken = token ?? store.getAccessToken() ?? "";

  // Populate the exam selector from the API when an explicit list isn't given.
  const [fetchedExams, setFetchedExams] = useState<InvigilatorExamOption[]>([]);
  useEffect(() => {
    if (exams !== undefined) return;
    let cancelled = false;
    apiClient
      .listExams()
      .then((list) => {
        if (!cancelled) {
          setFetchedExams(list.map((e) => ({ id: e.id, title: e.title })));
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [exams]);
  const examOptions = exams ?? fetchedExams;

  const [examId, setExamId] = useState<string>(initialExamId ?? "");
  const [draftExamId, setDraftExamId] = useState<string>(examId);

  return (
    <div className="flex flex-col gap-6 text-on-surface">
      <header className="flex flex-wrap items-center gap-4 rounded-lg border border-[#e3e8ee] bg-white px-4 py-3 shadow-sm">
        <span className="flex items-center gap-2 text-sm font-semibold text-navy-900">
          <MonitorCheck className="h-4 w-4 text-crimson-600" aria-hidden="true" />
          Invigilator Console
        </span>

        <form
          className="ml-auto flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setExamId(draftExamId.trim());
          }}
        >
          <label className="flex items-center gap-2 text-xs text-[#5a6270]">
            <span className="sr-only">Exam</span>
            {examOptions.length > 0 ? (
              <select
                value={draftExamId}
                onChange={(e) => setDraftExamId(e.target.value)}
                aria-label="Select exam"
                className="focus-ring rounded-md border border-[#cfd6e0] bg-white px-2 py-1.5 text-xs text-[#1a1d24]"
              >
                <option value="">Choose an exam…</option>
                {examOptions.map((exam) => (
                  <option key={exam.id} value={exam.id}>
                    {exam.title}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={draftExamId}
                onChange={(e) => setDraftExamId(e.target.value)}
                placeholder="Exam id"
                aria-label="Exam id"
                className="focus-ring rounded-md border border-[#cfd6e0] bg-white px-2 py-1.5 text-xs text-[#1a1d24]"
              />
            )}
          </label>
          <Button type="submit" disabled={draftExamId.trim().length === 0}>
            <Eye className="h-4 w-4" aria-hidden="true" />
            Monitor
          </Button>
        </form>
      </header>

      {examId.length === 0 ? (
        <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-8 text-center text-sm text-[#8a93a2]">
          Choose an exam to begin monitoring its sessions.
        </p>
      ) : (
        <InvigilatorConsole
          key={examId}
          examId={examId}
          token={resolvedToken}
          socketOptions={socketOptions}
          api={api}
          state={state}
        />
      )}
    </div>
  );
}

interface InvigilatorConsoleProps {
  examId: string;
  token: string;
  socketOptions?: InvigilatorSocketOptions;
  api?: InvigilatorApi;
  state?: InvigilatorState;
}

/**
 * The live, exam-scoped console body. Split out so the live socket hook is only
 * mounted once a concrete exam id is selected, and so it remounts cleanly when
 * the exam changes (via `key` on the parent).
 */
function InvigilatorConsole({
  examId,
  token,
  socketOptions,
  api,
  state,
}: InvigilatorConsoleProps) {
  const live = useInvigilatorSocket(examId, token, socketOptions);
  const console_ = state ?? live;

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(
    null,
  );

  // Seeded/historical sessions + alerts for this exam, loaded via REST so the
  // console is populated immediately (the socket only streams new events).
  const [restSessions, setRestSessions] = useState<
    ReturnType<typeof restSessionToView>[]
  >([]);
  const [restAlerts, setRestAlerts] = useState<
    ReturnType<typeof restAlertToView>[]
  >([]);
  useEffect(() => {
    if (state !== undefined || !examId) return;
    let cancelled = false;
    apiClient
      .listExamSessions(examId)
      .then((rows) => {
        if (!cancelled) setRestSessions(rows.map(restSessionToView));
      })
      .catch(() => undefined);
    apiClient
      .listExamAlerts(examId)
      .then((rows) => {
        if (!cancelled) setRestAlerts(rows.map(restAlertToView));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [examId, state]);

  const sessionViews = useMemo(() => {
    const merged = new Map<string, ReturnType<typeof toSessionView>>();
    for (const s of restSessions) merged.set(s.sessionId, s);
    for (const s of Object.values(console_.sessions).map(toSessionView)) {
      merged.set(s.sessionId, s);
    }
    return Array.from(merged.values());
  }, [console_.sessions, restSessions]);
  const alertViews = useMemo(() => {
    const live = console_.alerts.map(toAlertView);
    const seen = new Set(live.map((a) => a.id));
    return [...live, ...restAlerts.filter((a) => !seen.has(a.id))];
  }, [console_.alerts, restAlerts]);

  return (
    <main className="grid gap-6 lg:grid-cols-[1fr_minmax(20rem,24rem)]">
      <div className="flex flex-col gap-6">
        {/* Session Grid (Req 12.1, live session.update) */}
        <section aria-label="Monitored sessions">
          <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
            <Users className="h-4 w-4" aria-hidden="true" />
            Sessions
          </h2>
          {sessionViews.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-6 text-center text-sm text-[#8a93a2]">
              No sessions reported yet for this exam.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-5">
              {sessionViews.map((session) => (
                <button
                  key={session.sessionId}
                  type="button"
                  onClick={() => setSelectedSessionId(session.sessionId)}
                  aria-pressed={selectedSessionId === session.sessionId}
                  aria-label={`Inspect session ${session.name}`}
                  className="focus-ring rounded-md text-left"
                >
                  <SessionTile
                    name={session.name}
                    integrityScore={session.integrityPercent}
                    status={session.status}
                  />
                </button>
              ))}
            </div>
          )}
        </section>

        {/* Live Alert Feed (Req 12.1, live alert.broadcast) */}
        <section aria-label="Alerts">
          <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[#5a6270]">
            <BellRing className="h-4 w-4" aria-hidden="true" />
            Live Alerts
          </h2>
          {alertViews.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-6 text-center text-sm text-[#8a93a2]">No alerts yet.</p>
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

      {/* Session detail panel: anomalies + terminate action */}
      <aside aria-label="Session detail">
        {selectedSessionId === null ? (
          <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-6 text-center text-sm text-[#8a93a2]">
            Select a session to view its anomalies.
          </p>
        ) : (
          <SessionDetailPanel
            sessionId={selectedSessionId}
            status={console_.sessions[selectedSessionId]?.status}
            api={api}
            onClose={() => setSelectedSessionId(null)}
          />
        )}
      </aside>
    </main>
  );
}

interface SessionDetailPanelProps {
  sessionId: string;
  status?: string;
  api?: InvigilatorApi;
  onClose: () => void;
}

/**
 * Detail panel for one session: lists the session's anomalies (fetched on
 * select / sessionId change) and offers a Terminate action (Req 5.10). When
 * termination succeeds the panel reflects the returned `terminated` status.
 */
function SessionDetailPanel({
  sessionId,
  status,
  api,
  onClose,
}: SessionDetailPanelProps) {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [terminating, setTerminating] = useState(false);
  const [liveStatus, setLiveStatus] = useState<string | undefined>(status);

  const loadAnomalies = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchSessionAnomalies(sessionId, api)
      .then((rows) => {
        if (!cancelled) setAnomalies(rows);
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load anomalies.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, api]);

  useEffect(() => {
    setLiveStatus(status);
  }, [status, sessionId]);

  useEffect(() => loadAnomalies(), [loadAnomalies]);

  const handleTerminate = useCallback(() => {
    setTerminating(true);
    setError(null);
    terminateSession(sessionId, api)
      .then((session) => {
        setLiveStatus(session.status);
      })
      .catch(() => {
        setError("Failed to terminate session.");
      })
      .finally(() => {
        setTerminating(false);
      });
  }, [sessionId, api]);

  const isTerminated = liveStatus === "terminated";

  return (
    <div className="flex flex-col gap-4 rounded-md border border-hairline bg-surface-1 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">Session detail</h2>
          <p className="font-mono text-xs text-on-surface-muted">{sessionId}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close session detail"
          className="focus-ring rounded p-1 text-[#8a93a2] hover:text-[#1a1d24]"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      {liveStatus && (
        <StatPill severity={isTerminated ? "danger" : "info"}>
          {STATUS_LABEL[liveStatus as SessionStatus] ?? liveStatus}
        </StatPill>
      )}

      <section aria-label="Session anomalies">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-on-surface-muted">
          Anomalies
        </h3>
        {loading ? (
          <p className="text-sm text-on-surface-muted">Loading anomalies…</p>
        ) : anomalies.length === 0 ? (
          <p className="text-sm text-on-surface-muted">
            No anomalies recorded.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {anomalies.map((anomaly) => (
              <li
                key={anomaly.id}
                className="rounded-md bg-surface-2 p-2 text-sm"
                data-anomaly-id={anomaly.id}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{anomaly.category}</span>
                  <span className="font-mono text-xs text-on-surface-muted">
                    {anomaly.score.toFixed(2)}
                  </span>
                </div>
                {anomaly.reasons.length > 0 && (
                  <ul className="mt-1 space-y-0.5 font-mono text-xs text-on-surface-muted">
                    {anomaly.reasons.map((reason, i) => (
                      <li key={i}>• {reason}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {error && (
        <p role="alert" className="text-xs text-crimson-400">
          {error}
        </p>
      )}

      <Button
        variant="destructive"
        onClick={handleTerminate}
        disabled={terminating || isTerminated}
      >
        {isTerminated
          ? "Session terminated"
          : terminating
            ? "Terminating…"
            : "Terminate session"}
      </Button>
    </div>
  );
}

const STATUS_LABEL: Record<SessionStatus, string> = {
  not_started: "Not started",
  active: "Active",
  submitted: "Submitted",
  terminated: "Terminated",
};

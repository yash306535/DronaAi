# Implementation Plan: DRONA AI

## Overview

This plan converts the DRONA AI design into incremental coding tasks ordered by the design's P1/P2/P3 build phasing. The goal is a system that is **demoable as early as possible**: P1 delivers the orchestration backbone, the two-stage proctoring pipeline (Guardian Stage 1 + Stage 2), the Architect, the student portal, the live admin dashboard, and Herald broadcasting. P2 adds Sentinel, the inter-agent communication visualization, and the Analyst. P3 adds the Auditor and PDF reports.

Stack: Python 3.11 + FastAPI backend (SQLAlchemy, CrewAI, async WebSockets), React 18 + Vite + TypeScript + Tailwind frontend. Property-based tests use `hypothesis` (Python) and `fast-check` (TypeScript). Tasks build strictly on prior tasks and end by wiring everything together; the team can stop after P1 and still have a working, demoable system.

**Phase tags:** Tasks/sub-tasks tagged `[P2]` or `[P3]` belong to later phases. Untagged tasks are P1. Sub-tasks marked with `*` are optional (tests) and are not auto-implemented.

## Tasks

- [x] 1. Scaffold backend project structure and core configuration
  - Create `backend/app` package layout (`core`, `api`, `agents`, `services`, `repositories`, `models`, `schemas`) per the design Project Structure
  - Add `requirements.txt` with fastapi, uvicorn[standard], sqlalchemy, alembic, pydantic, pydantic-settings, python-jose[cryptography], passlib[bcrypt], httpx, pytest, hypothesis, pytest-asyncio
  - Implement `app/core/config.py` Settings (env-driven secrets via `SecretStr`), failing startup if a required secret is missing (by key name only)
  - Add `.env.example` documenting keys without values
  - _Requirements: 15.1, 15.2_

  - [x]* 1.1 Write unit tests for config/secret loading
    - Test that absent required secret aborts startup with a key-name-only error and that secret values are never echoed
    - _Requirements: 15.1, 15.2_

- [x] 2. Implement core cross-cutting infrastructure (errors, logging, security)
  - [x] 2.1 Implement structured logging and centralized error handling
    - Add `app/core/logging.py` (structured JSON logs with request/correlation id)
    - Add `app/core/errors.py`: `AppError` hierarchy (`AuthError`, `NotFoundError`, `ValidationError`, `UpstreamError`) + global FastAPI handlers mapping to the standard error envelope `{error:{code,message,requestId}}`, never leaking stack traces/secrets/raw DB text
    - _Requirements: 15.3, 15.4_

  - [x] 2.2 Implement JWT security utilities
    - Add `app/core/security.py`: bcrypt password hashing, JWT access (15 min) + refresh (7 day) encode/decode, refresh-token rotation/invalidation support
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x]* 2.3 Write unit tests for security utilities
    - Test token TTLs, rotation invalidates old refresh token, expired/malformed tokens rejected, passwords stored only as bcrypt hashes
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7_

- [x] 3. Implement the in-process async event bus
  - Implement `app/core/events.py`: `EventType` enum, `Event` dataclass (with `id`, `ts`, `source`, `session_id`), `EventBus.subscribe/publish` with concurrent fan-out, registration-order invocation, per-handler exception capture (log event id + handler id, never re-raise), no-op on no subscribers
  - Add event-id dedup registry so a replayed `event.id` is discarded
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.6, 11.7_

  - [x]* 3.1 Write property test for event delivery idempotency
    - **Property 7: Event delivery idempotency** — replaying the same `event.id` creates no duplicate anomaly/alert
    - **Validates: Requirements 11.6**

  - [x]* 3.2 Write unit tests for event bus delivery semantics
    - Test registration-order delivery, one failing handler does not block others, no-subscriber publish is a safe no-op, delivery within timing bound
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.7_

- [x] 4. Implement database models, session, and repository layer
  - [x] 4.1 Define SQLAlchemy ORM models and Pydantic schema mirrors
    - Implement models in `app/models`: User, Exam, Question, GeneratedPaper, ExamSession, SessionEvent, Anomaly, Alert, Answer, ExamAnalytics with indices on `session_id`, `exam_id`, `(exam_id, student_id)`
    - Mirror request/response schemas in `app/schemas` with validation rules (email RFC + unique, password 8–128 chars, blueprint ≥1 topic & total count ≥1, MCQ options ≥2 with exactly one correct, anomaly score / severity ranges)
    - Add DB session/engine setup (SQLite local) in `app/core` or `app/db`
    - _Requirements: 3.1, 3.2, 3.3, 4.4, 14.6, 14.7, 14.8_

  - [x] 4.2 Implement repository layer with transient-fault retry
    - Add `app/repositories` for each aggregate; all access via parameterized/bound queries (no string SQL)
    - Implement retry-once (≤500ms) on transient faults (connection failure/timeout/deadlock), else surface for 503 with request id, leaving data unchanged
    - _Requirements: 15.7, 15.10_

  - [x]* 4.3 Write unit tests for repositories and validation rules
    - Test blueprint/topic/question-count/title range validations, MCQ option constraints, and transient-fault retry-once behavior
    - _Requirements: 3.2, 3.3, 3.4, 4.4, 15.10_

- [x] 5. Implement authentication endpoints and RBAC guard
  - [x] 5.1 Implement Auth_Service endpoints
    - Add `app/api/auth.py`: `/auth/login`, `/auth/refresh`, `/auth/me`; 401 on bad credentials with non-disclosing error code; 400 on password length violation; refresh rotation invalidates prior token
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9_

  - [x] 5.2 Implement RBAC dependencies and ownership checks
    - Add `app/api/deps.py`: `require_role(*roles)` returning 401 (missing/expired) before logic, 403 (wrong role) before logic; three roles only (Admin, Invigilator, Student); Student own-resource ownership enforcement
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 15.8, 15.9_

  - [x]* 5.3 Write property test for RBAC enforcement
    - **Property 6: RBAC enforcement** — for all protected endpoints a request without the required role is rejected with 403 before any business logic runs
    - **Validates: Requirements 2.2, 15.9**

  - [x]* 5.4 Write unit tests for auth flows
    - Test login/refresh/me happy + failure paths, expired/malformed token rejection, password-length 400, non-disclosing 401
    - _Requirements: 1.1, 1.2, 1.5, 1.6, 1.8, 1.9, 2.1_

- [x] 6. Implement WebSocket manager and authenticated WS endpoints
  - [x] 6.1 Implement WebSocket connection manager
    - Add `app/core/ws.py`: room registry (`dashboard`, `invigilator:{exam_id}`, `session:{session_id}`), connect/disconnect/broadcast/send_personal, heartbeat ping every 30s with pong-within-10s, prune after 3 missed pings (within 5s), prune-on-delivery-failure and continue, room-scoped delivery only
    - _Requirements: 12A.1, 12A.2, 12A.3, 12A.4, 12A.5, 12A.7_

  - [x] 6.2 Implement authenticated WS routes
    - Add `app/api/ws_routes.py`: `/ws/dashboard`, `/ws/invigilator/{exam_id}`, `/ws/session/{session_id}`; validate `?token=` JWT and role before binding; close on missing/invalid token or unauthorized/unknown room
    - Define shared `WSMessage` envelope schema in `app/schemas`
    - _Requirements: 2.6, 2.7, 2.8, 12.1, 12.2, 12A.6_

  - [x]* 6.3 Write unit tests for WS auth and pruning
    - Test reject on bad token/role/unknown room before binding, heartbeat pruning, room-scoped delivery isolation
    - _Requirements: 2.6, 2.7, 2.8, 12A.4, 12A.5, 12A.6_

- [x] 7. Implement app factory, middleware, and orchestrator wiring
  - [x] 7.1 Implement FastAPI app factory and security middleware
    - Add `app/main.py`: app factory, lifespan (build event bus, wire orchestrator, prune task), CORS locked to configured frontend allowlist (deny non-allowlisted origins), 1 MB body-size limit → 422, security headers
    - Add rate limiting on `/auth/login` and `/proctoring/escalate` (5/IP and 5/session per rolling 60s → 429)
    - _Requirements: 15.3, 15.5, 15.6, 15.8_

  - [x] 7.2 Implement Orchestrator skeleton and event-bus wiring
    - Add `app/agents/orchestrator.py`: `build_crew`, `wire_event_bus` (subscribe each agent's handlers before any publish), `provision_exam`; CrewAI crew assembly stub
    - Publish `agent.message` to dashboard feed whenever an agent emits inter-agent communication
    - _Requirements: 11.1, 11.5_

  - [x]* 7.3 Write integration test for startup wiring and hardening
    - Test startup subscribes handlers before publish, CORS allowlist enforcement, 1MB body rejection, login rate-limit 429
    - _Requirements: 11.1, 15.3, 15.5, 15.6_

- [x] 8. Checkpoint - backend foundation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement Architect agent and exam provisioning
  - [x] 9.1 Implement exam creation and provisioning endpoints
    - Add `app/api/exams.py` + service: create exam (status `draft`, title 1–200 chars, blueprint validation), list exams by role audience, `/exams/{id}/provision` (draft→provisioning, dispatch per enrolled student, reject non-draft / zero-enrolled), `/exams/{id}/papers/status`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9_

  - [x] 9.2 Implement Architect agent paper generation
    - Add `app/agents/architect.py` + LLM client abstraction (Claude/OpenAI behind an interface) and versioned prompt template; build per-student seed (`hash(exam_id+student_id+nonce)`), generate exactly blueprint total count, enforce topic/difficulty distribution match, MCQ 2–maxOptions with one correct, schema-validate with retry (default 3) then abort + generation-failure event
    - Persist paper (answer keys server-side only), emit `paper.generated`; on persistence failure emit generation-failure and do not emit `paper.generated`
    - Wire Architect to `exam.provision` event; run LLM provisioning as background task
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_

  - [x]* 9.3 Write property test for paper uniqueness and distribution
    - **Property 4: Paper uniqueness** — for all pairs of papers in an exam, similarity < `uniqueness_ceiling` while topic/difficulty distribution matches the blueprint
    - **Validates: Requirements 4.2, 4.3**

  - [x]* 9.4 Write unit tests for Architect parsing and failure handling
    - Test schema-validation retry/abort, generation-failure event on exhausted retries and on persistence failure, MCQ option constraints (mocked LLM)
    - _Requirements: 4.5, 4.6, 4.8, 4.4_

- [x] 10. Implement student session lifecycle and answer-key confidentiality
  - [x] 10.1 Implement session endpoints and answer handling
    - Add `app/api/sessions.py` + service: `/sessions/{exam_id}/start` (create `active`, return own paper ≤3s, reject if existing active/submitted), `/sessions/{id}` (ownership-scoped), `/sessions/{id}/answers` (persist with server-recorded whole-second time spent; reject if not active), `/sessions/{id}/submit` (active→submitted, emit exactly one `exam.completed`), `/sessions/{id}/terminate` (invigilator/admin → terminated, reject later answers)
    - _Requirements: 5.1, 5.2, 5.4, 5.5, 5.6, 5.7, 5.10_

  - [x] 10.2 Implement answer-key-safe serialization layer
    - Add a student-facing serializer that strips all answer-key fields from payloads/headers/errors; pre-transmission guard blocks responses containing answer-key fields (serialization-integrity error), excluding the field from logs
    - _Requirements: 4.9, 5.3, 14.1, 14.2_

  - [x] 10.3 Implement session-event ingestion with authoritative timestamps
    - Add `/sessions/{id}/events`: accept batches of 1–100 events, reject >100 (persist none), record authoritative UTC server timestamp (ms precision), assign server timestamp when absent; treat client timestamp as untrusted; emit `session.event`
    - _Requirements: 5.8, 5.9, 14.3, 14.4_

  - [x]* 10.4 Write property test for answer-key confidentiality
    - **Property 5: Answer-key confidentiality** — for all student-facing responses, no `answer_key` field is ever serialized
    - **Validates: Requirements 5.3, 14.1, 14.2**

  - [x]* 10.5 Write unit tests for session lifecycle and event batching
    - Test duplicate-session rejection, answer rejection on non-active session, terminate blocks later answers, >100-event batch rejection, server-timestamp authority
    - _Requirements: 5.2, 5.5, 5.8, 5.9, 5.10, 14.3_

- [x] 11. Implement integrity score management
  - Implement integrity-score update logic: non-increasing as confirmed anomalies accumulate, clamped to [0,100]; anomaly score clamped to [0.0,1.0]; reject invalid alert severity (leave prior unchanged)
  - Emit `session.update` on integrity-score/status change
  - _Requirements: 14.5, 14.6, 14.7, 14.8_

  - [x]* 11.1 Write property test for integrity score monotonicity
    - **Property 8: Integrity score monotonicity** — for all sessions, `integrity_score` is non-increasing as confirmed anomalies accumulate and stays within [0,100]
    - **Validates: Requirements 14.5, 14.6**

- [x] 12. Implement Guardian Stage 2 confirmation and escalation endpoint
  - [x] 12.1 Implement Guardian agent Stage 2 vision confirmation
    - Add `app/agents/guardian.py`: `confirm_escalation` (validate frame, single OpenAI Vision call with structured `VisionVerdict` schema, ≤10s timeout), `on_frame_escalated` handler; confirm anomaly when anomalous + confidence ≥0.70 and emit `anomaly.detected` with `confirmed=true`; on benign record false positive and raise local threshold by 0.05 (cap 0.95); on Vision unavailable/timeout record unconfirmed `warning` and leave threshold unchanged; discard raw frame ≤60s; emit `agent.message`
    - _Requirements: 7.1, 7.4, 7.5, 7.6, 7.8_

  - [x] 12.2 Implement escalation endpoint
    - Add `app/api/proctoring.py`: `POST /proctoring/{session_id}/escalate` (Student JWT bound to active session; reject non-active/non-owned with auth error and no Vision call; reject payload >5MB or non-jpeg/png MIME with 422 and no Vision call); invoke Vision only on Stage-1-triggered escalation
    - _Requirements: 7.2, 7.3, 7.7_

  - [x]* 12.3 Write property test for no-false-alert-without-confirmation
    - **Property 2: No false alerts without confirmation** — for all guardian-sourced anomalies, broadcast occurs only when `confirmed == true`
    - **Validates: Requirements 7.4, 9.2, 9.3**

  - [x]* 12.4 Write unit tests for escalation guards and benign handling
    - Test oversized/bad-MIME rejection (no Vision call), non-owned/non-active rejection, benign threshold raise (cap 0.95), Vision-timeout unconfirmed warning, frame discard
    - _Requirements: 7.2, 7.3, 7.5, 7.6, 7.7, 7.8_

- [x] 13. Implement Herald agent real-time broadcasting
  - Add `app/agents/herald.py`: `on_anomaly_detected` persists alert with one severity {info,warning,danger} (≤2s); broadcast `alert.broadcast` over WS to dashboard + invigilator rooms only when `confirmed==true` (≤2s) including anomaly reasons; do NOT broadcast guardian anomalies with `confirmed==false`; optional SMTP email (≤30s) with failure-tolerant fallback to WS; wire to `anomaly.detected`
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [x]* 13.1 Write unit tests for Herald broadcasting rules
    - Test severity persistence, no-broadcast on unconfirmed guardian anomaly, reasons included in payload, email-failure fallback retains alert + completes WS broadcast
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.7_

- [x] 14. Checkpoint - P1 backend agents complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Scaffold frontend project and design system
  - [x] 15.1 Scaffold React 18 + Vite + TS + Tailwind app
    - Create `frontend` with vite, react, react-dom, typescript, tailwindcss, react-router-dom, zustand, recharts, @mediapipe/tasks-vision, vitest, fast-check, @testing-library/react
    - Set up `src/app` router/providers and base layout
    - _Requirements: 16.1_

  - [x] 15.2 Implement design system tokens and theme
    - Configure `tailwind.config.ts` with navy/crimson/gold palette, semantic colors, radii, shadows, fonts (Inter + JetBrains Mono), brand gradient
    - Add `src/theme` CSS variables (light + `data-theme="dashboard"` dark surfaces), severity→color mapping, and the four-level severity scale
    - Implement focus-ring utility (≥2px, ≥3:1 contrast) applied to interactive elements
    - _Requirements: 16.1, 16.2, 16.4, 16.5, 16.6, 16.8_

  - [x] 15.3 Implement shared UI components
    - Build Button, AgentCard, AlertItem (severity color + text label, never color-only; `aria-live` ready), AgentMessageRow (mono), SessionTile (integrity ring), StatPill, Heatmap cell per Core Component Specs
    - _Requirements: 16.2, 16.3, 16.5_

  - [x] 15.4 Implement shared types and API/WS clients
    - Add `src/types` mirroring backend schemas + `WSMessage` envelope; `src/lib/apiClient` (JWT auth header, refresh handling) and `src/lib/wsClient`
    - _Requirements: 12.1_

  - [x]* 15.5 Write unit tests for severity mapping and accessibility props
    - Test severity→color consistency across components, text-label presence (no color-only), `aria-live="polite"` on alert feed region
    - _Requirements: 16.2, 16.3, 16.7_

- [x] 16. Implement student exam portal with Stage 1 proctoring
  - [x] 16.1 Implement Stage 1 local screening logic
    - Add `frontend/src/features/exam/proctoring.ts`: pure `evaluateFrame(faceCount, gazeOffset, headYaw, now, state, cfg)` classifying `face_absent`/`multiple_faces`/`gaze_away`/`none`, debounce accumulators (per-kind minDuration), per-kind cooldown, escalation-timestamp recording; configurable thresholds (yaw 10–60°, gaze 0.10–0.90, minDuration 0.5–10s, cooldown 5–300s)
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x]* 16.2 Write property test for escalation gating (Stage 1)
    - **Property 1: Escalation gating** — for all frame sequences, a debounced signal (escalation) is returned only after persistence ≥ minDurationMs and cooldown elapsed; verifies the loop invariant of `evaluateFrame`
    - **Validates: Requirements 6.5, 6.6, 6.7**

  - [x] 16.3 Implement MediaPipe FaceMesh proctoring hook
    - Add `src/lib/mediapipe` + `useProctoring` hook: init FaceMesh (WASM), evaluate webcam frames ≥5 FPS locally with no network transmission; on debounced signal capture single downscaled JPEG and POST to `/proctoring/escalate`; handle webcam-denied (stop, kind `none`, surface error, retain session) and model-load-timeout (10s → `none` + error)
    - _Requirements: 6.1, 6.8, 6.9, 7.2_

  - [x] 16.4 Implement student exam UI
    - Build exam portal: login, start session, render own paper (no answer keys), answer entry with autosave, session-event capture (tab blur/focus, paste, copy, question view, heartbeat) batched to `/sessions/{id}/events`, submit flow; embed proctoring hook + webcam status indicator
    - _Requirements: 5.1, 5.3, 5.4, 5.6, 5.8_

  - [x]* 16.5 Write unit tests for student portal event capture
    - Test batched event submission (≤100), no answer-key in rendered paper, webcam-error surfacing
    - _Requirements: 5.3, 5.8, 6.8_

- [x] 17. Implement live admin dashboard
  - [x] 17.1 Implement dashboard WebSocket hook with reconnect
    - Add `src/lib/useDashboardSocket`: connect with token, expose `{agents, messages, alerts, sessions, connected}`; exponential backoff reconnect (1s→30s cap, 10 attempts) + REST snapshot resync on reconnect; connection-degraded indication if snapshot fails (keep retrying); cap message ring buffer at 500 (evict oldest)
    - _Requirements: 12.7, 12.8, 12.9_

  - [x] 17.2 Implement dashboard layout and live views
    - Build dark "mission control" dashboard (`data-theme="dashboard"`): top bar, Agent Status Strip (status cards updating on `agent.status` ≤2s), Inter-Agent Communication Log (mono, render `agent.message` ≤2s showing source→target, truncate >2000 chars), Live Alert Feed (severity-colored items on `alert.broadcast` ≤2s, `aria-live="polite"`), Session Grid (tiles update integrity ring/status on `session.update` ≤2s)
    - _Requirements: 12.3, 12.4, 12.5, 12.6, 16.4, 16.5, 16.7_

  - [x]* 17.3 Write unit tests for dashboard event rendering
    - Test agent.message truncation at 2000 chars, 500-message cap eviction, severity color + label on alert items, reconnect backoff schedule
    - _Requirements: 12.3, 12.5, 12.7, 12.8_

- [x] 18. Implement invigilator console
  - Build invigilator view: connect to `/ws/invigilator/{exam_id}`, list/monitor sessions, view session anomalies (`GET /sessions/{id}/anomalies`), terminate session action
  - _Requirements: 5.10, 12.1_

- [x] 19. Checkpoint - P1 end-to-end demoable
  - Ensure all tests pass, ask the user if questions arise.

- [x] 20. [P2] Implement Sentinel explainable behavioral detection
  - [x] 20.1 [P2] Implement Sentinel scoring engine
    - Add `app/agents/sentinel.py`: pure `score_event` weighted sum over normalized features (tab_switch_rate, paste_events, timing_anomaly via `timing_z_score`, answer_similarity), score clamped [0,1] within 2s of event; reasons for every term ≥ reason_threshold; emit `anomaly.detected` (with score + reasons) when ≥ detection_threshold; reject malformed events (no feature update, record error); compute from available inputs noting excluded ones; use server timestamps for timing
    - Implement orchestrator-side batched cross-student answer-similarity (TF-IDF/MCQ vectors) per question
    - Wire Sentinel to `session.event`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

  - [x]* 20.2 [P2] Write property test for score bounds & monotonic reasons
    - **Property 3: Score bounds & monotonic reasons** — for all events `0 ≤ score ≤ 1` and every returned reason corresponds to a term whose normalized contribution ≥ reason_threshold
    - **Validates: Requirements 8.2, 8.3**

  - [x]* 20.3 [P2] Write unit tests for Sentinel feature handling
    - Test malformed-event rejection, missing-input handling with exclusion reason, server-timestamp usage, threshold emission boundary
    - _Requirements: 8.6, 8.7, 8.8, 8.4_

- [x] 21. [P2] Implement Analyst post-exam analytics
  - [x] 21.1 [P2] Implement Analyst agent
    - Add `app/agents/analyst.py` + prompt: on `exam.completed` aggregate session results into score distribution (fixed bands), mean (2 dp), anomaly-count summary, difficulty heatmap (accuracy & difficulty 0–100%), per-student ≥1 improvement suggestion within 120s; emit `report.ready`; on LLM failure after 3 retries produce partial report marking pending sections + schedule retry within 300s, updating + re-emitting `report.ready` on retry completion
    - Add `app/api/analytics.py`: `GET /analytics/exams/{id}`
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [x]* 21.2 [P2] Write unit tests for Analyst aggregation and partial-report flow
    - Test band distribution, mean rounding, ≥1 suggestion per student, partial report + retry scheduling on LLM failure (mocked LLM)
    - _Requirements: 10.2, 10.4, 10.6, 10.7_

- [x] 22. [P2] Implement analytics frontend views
  - Build analytics feature with Recharts: score distribution chart, difficulty heatmap, anomaly summary, per-student reports/suggestions; consume `report.ready` and `GET /analytics/exams/{id}`
  - _Requirements: 10.1, 10.2, 10.3, 12.1_

  - [x]* 22.1 [P2] Write unit tests for analytics rendering
    - Test heatmap cell color scale and distribution rendering from sample analytics payload
    - _Requirements: 10.2, 10.3, 16.2_

- [x] 23. [P2] Checkpoint - P2 complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 24. [P3] Implement Auditor fairness audit
  - Add `app/agents/auditor.py` + prompt: on `paper.generated` review each question across cultural bias / difficulty calibration / language clarity (pass|fail each); overall `approved` if all pass else `needs_revision`; on approved emit `audit.completed` ≤60s and set paper audit_status `approved`; on needs_revision set `flagged` and record per-failing-question dimensions + issue descriptions; on review failure leave audit_status unchanged + emit audit-failed event with reason; wire into orchestration flow between Architect and release
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_

  - [x]* 24.1 [P3] Write unit tests for Auditor verdict logic
    - Test approved/needs_revision verdict derivation, flagged-question recording, audit-failure leaves status unchanged (mocked LLM)
    - _Requirements: 13.3, 13.4, 13.5, 13.6_

- [x] 25. [P3] Implement PDF report generation
  - Add `reportlab` + `GET /analytics/exams/{id}/report.pdf` rendering the Analyst report as a downloadable PDF
  - _Requirements: 10.1_

- [x] 26. Implement deployment configuration and demo-data seeding
  - [x] 26.1 Implement deployment config and migrations
    - Add `backend/render.yaml` (Render web service + managed PostgreSQL, secrets as env), `alembic` migrations for PostgreSQL, and `frontend/vercel.json` (SPA rewrites); document env keys
    - _Requirements: 15.1, 15.5_

  - [x] 26.2 Implement demo-data seed script
    - Add a backend seed script provisioning an admin, an invigilator, several students, one exam with a small blueprint, and a scripted anomaly timeline for a reliable, repeatable live demo
    - _Requirements: 3.1, 4.1, 5.1_

- [x] 27. Integration and final wiring
  - [x] 27.1 Wire full end-to-end orchestration
    - Confirm orchestrator subscribes all P1 (+P2 if built) agent handlers to the event bus and that REST/WS endpoints are mounted in the app factory; verify provision → paper.generated → session → escalate → anomaly.detected → alert.broadcast → submit → exam.completed flow
    - _Requirements: 11.1, 11.5_

  - [x]* 27.2 Write end-to-end integration test
    - Start exam → emit session events → trigger escalation (mocked Vision) → assert `anomaly.detected` → assert Herald broadcast over a test WS client → submit → assert Analyst `report.ready`; assert WS connection with wrong role rejected
    - _Requirements: 7.4, 9.2, 5.7, 10.5, 2.8_

- [x] 28. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (tests) and are not auto-implemented; core implementation tasks are never optional.
- Tasks tagged `[P2]`/`[P3]` are later phases — the team can stop after task 19 (end of P1) and still have a working, demoable system.
- Each task references specific requirement clauses for traceability.
- Property tests use `hypothesis` (Python backend) and `fast-check` (TypeScript Stage-1 logic), validating the 8 Correctness Properties from the design.
- Checkpoints (tasks 8, 14, 19, 23, 28) ensure incremental validation at phase boundaries.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "1.1"] },
    { "id": 1, "tasks": ["2.1", "2.2", "3", "4.1"] },
    { "id": 2, "tasks": ["2.3", "3.1", "3.2", "4.2", "6.1"] },
    { "id": 3, "tasks": ["4.3", "5.1", "5.2", "6.2"] },
    { "id": 4, "tasks": ["5.3", "5.4", "6.3", "7.1", "7.2"] },
    { "id": 5, "tasks": ["7.3", "9.1", "9.2", "10.1", "10.2", "10.3", "11"] },
    { "id": 6, "tasks": ["9.3", "9.4", "10.4", "10.5", "11.1", "12.1", "12.2"] },
    { "id": 7, "tasks": ["12.3", "12.4", "13"] },
    { "id": 8, "tasks": ["13.1", "15.1"] },
    { "id": 9, "tasks": ["15.2", "15.4"] },
    { "id": 10, "tasks": ["15.3", "16.1"] },
    { "id": 11, "tasks": ["15.5", "16.2", "16.3", "17.1"] },
    { "id": 12, "tasks": ["16.4", "17.2", "18"] },
    { "id": 13, "tasks": ["16.5", "17.3", "20.1"] },
    { "id": 14, "tasks": ["20.2", "20.3", "21.1"] },
    { "id": 15, "tasks": ["21.2", "22"] },
    { "id": 16, "tasks": ["22.1", "24"] },
    { "id": 17, "tasks": ["24.1", "25", "26.1"] },
    { "id": 18, "tasks": ["26.2", "27.1"] },
    { "id": 19, "tasks": ["27.2"] }
  ]
}
```

# Requirements Document

## Introduction

DRONA AI is an autonomous, multi-agent examination integrity platform. Six coordinated agents (Guardian, Architect, Sentinel, Analyst, Herald, and an optional Auditor) collaborate to make exam cheating structurally difficult: they generate a unique paper per student, proctor each session live in the browser, detect behavioral fraud with explainable scores, broadcast real-time alerts, and produce post-exam intelligence. Agents coordinate through a shared event bus, and every inter-agent message is streamed to a live admin dashboard — the system's signature demo moment.

The platform is built as a production-shaped system: a layered FastAPI backend with async WebSockets, SQLAlchemy persistence (SQLite local → PostgreSQL deploy), JWT role-based access control for three roles (Admin, Invigilator, Student), centralized error handling, and secrets-managed API keys. The architectural differentiator is a two-stage proctoring escalation pipeline: Stage 1 screens locally in the browser with MediaPipe FaceMesh at zero network cost, and Stage 2 escalates a single captured frame to OpenAI Vision for authoritative confirmation only when a local anomaly is first detected. The frontend is React 18 + Vite + TypeScript + Tailwind with a deliberately designed navy/crimson visual identity.

These requirements are derived from the approved design document and are prioritized to match the design's P1/P2/P3 build phasing.

### Priority Legend

- **P1 (Qualify)**: Orchestration backbone, Guardian (Stage 1 + escalation), Architect, student portal, admin dashboard, Herald, authentication, RBAC, answer-key confidentiality, backend robustness, design system.
- **P2 (Top 100)**: Sentinel, inter-agent communication visualization, Analyst.
- **P3 (Bonus)**: Auditor (fairness audit), PDF reports.

## Glossary

- **DRONA_System**: The complete DRONA AI platform comprising backend, frontend, agents, and persistence.
- **Guardian**: The agent owning identity and presence integrity through the two-stage proctoring pipeline.
- **Architect**: The agent that generates a unique exam paper per student from a blueprint.
- **Sentinel**: The agent that detects behavioral fraud from session telemetry and produces explainable anomaly scores.
- **Analyst**: The agent that produces post-exam analytics, reports, and improvement suggestions.
- **Herald**: The agent that broadcasts confirmed anomalies to humans via WebSocket and optional email.
- **Auditor**: The optional agent that reviews generated questions for bias, difficulty calibration, and clarity.
- **Orchestrator**: The CrewAI-based component that assembles the agent crew and wires agents to the event bus.
- **Event_Bus**: The in-process asynchronous publish/subscribe component decoupling agents from each other and from transport.
- **WebSocket_Manager**: The component maintaining live connections per room and fanning out events.
- **Stage_1_Screener**: The browser-side MediaPipe FaceMesh screening logic (`evaluateFrame`) that produces debounced local signals.
- **Stage_2_Confirmer**: The Guardian server component that submits an escalated frame to OpenAI Vision and returns a structured verdict.
- **Auth_Service**: The component handling login, token issuance, refresh, and JWT validation.
- **RBAC_Guard**: The authorization mechanism enforcing role requirements on endpoints and WebSocket connections.
- **Admin_Dashboard**: The live "mission control" admin view showing agent status, session grid, inter-agent communication feed, and alerts.
- **Blueprint**: An exam specification of topics, question counts, difficulty mix, and question types.
- **Generated_Paper**: A per-student set of questions produced by the Architect.
- **Local_Signal**: A debounced Stage-1 screening result with a kind (`face_absent`, `multiple_faces`, `gaze_away`, or `none`), duration, and local confidence.
- **Vision_Verdict**: The structured Stage-2 result (presence, face count, secondary person, looking away, confidence, rationale).
- **Anomaly**: A detected integrity violation with a source agent, category, score, reasons, evidence, and confirmation flag.
- **Alert**: A human-facing notification derived from an anomaly, with a severity and message.
- **Session_Event**: A telemetry record from a student session (tab blur/focus, paste, copy, answer change, question view, heartbeat).
- **Integrity_Score**: A per-session score where 1.0 is clean and the value decreases as confirmed anomalies accumulate.
- **Design_System**: The token-driven visual identity (navy/crimson palette, semantic tokens, typography, components).
- **JWT**: A JSON Web Token used for stateless authentication, issued as access and refresh tokens.

## Requirements

### Requirement 1: Authentication and Token Management (P1)

**User Story:** As a platform user, I want to authenticate with my credentials and receive secure tokens, so that I can access role-appropriate features safely.

#### Acceptance Criteria

1. WHEN a user submits a valid email and password to the login endpoint, THE Auth_Service SHALL return a JWT access token valid for 15 minutes and a refresh token valid for 7 days.
2. IF a user submits an email or password that does not match a stored credential, THEN THE Auth_Service SHALL reject the request with a 401 response and a machine-readable error code that does not indicate whether the email or the password was incorrect.
3. WHEN a user submits a valid, unexpired refresh token to the refresh endpoint, THE Auth_Service SHALL issue a rotated JWT access token valid for 15 minutes and a new refresh token valid for 7 days.
4. WHEN the Auth_Service issues a rotated refresh token, THE Auth_Service SHALL invalidate the submitted refresh token so that it cannot be reused.
5. IF a request presents an expired, malformed, or otherwise invalid refresh token to the refresh endpoint, THEN THE Auth_Service SHALL reject the request with a 401 response and a machine-readable error code, and SHALL NOT issue any new token.
6. IF a request presents an expired, malformed, or otherwise invalid access token, THEN THE Auth_Service SHALL reject the request with a 401 response and a machine-readable error code.
7. THE Auth_Service SHALL store user passwords only as bcrypt hashes and SHALL NOT persist or log raw passwords.
8. IF a user submits an account password shorter than 8 characters or longer than 128 characters, THEN THE Auth_Service SHALL reject the request with a 400 response and a machine-readable error code indicating the password length constraint, and SHALL NOT create or update the account.
9. WHEN a user requests the current-profile endpoint with a valid, unexpired access token, THE Auth_Service SHALL return the authenticated user's profile including the assigned role.

### Requirement 2: Role-Based Access Control (P1)

**User Story:** As a security-conscious administrator, I want every protected resource guarded by role, so that users can only perform actions appropriate to their role.

#### Acceptance Criteria

1. IF a request to a protected endpoint presents a JWT that is missing, malformed, or expired, THEN THE RBAC_Guard SHALL reject the request with a 401 response before any business logic runs.
2. IF an authenticated request presents a role not authorized for the requested endpoint, THEN THE RBAC_Guard SHALL reject the request with a 403 response before any business logic runs.
3. THE RBAC_Guard SHALL recognize exactly three roles: Admin, Invigilator, and Student.
4. WHEN an authenticated Student requests a session or paper whose assigned student identifier matches the requesting Student's identifier, THE DRONA_System SHALL return the requested resource.
5. IF an authenticated Student requests a session or paper whose assigned student identifier does not match the requesting Student's identifier, THEN THE DRONA_System SHALL reject the request with a 403 response before returning any resource data.
6. WHEN a client opens a WebSocket connection, THE WebSocket_Manager SHALL validate the JWT and role before binding the connection to a room.
7. IF a WebSocket connection presents a JWT that is missing, malformed, or expired, THEN THE WebSocket_Manager SHALL reject the connection by closing it before binding it to any room.
8. IF a WebSocket connection presents a role not authorized for the requested room, THEN THE WebSocket_Manager SHALL reject the connection by closing it before binding it to the requested room.

### Requirement 3: Exam Creation and Blueprint Definition (P1)

**User Story:** As an Admin, I want to create an exam with a blueprint, so that the platform can provision unique papers for enrolled students.

#### Acceptance Criteria

1. WHEN an Admin submits an exam definition containing a non-empty title of 1 to 200 characters, a blueprint with at least 1 topic, and a total question count of at least 1, THE DRONA_System SHALL create an exam record with status `draft`.
2. IF an exam blueprint specifies fewer than 1 topic or more than 100 topics, THEN THE DRONA_System SHALL reject the exam creation with a 422 validation response indicating the topic count is out of range.
3. IF an exam blueprint specifies a total question count less than 1 or greater than 1000, THEN THE DRONA_System SHALL reject the exam creation with a 422 validation response indicating the question count is out of range.
4. IF an Admin submits an exam definition with a title that is empty or exceeds 200 characters, THEN THE DRONA_System SHALL reject the exam creation with a 422 validation response indicating the title is invalid, and SHALL NOT create an exam record.
5. WHEN an Admin requests the exam list, THE DRONA_System SHALL return all exams whose assigned role audience includes the requesting user's Admin or Invigilator role.
6. WHEN an Admin triggers provisioning for an exam with status `draft`, THE DRONA_System SHALL set the exam status to `provisioning`.
7. WHEN the exam status transitions to `provisioning`, THE DRONA_System SHALL dispatch paper generation for each enrolled student.
8. IF an Admin triggers provisioning for an exam whose status is not `draft`, THEN THE DRONA_System SHALL reject the request with a validation response indicating the exam is not in a provisionable state, and SHALL leave the exam status unchanged.
9. IF an Admin triggers provisioning for an exam that has zero enrolled students, THEN THE DRONA_System SHALL reject the request with a validation response indicating no students are enrolled, and SHALL leave the exam status as `draft`.

### Requirement 4: Architect Unique Paper Generation (P1)

**User Story:** As an exam authority, I want each student to receive a unique but equivalently fair paper, so that copying answers between students provides no advantage.

#### Acceptance Criteria

1. WHEN the Architect receives an `exam.provision` event for a student, THE Architect SHALL generate a paper containing exactly the total question count specified by the blueprint within the configured generation timeout (default 60 seconds).
2. WHEN the Architect generates a paper, THE Architect SHALL produce a paper in which the question count per topic and the question count per difficulty level each equal the corresponding counts specified in the exam blueprint, with zero deviation.
3. FOR ALL pairs of distinct papers generated for the same exam, THE Architect SHALL produce content whose pairwise similarity score, expressed as a value between 0.0 and 1.0, is strictly below the configured uniqueness ceiling.
4. WHERE a generated MCQ question is produced, THE Architect SHALL include between 2 and the configured maximum option count (default 4) options, of which exactly one is marked as the correct option.
5. IF the Architect's language model output fails schema validation, THEN THE Architect SHALL reject the output, retain no partial paper for the student, and retry generation up to the configured maximum retries (default 3 attempts).
6. IF schema validation continues to fail after the configured maximum retries are exhausted, THEN THE Architect SHALL abort generation for that student, persist no paper, and emit a generation-failure event indicating the student and the failure cause.
7. WHEN a paper is successfully generated and persisted, THE Architect SHALL emit a `paper.generated` event identifying the student and the generated paper.
8. IF persistence of a successfully generated paper fails, THEN THE Architect SHALL not emit a `paper.generated` event and SHALL emit a generation-failure event indicating the student and the persistence failure cause.
9. THE Architect SHALL store each question's answer key server-side only and SHALL exclude the answer key from any paper content delivered to the student.

### Requirement 5: Student Exam Session Lifecycle (P1)

**User Story:** As a Student, I want to start, take, and submit my exam, so that my responses are recorded and graded.

#### Acceptance Criteria

1. WHEN a Student starts a session for an exam and the Student has no existing active or submitted session for that exam, THE DRONA_System SHALL create a session with status `active` and return the Student's own paper within 3 seconds.
2. IF a Student starts a session for an exam for which the Student already has an `active` or `submitted` session, THEN THE DRONA_System SHALL reject the request, retain the existing session unchanged, and return an error response indicating that a session already exists.
3. WHEN the DRONA_System returns a paper to a Student, THE DRONA_System SHALL exclude all answer-key fields from the response such that no correct-answer, scoring-key, or solution field is present in the returned payload.
4. WHILE a session has status `active`, WHEN a Student submits or updates an answer, THE DRONA_System SHALL persist the answer together with the server-recorded time spent measured in whole seconds.
5. IF a Student submits or updates an answer for a session whose status is not `active`, THEN THE DRONA_System SHALL reject the request, leave any previously persisted answer unchanged, and return an error response indicating that the session is not active.
6. WHEN a Student submits the exam for an `active` session, THE DRONA_System SHALL set the session status to `submitted`.
7. WHEN a session status transitions to `submitted`, THE DRONA_System SHALL emit exactly one `exam.completed` event.
8. WHEN a Student sends a batch of between 1 and 100 session events, THE DRONA_System SHALL persist each event with an authoritative server timestamp recorded in UTC with millisecond precision.
9. IF a Student sends a batch containing more than 100 session events, THEN THE DRONA_System SHALL reject the batch, persist none of its events, and return an error response indicating that the batch size limit was exceeded.
10. WHEN an authenticated Invigilator or Admin terminates a session, THE DRONA_System SHALL set the session status to `terminated` and reject any subsequent answer submissions for that session.

### Requirement 6: Guardian Stage 1 Local Screening (P1)

**User Story:** As a privacy-conscious student and a cost-conscious operator, I want proctoring to screen locally in the browser, so that video stays on the device and cloud cost stays near zero during normal exams.

#### Acceptance Criteria

1. WHILE a student session is active, THE Stage_1_Screener SHALL evaluate webcam frames locally in the browser at a rate of at least 5 frames per second without transmitting any video frame, image, or derived frame pixel data over the network.
2. WHEN the Stage_1_Screener (MediaPipe FaceMesh) detects zero faces in a frame, THE Stage_1_Screener SHALL classify the instantaneous condition as `face_absent`.
3. WHEN the Stage_1_Screener (MediaPipe FaceMesh) detects more than one face in a frame, THE Stage_1_Screener SHALL classify the instantaneous condition as `multiple_faces`.
4. WHEN the estimated head yaw magnitude exceeds its configured threshold (configurable from 10 to 60 degrees, default 25 degrees) or the estimated gaze offset exceeds its configured threshold (configurable from 0.10 to 0.90 on a normalized 0.0 to 1.0 scale, default 0.30), THE Stage_1_Screener SHALL classify the instantaneous condition as `gaze_away`.
5. IF a candidate condition has not persisted continuously for at least its configured minimum duration (configurable per kind from 0.5 to 10 seconds, default 2 seconds), THEN THE Stage_1_Screener SHALL return a signal of kind `none`.
6. IF the elapsed time since the last recorded escalation of a candidate kind is less than its configured cooldown (configurable per kind from 5 to 300 seconds, default 30 seconds), THEN THE Stage_1_Screener SHALL return a signal of kind `none`.
7. WHEN a candidate condition has persisted continuously beyond its configured minimum duration and its configured cooldown has elapsed, THE Stage_1_Screener SHALL return a debounced signal of that kind and record the escalation timestamp within 200 milliseconds of the evaluated frame.
8. IF webcam access is denied or the webcam stream becomes unavailable, THEN THE Stage_1_Screener SHALL stop evaluation, return a signal of kind `none`, and surface an error indication identifying that the webcam is unavailable while retaining the active session state.
9. IF the MediaPipe FaceMesh model fails to initialize or load within 10 seconds of session start, THEN THE Stage_1_Screener SHALL return a signal of kind `none` and surface an error indication identifying that local screening is unavailable.

### Requirement 7: Guardian Stage 2 Cloud Confirmation (P1)

**User Story:** As an Invigilator, I want suspect frames confirmed by an authoritative vision check, so that alerts reflect verified anomalies rather than transient local glitches.

#### Acceptance Criteria

1. WHEN a Student posts an escalation with a debounced local signal and a frame, THE Stage_2_Confirmer SHALL submit the frame to OpenAI Vision and return a structured Vision_Verdict containing a verdict label and a confidence value between 0.0 and 1.0, within 10 seconds of receiving the escalation.
2. THE DRONA_System SHALL invoke OpenAI Vision only in response to an escalation triggered by a Stage-1 anomaly, and SHALL NOT call OpenAI Vision during normal frame screening.
3. IF an escalation payload exceeds the configured maximum frame size (default 5 MB) or has a MIME type other than image/jpeg or image/png, THEN THE Stage_2_Confirmer SHALL reject the request with a 422 response, discard the rejected payload, and SHALL NOT call OpenAI Vision.
4. WHEN a Vision_Verdict indicates an anomalous condition with a confidence of at least 0.70, THE Guardian SHALL treat the anomaly as confirmed and emit an `anomaly.detected` event with `confirmed` set to true.
5. WHEN a Vision_Verdict returns a benign result, THE Guardian SHALL record a false positive and SHALL raise the local screening threshold for that session by 0.05, capped at a maximum of 0.95.
6. IF OpenAI Vision is unavailable or does not respond within the 10-second timeout during an escalation, THEN THE Guardian SHALL record the anomaly as unconfirmed with severity `warning` and leave the session's local screening threshold unchanged.
7. IF an escalation is received for a session that is not active or not bound to the requesting Student's JWT, THEN THE Stage_2_Confirmer SHALL reject the request, SHALL NOT call OpenAI Vision, and SHALL return an authorization error response.
8. WHEN the Stage_2_Confirmer finishes scoring an escalated frame, regardless of outcome, THE Stage_2_Confirmer SHALL discard the raw escalated frame within 60 seconds under the default retention policy.

### Requirement 8: Sentinel Explainable Behavioral Detection (P2)

**User Story:** As an Invigilator, I want behavioral anomalies scored with human-readable reasons, so that I can understand and justify why a session was flagged.

#### Acceptance Criteria

1. WHEN the Sentinel receives a valid `session.event`, THE Sentinel SHALL update the session's behavioral features and compute an updated anomaly score within 2 seconds of event receipt.
2. THE Sentinel SHALL produce every anomaly score as a value within the inclusive range 0 to 1.
3. WHEN the Sentinel returns an anomaly score, THE Sentinel SHALL include a human-readable reason for every contributing term whose normalized contribution is greater than or equal to the configured reason threshold, where the reason threshold is configurable within the inclusive range 0 to 1.
4. WHEN an anomaly score becomes greater than or equal to the configured detection threshold, THE Sentinel SHALL emit an `anomaly.detected` event containing the anomaly score and the contributing reasons, where the detection threshold is configurable within the inclusive range 0 to 1.
5. THE Sentinel SHALL compute behavioral features from tab-switch count, paste events, per-question timing, and cross-student answer similarity.
6. WHEN computing timing-based features, THE Sentinel SHALL use the server-recorded timestamp rather than the client-supplied timestamp.
7. IF the Sentinel receives a `session.event` that is malformed or missing required fields, THEN THE Sentinel SHALL reject the event without updating the session's behavioral features and SHALL record an error indication identifying the rejected event.
8. IF one or more required feature inputs (tab-switch count, paste events, per-question timing, or cross-student answer similarity) are unavailable when computing an anomaly score, THEN THE Sentinel SHALL compute the score from the available inputs and SHALL include a human-readable reason indicating which inputs were excluded.

### Requirement 9: Herald Real-Time Alert Broadcasting (P1)

**User Story:** As an Admin or Invigilator, I want confirmed anomalies broadcast to me in real time, so that I can respond to integrity violations during the exam.

#### Acceptance Criteria

1. WHEN the Herald receives an `anomaly.detected` event, THE Herald SHALL persist a corresponding alert with exactly one severity value from the set {info, warning, danger} as indicated by the received event, within 2 seconds of receiving the event.
2. WHEN the Herald persists an alert for an anomaly whose `confirmed` flag is true, THE Herald SHALL broadcast an `alert.broadcast` message over WebSocket to the dashboard room and invigilator room associated with the exam session of that anomaly, within 2 seconds of persisting the alert.
3. IF the Herald persists an alert for an anomaly sourced from the Guardian whose `confirmed` flag is false, THEN THE Herald SHALL NOT broadcast an `alert.broadcast` message for that anomaly.
4. WHERE an SMTP configuration is present, THE Herald SHALL additionally send an email notification for the broadcast alert within 30 seconds of persisting the alert.
5. IF sending the email notification fails, THEN THE Herald SHALL retain the persisted alert, complete the WebSocket broadcast, and record an indication that email delivery failed.
6. WHERE no SMTP configuration is present, THE Herald SHALL deliver the alert over WebSocket as the primary channel.
7. WHEN the Herald broadcasts an alert, THE Herald SHALL include the anomaly's reasons in the `alert.broadcast` payload.

### Requirement 10: Analyst Post-Exam Analytics (P2)

**User Story:** As an Admin, I want post-exam reports and analytics, so that I can evaluate performance, difficulty, and integrity outcomes.

#### Acceptance Criteria

1. WHEN the Analyst receives an `exam.completed` event, THE Analyst SHALL aggregate all session results for the exam and produce an analytics report containing the score distribution, mean score, anomaly count summary, difficulty heatmap, and per-student improvement suggestions, within 120 seconds of receiving the event.
2. WHEN the Analyst generates the score summary, THE Analyst SHALL produce a score distribution grouped into fixed score bands covering the full scoring range, the arithmetic mean score rounded to 2 decimal places, and a total count of flagged anomalies.
3. WHEN the Analyst generates the difficulty heatmap, THE Analyst SHALL map each topic or question to an accuracy value between 0 and 100 percent and a difficulty value between 0 and 100 percent.
4. WHEN the Analyst generates improvement suggestions, THE Analyst SHALL produce at least one improvement suggestion for each student who completed the exam, derived from that student's exam results.
5. WHEN analytics generation completes successfully for all report sections, THE Analyst SHALL emit a `report.ready` event identifying the exam.
6. IF the Analyst's language model call fails after 3 retry attempts, THEN THE Analyst SHALL produce a partial report containing all successfully generated sections, mark each unavailable section with a status indicating it is pending, and schedule a retry of the failed sections within 300 seconds.
7. WHEN the scheduled retry completes the previously failed sections, THE Analyst SHALL update the report with the completed sections and emit a `report.ready` event identifying the exam.

### Requirement 11: Multi-Agent Orchestration Backbone (P1)

**User Story:** As a system architect, I want agents to coordinate through a shared event bus, so that agents stay decoupled and their interactions are observable.

#### Acceptance Criteria

1. WHEN the Orchestrator wires the Event_Bus during system startup, THE Orchestrator SHALL subscribe each agent's handlers to its designated event types before any event is published.
2. WHEN an event is published, THE Event_Bus SHALL deliver the event to every handler subscribed to that event type within 1000 milliseconds of publication.
3. WHEN an event is published, THE Event_Bus SHALL invoke subscribed handlers in their registration order.
4. IF a subscribed handler raises an exception during delivery, THEN THE Event_Bus SHALL capture the exception, record a central log entry that includes the event id and the handler identifier, and continue delivering the event to the remaining subscribed handlers without re-raising the exception to the publisher.
5. WHEN an agent emits an inter-agent communication, THE DRONA_System SHALL publish an `agent.message` event to the dashboard feed.
6. IF an event is received whose event id matches an event already processed, THEN THE DRONA_System SHALL discard the duplicate event and SHALL NOT create any additional anomaly or alert for that event id.
7. IF an event is published and no handler is subscribed to its event type, THEN THE Event_Bus SHALL discard the event and SHALL NOT raise an error to the publisher.

### Requirement 12: Live Admin Dashboard (P1/P2)

**User Story:** As an Admin, I want a live mission-control dashboard, so that I can watch agents coordinate and respond to anomalies in real time.

#### Acceptance Criteria

1. WHEN an Admin connects to the dashboard WebSocket with a valid token, THE WebSocket_Manager SHALL bind the connection to the `dashboard` room and stream live events.
2. IF a dashboard WebSocket connection presents a token that is missing, malformed, or expired, THEN THE WebSocket_Manager SHALL reject the connection by closing it before streaming any event.
3. WHEN an `agent.message` event is received, THE Admin_Dashboard SHALL render it in the inter-agent communication log showing source, target, and message text within 2 seconds of receipt, truncating message text longer than 2,000 characters.
4. WHEN an `agent.status` event is received, THE Admin_Dashboard SHALL update the corresponding agent status card's state and load within 2 seconds of receipt.
5. WHEN an `alert.broadcast` event is received, THE Admin_Dashboard SHALL insert a severity-colored alert item into the live alert feed within 2 seconds of receipt.
6. WHEN a `session.update` event is received, THE Admin_Dashboard SHALL update the affected session tile's integrity score and status within 2 seconds of receipt.
7. THE Admin_Dashboard SHALL cap the inter-agent communication log at the most recent 500 messages, evicting the oldest message when the cap is exceeded.
8. WHEN a dashboard WebSocket connection drops, THE Admin_Dashboard SHALL automatically attempt to reconnect with exponential backoff starting at 1 second and capped at 30 seconds, for up to 10 attempts, and SHALL resynchronize state via a REST snapshot on reconnection.
9. IF the REST snapshot resynchronization fails after reconnection, THEN THE Admin_Dashboard SHALL surface a connection-degraded indication to the Admin and continue retrying the snapshot.

### Requirement 12A: WebSocket Connection Management (P1)

**User Story:** As an operator, I want WebSocket connections managed reliably, so that live updates remain consistent and stale connections are cleaned up.

#### Acceptance Criteria

1. THE WebSocket_Manager SHALL maintain up to 10,000 concurrent connections grouped by room, including `dashboard`, `invigilator:{exam_id}`, and `session:{session_id}`.
2. WHEN a client successfully authenticates for a room, THE WebSocket_Manager SHALL bind the connection to that room and begin delivering room-targeted events.
3. WHILE a WebSocket connection is open, THE WebSocket_Manager SHALL send a heartbeat ping every 30 seconds and expect a pong within 10 seconds.
4. IF a connection fails to respond to 3 consecutive heartbeat pings, THEN THE WebSocket_Manager SHALL prune the connection by closing it, removing it from its room, and releasing its resources within 5 seconds.
5. WHEN an event targets a room, THE WebSocket_Manager SHALL deliver the event only to connections bound to that room and SHALL NOT deliver it to connections in other rooms.
6. IF a connection requests a room with an invalid or unknown room identifier, THEN THE WebSocket_Manager SHALL reject the connection by closing it without binding it to any room.
7. IF delivery of an event to a bound connection fails, THEN THE WebSocket_Manager SHALL prune that connection and continue delivering the event to the remaining connections in the room.

### Requirement 13: Auditor Fairness Audit (P3)

**User Story:** As an exam authority, I want generated questions reviewed for fairness before release, so that papers are free of bias and ambiguity.

#### Acceptance Criteria

1. WHEN the Auditor receives a `paper.generated` event, THE Auditor SHALL review each question against three fairness dimensions: cultural bias (presence of region-, gender-, religion-, or socioeconomic-specific references not required by the subject), difficulty calibration (deviation of the question's assessed difficulty from its declared target difficulty level), and language clarity (presence of ambiguous phrasing, undefined terms, or multiple defensible correct answers).
2. WHEN the Auditor reviews a question, THE Auditor SHALL assign each of the three fairness dimensions a pass or fail result, where a dimension result of fail indicates at least one issue of that dimension was detected.
3. WHEN the Auditor completes review of all questions in a paper, THE Auditor SHALL produce an overall verdict of `approved` if every question passes all three fairness dimensions, otherwise `needs_revision`.
4. WHEN the Auditor produces a verdict of `approved`, THE Auditor SHALL emit an `audit.completed` event within 60 seconds of producing the verdict and set the paper's audit status to `approved`.
5. WHEN the Auditor produces a verdict of `needs_revision`, THE Auditor SHALL set the paper's audit status to `flagged` and record, for each failing question, the question identifier, the failing dimension(s), and a description of each detected issue.
6. IF the Auditor cannot complete the review of a paper, THEN THE Auditor SHALL leave the paper's audit status unchanged from its pre-review value, emit an event indicating that the audit failed, and include an indication of the reason the review could not be completed.

### Requirement 14: Answer-Key Confidentiality and Data Integrity (P1)

**User Story:** As an exam authority, I want answer keys and untrusted client data handled securely, so that students cannot extract answers or forge timing.

#### Acceptance Criteria

1. FOR ALL responses returned to a Student, THE DRONA_System SHALL exclude every answer-key field from serialization, such that no answer-key value appears in the response payload, headers, or error output.
2. IF a response intended for a Student is detected to contain an answer-key field before transmission, THEN THE DRONA_System SHALL block transmission of that response, return an error response indicating a serialization-integrity failure, and exclude the answer-key field from any logged output.
3. WHEN a session event is received, THE DRONA_System SHALL treat the client-supplied timestamp as untrusted and SHALL use the server-recorded timestamp as the authoritative value for all timing analysis.
4. IF a session event is received without a server-recordable timestamp, THEN THE DRONA_System SHALL assign the server timestamp at the moment of receipt and SHALL proceed using that value as authoritative.
5. WHEN a confirmed anomaly is recorded for a session, THE DRONA_System SHALL update the session's integrity score to a value less than or equal to its prior value, such that the integrity score is non-increasing as confirmed anomalies accumulate.
6. THE DRONA_System SHALL constrain each session's integrity score to the inclusive range 0 to 100, clamping any computed value below 0 to 0 and any computed value above 100 to 100.
7. THE DRONA_System SHALL constrain each anomaly score to the inclusive range 0.0 to 1.0, clamping any computed value outside this range to the nearest bound.
8. IF an alert severity is assigned a value outside the defined severity enumeration, THEN THE DRONA_System SHALL reject the assignment and return an error response indicating an invalid severity value, leaving the alert's prior severity unchanged.

### Requirement 15: Backend Robustness and Security (P1)

**User Story:** As an operator, I want a production-quality, secure backend, so that the platform is robust against failure and abuse.

#### Acceptance Criteria

1. THE DRONA_System SHALL load all secrets, including the JWT secret and API keys, from environment configuration, and SHALL NOT write secret values to logs, error messages, error envelopes, or API responses.
2. IF any required secret is absent from environment configuration at startup, THEN THE DRONA_System SHALL abort startup and emit a startup error indicating the missing secret by configuration key name only.
3. WHEN a request payload is received, THE DRONA_System SHALL validate it against its declared typed schema, and IF the payload violates the schema or exceeds the maximum accepted body size of 1 MB, THEN THE DRONA_System SHALL reject it with a 422 response carrying the standard error envelope.
4. WHEN an unhandled application error occurs, THE DRONA_System SHALL return a 500 response containing the standard error envelope with a non-empty error code field, a human-readable message field, and a request id field that matches the request id recorded for that request, and SHALL NOT include stack traces, secret values, or raw database error text.
5. WHEN a cross-origin request presents an Origin not in the configured frontend origin allowlist, THE DRONA_System SHALL omit cross-origin access headers and deny the request, and WHEN the Origin matches an allowlisted entry exactly, THE DRONA_System SHALL permit the request.
6. WHEN requests to the login endpoint or the proctoring escalation endpoint exceed 5 requests per source IP within any rolling 60-second window, or 5 requests per session within any rolling 60-second window, THE DRONA_System SHALL reject further requests in that window with a 429 response carrying the standard error envelope until the window resets.
7. THE DRONA_System SHALL execute every database access through parameterized queries with bound parameters, and SHALL NOT construct any query by concatenating or interpolating request-derived values into query text.
8. WHEN a request reaches any network-exposed REST or WebSocket endpoint other than the public login endpoint, THE DRONA_System SHALL require a valid authentication token, and IF the token is absent, expired, or invalid, THEN THE DRONA_System SHALL reject the request with a 401 response carrying the standard error envelope.
9. IF an authenticated caller's role is not permitted for the requested endpoint, THEN THE DRONA_System SHALL reject the request with a 403 response carrying the standard error envelope and SHALL NOT perform the requested operation.
10. IF a database operation fails due to a transient fault, defined as a connection failure, connection timeout, or deadlock, THEN THE DRONA_System SHALL retry the operation exactly once after a delay of at most 500 milliseconds, and IF the retry also fails, THEN THE DRONA_System SHALL return a 503 response carrying the standard error envelope with a request id and SHALL leave persisted data unchanged.

### Requirement 16: Visual Design System (P1)

**User Story:** As a user, I want a polished, consistent, and accessible interface, so that the platform feels authoritative and trustworthy.

#### Acceptance Criteria

1. THE DRONA_System SHALL apply the navy/crimson brand palette and semantic color tokens defined in the design system to all interface surfaces, including backgrounds, text, borders, icons, and interactive controls.
2. WHEN displaying an alert, anomaly badge, or agent state, THE DRONA_System SHALL map each severity level to its designated color from the four-level scale (info, success, warning, danger) and SHALL apply the same color to a given severity level across every surface and component.
3. WHEN rendering an alert, THE DRONA_System SHALL convey severity using both the mapped severity color and a visible text label, and SHALL NOT use color as the only means of conveying severity.
4. THE Admin_Dashboard SHALL apply the dark "mission control" surface theme defined in the design system to all dashboard surfaces.
5. THE DRONA_System SHALL render the inter-agent communication log in the monospace typeface defined by the design system.
6. THE DRONA_System SHALL render a keyboard focus indicator on every interactive element that has a color contrast ratio of at least 3:1 against adjacent colors and an outline thickness of at least 2 CSS pixels, and SHALL keep the indicator visible for the full duration that the element holds keyboard focus.
7. WHEN a new alert is added to the live alert feed, THE DRONA_System SHALL expose the alert within an `aria-live` region set to "polite" so that assistive technologies announce it without interrupting the user's current task.
8. THE DRONA_System SHALL render text and non-text interface elements with a color contrast ratio of at least 4.5:1 for normal-size text and at least 3:1 for large-size text and non-text UI components, measured against their immediate background.

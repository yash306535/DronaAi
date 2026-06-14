"""Demo-data seed script for DRONA AI (task 26.2).

Provisions a reliable, repeatable, **offline** demo dataset so the platform shows
compelling data on first load — without calling any LLM or Vision API. Run it
with::

    python -m app.seed

It creates:

- An admin, an invigilator, and several students with known bcrypt-hashed
  passwords (see :data:`DEMO_CREDENTIALS`; hashing via
  :func:`app.core.security.hash_password`).
- One **live** exam with a small blueprint (a few topics, a modest total
  question count) plus deterministically-synthesized papers + questions for
  each student, so sessions can start immediately.
- A few exam sessions in useful states (active, clean-active, not-started) and a
  scripted anomaly/alert timeline across categories and severities (some
  confirmed) so the live dashboard is immediately interesting.
- A **completed** exam with submitted sessions, graded answers, and a persisted
  :class:`~app.models.orm.ExamAnalytics` record so the analytics view has
  content.

The script is idempotent-ish: it ensures the schema exists
(:func:`app.core.db.create_all`) and clears any prior demo rows (matched by the
fixed demo ids / demo emails) before re-seeding, so re-running it never crashes
on a duplicate key. It relies on the ORM models, repositories, and
``app.core.security`` rather than touching the database directly for inserts.

This module imports — but never modifies — config, security, db, models,
repositories, agents, api, main, and events.

Requirements: 3.1 (per-student unique papers), 4.1 (paper generation), 5.1
(session lifecycle / live proctoring data).
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.db import create_all, get_session_factory
from app.core.security import hash_password
from app.models.enums import (
    AlertSeverity,
    AnomalyCategory,
    AuditStatus,
    ExamStatus,
    QuestionType,
    Role,
    SessionStatus,
    SourceAgent,
)
from app.models.orm import (
    Alert,
    Anomaly,
    Answer,
    Exam,
    ExamAnalytics,
    ExamSession,
    GeneratedPaper,
    Question,
    User,
)

# --- Deterministic demo identity -------------------------------------------

# A fixed namespace so demo ids are stable across runs. Stable ids let the seed
# clear its own prior rows precisely and let documentation reference them.
_NS = uuid.UUID("d2071a00-0000-4000-8000-000000000000")


def _demo_id(name: str) -> str:
    """Return a stable UUID string for the demo entity named ``name``."""
    return str(uuid.uuid5(_NS, name))


# Known, documented demo passwords (hashed at seed time, never stored raw).
ADMIN_PASSWORD = "AdminPass123!"
INVIGILATOR_PASSWORD = "InvigilatorPass123!"
STUDENT_PASSWORD = "StudentPass123!"

ADMIN_EMAIL = "admin@drona.ai"
INVIGILATOR_EMAIL = "invigilator@drona.ai"

# Four demo students keep the dashboard varied without being noisy.
STUDENT_NAMES = ["Arjuna", "Bhima", "Nakula", "Karna"]


def _student_email(index: int) -> str:
    return f"student{index + 1}@drona.ai"


# Stable exam ids so re-runs replace rather than duplicate.
EXAM_LIVE_ID = _demo_id("exam:live")
EXAM_COMPLETED_ID = _demo_id("exam:completed")

# A small blueprint: three topics, six questions total.
DEMO_TOPICS = ["Algebra", "Geometry", "Trigonometry"]
TOTAL_QUESTIONS = 6

# A fixed base time so the scripted anomaly timeline is reproducible.
_BASE_TIME = datetime(2026, 6, 13, 9, 0, 0, tzinfo=timezone.utc)


# --- Summary ----------------------------------------------------------------


@dataclass(slots=True)
class SeedSummary:
    """Counts of seeded entities + the demo credentials, for the run summary."""

    users: int = 0
    students: int = 0
    exams: int = 0
    papers: int = 0
    questions: int = 0
    sessions: int = 0
    anomalies: int = 0
    alerts: int = 0
    answers: int = 0
    analytics: int = 0
    credentials: list[tuple[str, str, str]] = field(default_factory=list)

    def render(self) -> str:
        """Return a short human-readable summary block."""
        lines = [
            "",
            "=" * 60,
            "DRONA AI demo data seeded",
            "=" * 60,
            f"  Users          : {self.users} (admin + invigilator + {self.students} students)",
            f"  Exams          : {self.exams} (1 live, 1 completed)",
            f"  Papers         : {self.papers}",
            f"  Questions      : {self.questions}",
            f"  Exam sessions  : {self.sessions}",
            f"  Anomalies      : {self.anomalies}",
            f"  Alerts         : {self.alerts}",
            f"  Answers        : {self.answers}",
            f"  Exam analytics : {self.analytics}",
            "-" * 60,
            "Demo credentials (email / password / role):",
        ]
        for email, password, role in self.credentials:
            lines.append(f"  - {email:<24} {password:<22} {role}")
        lines.append("=" * 60)
        lines.append("")
        return "\n".join(lines)


# --- Clearing (idempotency) -------------------------------------------------


def _clear_demo_data(session: Session) -> None:
    """Delete any previously-seeded demo rows so re-running never duplicates.

    Deletes the demo exams first (cascading their papers, sessions, events,
    anomalies, alerts, answers, and analytics via ORM relationships), then any
    demo users (matched by their fixed ids *and* by the known demo emails, so a
    stray row sharing a unique email is also removed). The whole clear is one
    committed unit of work.
    """
    for exam_id in (EXAM_LIVE_ID, EXAM_COMPLETED_ID):
        exam = session.get(Exam, exam_id)
        if exam is not None:
            session.delete(exam)
    session.flush()

    demo_user_ids = [_demo_id("user:admin"), _demo_id("user:invigilator")]
    demo_user_ids += [_demo_id(f"user:student:{i}") for i in range(len(STUDENT_NAMES))]
    demo_emails = [ADMIN_EMAIL, INVIGILATOR_EMAIL] + [
        _student_email(i) for i in range(len(STUDENT_NAMES))
    ]

    seen: set[str] = set()
    for user_id in demo_user_ids:
        user = session.get(User, user_id)
        if user is not None and user.id not in seen:
            seen.add(user.id)
            session.delete(user)
    for email in demo_emails:
        user = (
            session.query(User).filter(User.email == email).one_or_none()
        )
        if user is not None and user.id not in seen:
            seen.add(user.id)
            session.delete(user)
    session.commit()


# --- Deterministic content generation ---------------------------------------


def _build_questions(seed_str: str) -> list[Question]:
    """Synthesize ``TOTAL_QUESTIONS`` deterministic demo questions.

    Deterministic (seeded by ``seed_str``) so the dataset is repeatable and
    offline. Cycles through MCQ / short / numerical types and the demo topics,
    producing valid rows (MCQ has >=2 options; answer_key is one of them).
    """
    rng = random.Random(seed_str)
    types = [QuestionType.MCQ, QuestionType.SHORT, QuestionType.NUMERICAL]
    questions: list[Question] = []
    for index in range(TOTAL_QUESTIONS):
        topic = DEMO_TOPICS[index % len(DEMO_TOPICS)]
        qtype = types[index % len(types)]
        difficulty = round(rng.uniform(0.2, 0.9), 2)
        if qtype is QuestionType.MCQ:
            correct = rng.randint(1, 9)
            distractors = sorted({correct + d for d in (1, 2, 3)})
            options = [str(correct)] + [str(d) for d in distractors]
            rng.shuffle(options)
            questions.append(
                Question(
                    index=index,
                    type=qtype,
                    prompt=f"[{topic}] Which value satisfies the demo relation #{index + 1}?",
                    options=options,
                    answer_key=str(correct),
                    topic=topic,
                    difficulty=difficulty,
                    max_marks=1.0,
                )
            )
        elif qtype is QuestionType.NUMERICAL:
            answer = rng.randint(10, 99)
            questions.append(
                Question(
                    index=index,
                    type=qtype,
                    prompt=f"[{topic}] Compute the demo quantity for item #{index + 1}.",
                    options=None,
                    answer_key=str(answer),
                    topic=topic,
                    difficulty=difficulty,
                    max_marks=2.0,
                )
            )
        else:  # SHORT
            questions.append(
                Question(
                    index=index,
                    type=qtype,
                    prompt=f"[{topic}] Briefly explain demo concept #{index + 1}.",
                    options=None,
                    answer_key=f"Sample answer for {topic} #{index + 1}",
                    topic=topic,
                    difficulty=difficulty,
                    max_marks=3.0,
                )
            )
    return questions


def _make_user(*, key: str, email: str, full_name: str, role: Role, password: str) -> User:
    return User(
        id=_demo_id(key),
        email=email,
        full_name=full_name,
        role=role,
        password_hash=hash_password(password),
    )


def _make_paper(exam_id: str, student_id: str, seed: str) -> GeneratedPaper:
    return GeneratedPaper(
        exam_id=exam_id,
        student_id=student_id,
        seed=seed,
        audit_status=AuditStatus.APPROVED,
    )


# --- Seed -------------------------------------------------------------------


def seed(session: Session) -> SeedSummary:
    """Seed (or re-seed) the demo dataset into ``session``'s database.

    Idempotent-ish: clears prior demo rows first, so calling it repeatedly
    leaves the database with exactly one copy of the demo dataset and never
    raises on a duplicate key.
    """
    _clear_demo_data(session)

    summary = SeedSummary()

    # --- Users ---
    admin = _make_user(
        key="user:admin",
        email=ADMIN_EMAIL,
        full_name="Drona Admin",
        role=Role.ADMIN,
        password=ADMIN_PASSWORD,
    )
    invigilator = _make_user(
        key="user:invigilator",
        email=INVIGILATOR_EMAIL,
        full_name="Kripa Invigilator",
        role=Role.INVIGILATOR,
        password=INVIGILATOR_PASSWORD,
    )
    students = [
        _make_user(
            key=f"user:student:{i}",
            email=_student_email(i),
            full_name=name,
            role=Role.STUDENT,
            password=STUDENT_PASSWORD,
        )
        for i, name in enumerate(STUDENT_NAMES)
    ]
    session.add_all([admin, invigilator, *students])
    session.flush()

    summary.users = 2 + len(students)
    summary.students = len(students)
    summary.credentials = [
        (ADMIN_EMAIL, ADMIN_PASSWORD, Role.ADMIN.value),
        (INVIGILATOR_EMAIL, INVIGILATOR_PASSWORD, Role.INVIGILATOR.value),
        (_student_email(0), STUDENT_PASSWORD, f"{Role.STUDENT.value} (all students share this)"),
    ]

    blueprint = {
        "topics": [
            {"name": topic, "count": 2} for topic in DEMO_TOPICS
        ],
        "total_questions": TOTAL_QUESTIONS,
        "difficulty_mix": {"easy": 2, "medium": 3, "hard": 1},
        "types": ["mcq", "short", "numerical"],
    }

    # --- Live exam (drives the dashboard) ---
    live_exam = Exam(
        id=EXAM_LIVE_ID,
        title="DRONA Demo — Mathematics Midterm",
        subject="Mathematics",
        blueprint=blueprint,
        duration_minutes=90,
        starts_at=_BASE_TIME,
        status=ExamStatus.LIVE,
        created_by=admin.id,
    )
    session.add(live_exam)
    session.flush()
    summary.exams += 1

    # Papers + questions for every student on the live exam.
    live_papers: dict[str, GeneratedPaper] = {}
    for student in students:
        paper = _make_paper(live_exam.id, student.id, seed=f"live:{student.id}")
        paper.questions = _build_questions(paper.seed)
        session.add(paper)
        live_papers[student.id] = paper
        summary.papers += 1
        summary.questions += len(paper.questions)
    session.flush()

    summary.sessions += _seed_live_sessions(session, live_exam, students, live_papers, summary)

    # --- Completed exam (drives the analytics view) ---
    completed_exam = Exam(
        id=EXAM_COMPLETED_ID,
        title="DRONA Demo — Physics Quiz (completed)",
        subject="Physics",
        blueprint=blueprint,
        duration_minutes=60,
        starts_at=_BASE_TIME - timedelta(days=1),
        status=ExamStatus.COMPLETED,
        created_by=admin.id,
    )
    session.add(completed_exam)
    session.flush()
    summary.exams += 1

    graded_students = students[:2]
    completed_papers: dict[str, GeneratedPaper] = {}
    for student in graded_students:
        paper = _make_paper(
            completed_exam.id, student.id, seed=f"completed:{student.id}"
        )
        paper.questions = _build_questions(paper.seed)
        session.add(paper)
        completed_papers[student.id] = paper
        summary.papers += 1
        summary.questions += len(paper.questions)
    session.flush()

    summary.sessions += _seed_completed_sessions(
        session, completed_exam, graded_students, completed_papers, summary
    )

    # Persisted analytics so the analytics view has content on first load.
    analytics = ExamAnalytics(
        exam_id=completed_exam.id,
        summary={
            "students": len(graded_students),
            "mean_score": 72.5,
            "median_score": 74.0,
            "max_score": 88.0,
            "min_score": 57.0,
            "anomaly_count": 2,
        },
        difficulty_heatmap={
            topic: {"accuracy": round(0.6 + 0.1 * i, 2), "difficulty": round(0.4 + 0.1 * i, 2)}
            for i, topic in enumerate(DEMO_TOPICS)
        },
        per_student={
            student.id: {
                "name": student.full_name,
                "score": 75.0 - 5.0 * i,
                "suggestions": [f"Review {DEMO_TOPICS[i % len(DEMO_TOPICS)]} fundamentals"],
            }
            for i, student in enumerate(graded_students)
        },
    )
    session.add(analytics)
    summary.analytics += 1

    session.commit()
    return summary


def _seed_live_sessions(
    session: Session,
    exam: Exam,
    students: list[User],
    papers: dict[str, GeneratedPaper],
    summary: SeedSummary,
) -> int:
    """Create live-exam sessions in useful states + a scripted anomaly timeline."""
    count = 0

    # Student 0 — ACTIVE with a rich, scripted anomaly timeline (the demo star).
    s0 = students[0]
    sess0 = ExamSession(
        id=_demo_id("session:live:0"),
        exam_id=exam.id,
        student_id=s0.id,
        paper_id=papers[s0.id].id,
        status=SessionStatus.ACTIVE,
        started_at=_BASE_TIME,
        integrity_score=58.0,
    )
    session.add(sess0)
    session.flush()

    # (category, source_agent, score, confirmed, severity, minute_offset)
    timeline = [
        (AnomalyCategory.GAZE_AWAY, SourceAgent.GUARDIAN, 0.42, False, AlertSeverity.INFO, 3),
        (AnomalyCategory.TAB_SWITCH, SourceAgent.SENTINEL, 0.55, True, AlertSeverity.WARNING, 7),
        (AnomalyCategory.PASTE, SourceAgent.SENTINEL, 0.61, True, AlertSeverity.WARNING, 11),
        (AnomalyCategory.MULTIPLE_FACES, SourceAgent.GUARDIAN, 0.93, True, AlertSeverity.DANGER, 16),
        (AnomalyCategory.FACE_ABSENT, SourceAgent.GUARDIAN, 0.88, True, AlertSeverity.DANGER, 21),
    ]
    for idx, (category, agent, score, confirmed, severity, minute) in enumerate(timeline):
        anomaly = Anomaly(
            id=_demo_id(f"anomaly:live:0:{idx}"),
            session_id=sess0.id,
            source_agent=agent,
            category=category,
            score=score,
            reasons=[
                f"{agent.value} detected {category.value}",
                f"sustained signal at +{minute}m",
            ],
            evidence={"minute_offset": minute, "stage": "demo"},
            detected_at=_BASE_TIME + timedelta(minutes=minute),
            confirmed=confirmed,
        )
        session.add(anomaly)
        session.flush()
        summary.anomalies += 1
        # Raise a human-facing alert for every confirmed anomaly.
        if confirmed:
            session.add(
                Alert(
                    id=_demo_id(f"alert:live:0:{idx}"),
                    anomaly_id=anomaly.id,
                    session_id=sess0.id,
                    severity=severity,
                    message=f"{category.value.replace('_', ' ').title()} in {s0.full_name}'s session",
                    delivered_ws=True,
                    delivered_email=severity is AlertSeverity.DANGER,
                    created_at=_BASE_TIME + timedelta(minutes=minute, seconds=2),
                )
            )
            summary.alerts += 1
    count += 1

    # Student 1 — ACTIVE and clean (a healthy baseline session).
    s1 = students[1]
    session.add(
        ExamSession(
            id=_demo_id("session:live:1"),
            exam_id=exam.id,
            student_id=s1.id,
            paper_id=papers[s1.id].id,
            status=SessionStatus.ACTIVE,
            started_at=_BASE_TIME + timedelta(minutes=1),
            integrity_score=100.0,
        )
    )
    count += 1

    # Student 2 — ACTIVE with a single low-severity anomaly (no alert).
    s2 = students[2]
    sess2 = ExamSession(
        id=_demo_id("session:live:2"),
        exam_id=exam.id,
        student_id=s2.id,
        paper_id=papers[s2.id].id,
        status=SessionStatus.ACTIVE,
        started_at=_BASE_TIME + timedelta(minutes=2),
        integrity_score=86.0,
    )
    session.add(sess2)
    session.flush()
    session.add(
        Anomaly(
            id=_demo_id("anomaly:live:2:0"),
            session_id=sess2.id,
            source_agent=SourceAgent.SENTINEL,
            category=AnomalyCategory.TIMING,
            score=0.38,
            reasons=["Answered question 3 unusually fast"],
            evidence={"minute_offset": 5, "stage": "demo"},
            detected_at=_BASE_TIME + timedelta(minutes=5),
            confirmed=False,
        )
    )
    summary.anomalies += 1
    count += 1

    # Student 3 — NOT_STARTED (ready to begin, useful for a "start exam" demo).
    s3 = students[3]
    session.add(
        ExamSession(
            id=_demo_id("session:live:3"),
            exam_id=exam.id,
            student_id=s3.id,
            paper_id=papers[s3.id].id,
            status=SessionStatus.NOT_STARTED,
            integrity_score=100.0,
        )
    )
    count += 1

    return count


def _seed_completed_sessions(
    session: Session,
    exam: Exam,
    students: list[User],
    papers: dict[str, GeneratedPaper],
    summary: SeedSummary,
) -> int:
    """Create submitted sessions with graded answers for the completed exam."""
    count = 0
    for s_idx, student in enumerate(students):
        paper = papers[student.id]
        sess = ExamSession(
            id=_demo_id(f"session:completed:{s_idx}"),
            exam_id=exam.id,
            student_id=student.id,
            paper_id=paper.id,
            status=SessionStatus.SUBMITTED,
            started_at=_BASE_TIME - timedelta(days=1),
            submitted_at=_BASE_TIME - timedelta(days=1) + timedelta(minutes=45),
            integrity_score=92.0 - 4.0 * s_idx,
        )
        session.add(sess)
        session.flush()

        for q_idx, question in enumerate(paper.questions):
            # The first student answers everything correctly; others miss some.
            is_correct = s_idx == 0 or q_idx % 2 == 0
            response = question.answer_key if is_correct else "incorrect-demo-response"
            session.add(
                Answer(
                    session_id=sess.id,
                    question_id=question.id,
                    response=response,
                    time_spent_ms=30_000 + q_idx * 5_000,
                    is_correct=is_correct,
                    awarded_marks=question.max_marks if is_correct else 0.0,
                )
            )
            summary.answers += 1
        count += 1
    return count


# --- CLI entry point --------------------------------------------------------


def main() -> None:
    """Ensure the schema exists, seed the demo dataset, and print a summary."""
    create_all()
    factory = get_session_factory()
    session = factory()
    try:
        summary = seed(session)
    finally:
        session.close()
    print(summary.render())


if __name__ == "__main__":
    main()

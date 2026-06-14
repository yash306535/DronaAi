"""Exam analytics endpoint (Requirement 10).

Thin HTTP layer exposing the Analyst's persisted
:class:`~app.models.orm.ExamAnalytics` record:

| Method | Path | Role | Purpose |
|--------|------|------|---------|
| GET | ``/analytics/exams/{id}`` | admin | Full exam analytics (10.1-10.7) |
| GET | ``/analytics/exams/{id}/report.pdf`` | admin | Downloadable PDF report (P3) |

The Analyst (``app/agents/analyst.py``) aggregates a completed exam's results
into the analytics record on ``exam.completed`` and upserts it via
:class:`ExamAnalyticsRepository`. This route returns that persisted record so
the admin analytics dashboard can render the score distribution, difficulty
heatmap, and per-student suggestions. A 404 is returned when no analytics have
been produced for the exam yet (e.g. it is not complete).

The ``report.pdf`` route renders that same persisted record as a downloadable
PDF (cover, score summary, difficulty heatmap, per-student suggestions) via the
unit-testable :func:`app.services.pdf_report.render_exam_report_pdf` helper and
streams the bytes back with an ``application/pdf`` attachment disposition. It
also 404s when no analytics exist for the exam yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import AuthUser, require_role
from app.core.db import get_db
from app.core.errors import NotFoundError
from app.models.enums import Role
from app.repositories.analytics import ExamAnalyticsRepository
from app.schemas.analytics import ExamAnalyticsRead
from app.services.pdf_report import render_exam_report_pdf

router = APIRouter(prefix="/analytics", tags=["analytics"])

ANALYTICS_NOT_FOUND_CODE = "analytics_not_found"


@router.get("/exams/{exam_id}", response_model=ExamAnalyticsRead)
def get_exam_analytics(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
) -> ExamAnalyticsRead:
    """Return the persisted analytics for ``exam_id`` (admin only).

    Raises :class:`NotFoundError` (HTTP 404) when no analytics record exists for
    the exam yet — the Analyst produces one on ``exam.completed``.
    """
    row = ExamAnalyticsRepository(db).get_for_exam(exam_id)
    if row is None:
        raise NotFoundError(
            "No analytics are available for this exam yet.",
            code=ANALYTICS_NOT_FOUND_CODE,
        )
    return ExamAnalyticsRead.model_validate(row)


@router.get("/exams/{exam_id}/report.pdf")
def get_exam_report_pdf(
    exam_id: str,
    user: AuthUser = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_db),
) -> Response:
    """Stream the persisted analytics for ``exam_id`` as a PDF (admin only).

    Renders the same :class:`ExamAnalytics` record returned by
    :func:`get_exam_analytics` into a downloadable PDF via the unit-testable
    :func:`app.services.pdf_report.render_exam_report_pdf` helper. Raises
    :class:`NotFoundError` (HTTP 404) when no analytics record exists for the
    exam yet.
    """
    row = ExamAnalyticsRepository(db).get_for_exam(exam_id)
    if row is None:
        raise NotFoundError(
            "No analytics are available for this exam yet.",
            code=ANALYTICS_NOT_FOUND_CODE,
        )
    pdf_bytes = render_exam_report_pdf(row)
    filename = f"exam-{exam_id}-report.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


__all__ = ["router", "ANALYTICS_NOT_FOUND_CODE"]

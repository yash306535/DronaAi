"""PDF rendering for the Analyst exam analytics report (Requirement 10.1, P3).

This module turns a persisted :class:`~app.models.orm.ExamAnalytics` record (the
aggregate produced by the Analyst — score distribution, mean, anomaly count,
difficulty heatmap, and per-student reports/suggestions) into a downloadable
PDF document using ``reportlab``.

It is kept deliberately free of any web/HTTP concerns so it is trivially
unit-testable: :func:`render_exam_report_pdf` accepts a plain analytics-shaped
object and returns the PDF as ``bytes``. The analytics endpoint
(``app/api/analytics.py``) simply streams those bytes with an
``application/pdf`` content type and an attachment ``Content-Disposition``.

The input object only needs the read-side attributes of the analytics record:
``exam_id``, ``generated_at``, ``summary``, ``difficulty_heatmap`` and
``per_student`` (the SQLAlchemy ORM row and the ``ExamAnalyticsRead`` schema
both satisfy this). The section dicts are the free-form JSON shapes the Analyst
writes (see ``app/agents/analyst.py``); this renderer reads them defensively so
a partial/pending report still produces a valid PDF.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any, Protocol

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Brand colors (navy/crimson identity from the design system).
_NAVY = colors.HexColor("#1b2a4a")
_CRIMSON = colors.HexColor("#b3243b")
_NAVY_LIGHT = colors.HexColor("#5a6ba0")
_ROW_ALT = colors.HexColor("#eef1f7")


class AnalyticsLike(Protocol):
    """Structural type the renderer needs (ORM row or ``ExamAnalyticsRead``)."""

    exam_id: str
    generated_at: datetime
    summary: dict
    difficulty_heatmap: dict
    per_student: dict


def _styles() -> dict[str, ParagraphStyle]:
    """Return the paragraph styles used across the report."""
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}
    styles["title"] = ParagraphStyle(
        "DronaTitle",
        parent=base["Title"],
        textColor=_NAVY,
        fontSize=26,
        leading=30,
        spaceAfter=6,
    )
    styles["subtitle"] = ParagraphStyle(
        "DronaSubtitle",
        parent=base["Normal"],
        textColor=_CRIMSON,
        fontSize=13,
        leading=16,
        spaceAfter=4,
    )
    styles["meta"] = ParagraphStyle(
        "DronaMeta",
        parent=base["Normal"],
        textColor=_NAVY_LIGHT,
        fontSize=10,
        leading=14,
    )
    styles["h2"] = ParagraphStyle(
        "DronaH2",
        parent=base["Heading2"],
        textColor=_NAVY,
        fontSize=15,
        leading=18,
        spaceBefore=14,
        spaceAfter=6,
    )
    styles["h3"] = ParagraphStyle(
        "DronaH3",
        parent=base["Heading3"],
        textColor=_NAVY,
        fontSize=12,
        leading=15,
        spaceBefore=8,
        spaceAfter=2,
    )
    styles["body"] = ParagraphStyle(
        "DronaBody",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
    )
    styles["bullet"] = ParagraphStyle(
        "DronaBullet",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        leftIndent=12,
        bulletIndent=2,
    )
    return styles


def _fmt_ts(value: Any) -> str:
    """Render a ``generated_at`` value as a readable UTC string."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return str(value)


def _table_style(header_span: int) -> TableStyle:
    """Shared table style: navy header row, zebra striping, light grid."""
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (header_span - 1, 0), _NAVY),
            ("TEXTCOLOR", (0, 0), (header_span - 1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.4, _NAVY_LIGHT),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ROW_ALT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
    )


def _cover(analytics: AnalyticsLike, styles: dict[str, ParagraphStyle]) -> list:
    """Build the cover flowables: exam id + generated_at (Requirement 10.1)."""
    return [
        Spacer(1, 40 * mm),
        Paragraph("DRONA AI", styles["title"]),
        Paragraph("Post-Exam Analytics Report", styles["subtitle"]),
        Spacer(1, 8 * mm),
        Paragraph(f"Exam ID: {analytics.exam_id}", styles["meta"]),
        Paragraph(
            f"Generated at: {_fmt_ts(analytics.generated_at)}", styles["meta"]
        ),
    ]


def _summary_section(
    summary: dict, styles: dict[str, ParagraphStyle]
) -> list:
    """Score distribution + mean + anomaly count summary (Requirement 10.2)."""
    summary = summary or {}
    flow: list = [Paragraph("Score Summary", styles["h2"])]

    mean = summary.get("mean", 0.0)
    anomaly_count = summary.get("anomalyCount", 0)
    completed = summary.get("completedStudents", 0)
    flow.append(
        Paragraph(
            f"Mean score: <b>{mean}</b> &nbsp;&nbsp; "
            f"Flagged anomalies: <b>{anomaly_count}</b> &nbsp;&nbsp; "
            f"Completed students: <b>{completed}</b>",
            styles["body"],
        )
    )
    flow.append(Spacer(1, 4 * mm))

    distribution = summary.get("distribution") or {}
    flow.append(Paragraph("Score distribution", styles["h3"]))
    if distribution:
        rows = [["Score band", "Students"]]
        rows.extend([band, str(count)] for band, count in distribution.items())
        table = Table(rows, colWidths=[80 * mm, 40 * mm], hAlign="LEFT")
        table.setStyle(_table_style(2))
        flow.append(table)
    else:
        flow.append(Paragraph("No score distribution available.", styles["body"]))
    return flow


def _heatmap_section(
    heatmap: dict, styles: dict[str, ParagraphStyle]
) -> list:
    """Difficulty heatmap: topic -> accuracy/difficulty (Requirement 10.3)."""
    heatmap = heatmap or {}
    flow: list = [Paragraph("Difficulty Heatmap", styles["h2"])]
    topics = heatmap.get("topics") or {}
    if topics:
        rows = [["Topic", "Accuracy (%)", "Difficulty (%)"]]
        for topic, values in topics.items():
            values = values or {}
            rows.append(
                [
                    str(topic),
                    str(values.get("accuracy", 0.0)),
                    str(values.get("difficulty", 0.0)),
                ]
            )
        table = Table(
            rows, colWidths=[80 * mm, 40 * mm, 40 * mm], hAlign="LEFT"
        )
        table.setStyle(_table_style(3))
        flow.append(table)
    else:
        flow.append(Paragraph("No topic heatmap available.", styles["body"]))
    return flow


def _per_student_section(
    per_student: dict, styles: dict[str, ParagraphStyle]
) -> list:
    """Per-student reports + improvement suggestions (Requirement 10.4)."""
    per_student = per_student or {}
    flow: list = [Paragraph("Per-Student Reports", styles["h2"])]
    students = per_student.get("students") or {}
    if not students:
        flow.append(Paragraph("No student reports available.", styles["body"]))
        return flow

    for student_id, entry in students.items():
        entry = entry or {}
        flow.append(Paragraph(f"Student: {student_id}", styles["h3"]))
        flow.append(
            Paragraph(f"Score: <b>{entry.get('score', 0.0)}</b>", styles["body"])
        )

        topic_accuracy = entry.get("topicAccuracy") or {}
        if topic_accuracy:
            acc_text = ", ".join(
                f"{topic}: {pct}%" for topic, pct in topic_accuracy.items()
            )
            flow.append(Paragraph(f"Topic accuracy: {acc_text}", styles["body"]))

        suggestions = entry.get("suggestions") or []
        status = entry.get("suggestionsStatus")
        if suggestions:
            flow.append(Paragraph("Improvement suggestions:", styles["body"]))
            for suggestion in suggestions:
                flow.append(
                    Paragraph(f"&bull;&nbsp; {suggestion}", styles["bullet"])
                )
        elif status == "pending":
            flow.append(
                Paragraph(
                    "Improvement suggestions: <i>pending</i>", styles["body"]
                )
            )
        else:
            flow.append(
                Paragraph("Improvement suggestions: none.", styles["body"])
            )
        flow.append(Spacer(1, 3 * mm))
    return flow


def render_exam_report_pdf(analytics: AnalyticsLike) -> bytes:
    """Render ``analytics`` as a downloadable PDF and return the bytes.

    The document contains a cover (exam id + generated_at), the score summary
    (distribution + mean + anomaly count), the difficulty heatmap, and the
    per-student reports/suggestions (Requirement 10.1-10.4). The returned bytes
    are a complete PDF document beginning with the ``%PDF`` header.
    """
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"DRONA AI Exam Report {analytics.exam_id}",
        author="DRONA AI Analyst",
    )

    story: list = []
    story.extend(_cover(analytics, styles))
    story.append(PageBreak())
    story.extend(_summary_section(analytics.summary, styles))
    story.extend(_heatmap_section(analytics.difficulty_heatmap, styles))
    story.extend(_per_student_section(analytics.per_student, styles))

    doc.build(story)
    return buffer.getvalue()


__all__ = ["render_exam_report_pdf", "AnalyticsLike"]

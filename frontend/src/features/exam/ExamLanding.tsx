import { useEffect, useState } from "react";
import { BookOpen, Clock, FileText, PlayCircle } from "lucide-react";
import { apiClient, type ApiClient } from "@/lib/apiClient";
import type { ExamRead } from "@/types";
import { ExamView } from "@/features/exam/ExamView";

export interface ExamLandingProps {
  /** Injectable client (defaults to the shared instance); eases testing. */
  api?: Pick<ApiClient, "listAvailableExams" | "post">;
}

function resolveExamFromQuery(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("exam");
}

/**
 * Student exam entry. Lists the exams currently open to sit
 * (`GET /exams/available`) and, once one is chosen (or supplied via `?exam=`),
 * hands off to {@link ExamView} which starts the session and renders the paper.
 */
export function ExamLanding({ api = apiClient }: ExamLandingProps) {
  const [examId, setExamId] = useState<string | null>(() => resolveExamFromQuery());
  const [exams, setExams] = useState<ExamRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (examId) return;
    let cancelled = false;
    setLoading(true);
    api
      .listAvailableExams()
      .then((list) => {
        if (!cancelled) setExams(list);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load available exams.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, examId]);

  // Once an exam is selected, the exam portal takes over.
  if (examId) {
    return <ExamView examId={examId} api={api} />;
  }

  return (
    <section className="mx-auto max-w-2xl">
      <header className="mb-6">
        <h1 className="flex items-center gap-2 text-2xl font-semibold text-[#1a1d24]">
          <BookOpen className="h-6 w-6 text-navy-800" aria-hidden="true" />
          Available Exams
        </h1>
        <p className="mt-1 text-sm text-[#5a6270]">
          Choose an exam to begin. Your webcam will be used for live proctoring.
        </p>
      </header>

      {loading ? (
        <p className="text-sm text-[#5a6270]">Loading available exams…</p>
      ) : error ? (
        <p role="alert" className="rounded-md bg-bg-danger px-3 py-2 text-sm text-danger">
          {error}
        </p>
      ) : exams.length === 0 ? (
        <p className="rounded-lg border border-dashed border-[#cfd6e0] bg-white p-8 text-center text-sm text-[#8a93a2]">
          No exams are open right now. Check back when an exam goes live.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {exams.map((exam) => (
            <li
              key={exam.id}
              className="flex items-center gap-4 rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm"
            >
              <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-bg-info text-info">
                <FileText className="h-5 w-5" aria-hidden="true" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold text-[#1a1d24]">
                  {exam.title}
                </div>
                <div className="mt-0.5 flex items-center gap-3 text-xs text-[#5a6270]">
                  <span>{exam.subject}</span>
                  <span className="flex items-center gap-1">
                    <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                    {exam.duration_minutes} min
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setExamId(exam.id)}
                className="focus-ring inline-flex items-center gap-2 rounded-md bg-navy-800 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-navy-600"
              >
                <PlayCircle className="h-4 w-4" aria-hidden="true" />
                Start
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

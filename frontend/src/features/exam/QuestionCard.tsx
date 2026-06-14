import { useId } from "react";
import type { StudentQuestion } from "@/types";

export interface QuestionCardProps {
  question: StudentQuestion;
  /** The student's current response for this question ("" when unanswered). */
  value: string;
  /** Called whenever the response changes (drives autosave + telemetry). */
  onChange: (value: string) => void;
  /** Disable entry once the session is no longer active. */
  disabled?: boolean;
  /** Called when the question's answer entry receives focus (question_view). */
  onView?: () => void;
}

/**
 * Renders a single question's prompt + answer entry for the three supported
 * types (mcq / short / numerical). The question shape is the student-facing
 * {@link StudentQuestion}, which by construction carries NO answer key — only
 * the prompt, options text, topic, and marks (Requirement 5.3).
 */
export function QuestionCard({
  question,
  value,
  onChange,
  disabled = false,
  onView,
}: QuestionCardProps) {
  const groupId = useId();

  return (
    <article
      className="flex flex-col gap-3 rounded-lg border border-[#e3e8ee] bg-white p-4 shadow-sm"
      aria-labelledby={`${groupId}-prompt`}
      onFocus={onView}
    >
      <header className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-semibold uppercase tracking-wider text-[#8a93a2]">
          Question {question.index + 1}
          <span className="ml-2 text-[#8a93a2]">· {question.topic}</span>
        </span>
        <span className="text-xs text-[#8a93a2]">{question.max_marks} marks</span>
      </header>

      <p id={`${groupId}-prompt`} className="text-sm leading-relaxed text-[#1a1d24]">
        {question.prompt}
      </p>

      {question.type === "mcq" ? (
        <fieldset className="flex flex-col gap-2" disabled={disabled}>
          <legend className="sr-only">Select one option</legend>
          {(question.options ?? []).map((option, optionIndex) => {
            const optionId = `${groupId}-opt-${optionIndex}`;
            return (
              <label
                key={optionId}
                htmlFor={optionId}
                className="flex cursor-pointer items-center gap-2 rounded-md border border-[#cfd6e0] px-3 py-2 text-sm text-[#1a1d24] transition-colors hover:border-navy-600 hover:bg-[#f4f6f9]"
              >
                <input
                  id={optionId}
                  type="radio"
                  name={groupId}
                  value={option}
                  checked={value === option}
                  onChange={(e) => onChange(e.target.value)}
                  disabled={disabled}
                  className="focus-ring accent-navy-800"
                />
                <span>{option}</span>
              </label>
            );
          })}
        </fieldset>
      ) : question.type === "numerical" ? (
        <input
          type="number"
          inputMode="decimal"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          aria-label={`Answer for question ${question.index + 1}`}
          className="focus-ring w-full rounded-md border border-[#cfd6e0] bg-white px-3 py-2 text-[#1a1d24] placeholder:text-[#8a93a2]"
          placeholder="Enter a number"
        />
      ) : (
        <textarea
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          aria-label={`Answer for question ${question.index + 1}`}
          rows={4}
          className="focus-ring w-full resize-y rounded-md border border-[#cfd6e0] bg-white px-3 py-2 text-[#1a1d24] placeholder:text-[#8a93a2]"
          placeholder="Type your answer"
        />
      )}
    </article>
  );
}

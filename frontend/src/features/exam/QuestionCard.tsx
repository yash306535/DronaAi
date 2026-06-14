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
      className="flex flex-col gap-3 rounded-md border border-navy-600 bg-navy-800 p-4"
      aria-labelledby={`${groupId}-prompt`}
      onFocus={onView}
    >
      <header className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-semibold uppercase tracking-wider text-navy-400">
          Question {question.index + 1}
          <span className="ml-2 text-navy-400">· {question.topic}</span>
        </span>
        <span className="text-xs text-navy-400">{question.max_marks} marks</span>
      </header>

      <p id={`${groupId}-prompt`} className="text-sm leading-relaxed text-white">
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
                className="flex cursor-pointer items-center gap-2 rounded border border-navy-600 px-3 py-2 text-sm hover:border-navy-400"
              >
                <input
                  id={optionId}
                  type="radio"
                  name={groupId}
                  value={option}
                  checked={value === option}
                  onChange={(e) => onChange(e.target.value)}
                  disabled={disabled}
                  className="focus-ring"
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
          className="focus-ring w-full rounded-md border border-navy-600 bg-navy-900 px-3 py-2 text-white placeholder:text-navy-400"
          placeholder="Enter a number"
        />
      ) : (
        <textarea
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          aria-label={`Answer for question ${question.index + 1}`}
          rows={4}
          className="focus-ring w-full resize-y rounded-md border border-navy-600 bg-navy-900 px-3 py-2 text-white placeholder:text-navy-400"
          placeholder="Type your answer"
        />
      )}
    </article>
  );
}

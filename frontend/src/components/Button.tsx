import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/components/classNames";

/**
 * Button variants per design.md "Core Component Specs":
 * - primary:     navy-800 bg, white text, hover navy-600
 * - destructive: crimson-600 bg, white text, hover crimson-400
 */
export type ButtonVariant = "primary" | "secondary" | "destructive";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: "bg-navy-800 text-white hover:bg-navy-600",
  secondary:
    "border border-[#cfd6e0] bg-white text-[#1a1d24] hover:bg-[#f4f6f9]",
  destructive: "bg-crimson-600 text-white hover:bg-crimson-400",
};

/**
 * Primary action button. radius-md + shadow-sm with a visible keyboard focus
 * ring (Requirement 16.6 via the shared `.focus-ring` utility).
 */
export function Button({
  variant = "primary",
  type = "button",
  className,
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn(
        "focus-ring inline-flex items-center justify-center gap-2 rounded-md px-4 py-2",
        "text-sm font-medium shadow-sm transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-60",
        VARIANT_CLASSES[variant],
        className,
      )}
      {...props}
    />
  );
}

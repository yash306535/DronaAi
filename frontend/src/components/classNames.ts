// Tiny className combiner used by the shared UI components.
// Filters out falsy values and joins the rest with a single space.
export type ClassValue = string | false | null | undefined;

export function cn(...values: ClassValue[]): string {
  return values.filter(Boolean).join(" ");
}

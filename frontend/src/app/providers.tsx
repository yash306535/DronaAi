import type { ReactNode } from "react";

interface AppProvidersProps {
  children: ReactNode;
}

/**
 * Global application providers (theme, auth, query clients, etc.).
 * Placeholder shell — later tasks add concrete providers here.
 */
export function AppProviders({ children }: AppProvidersProps) {
  return <>{children}</>;
}

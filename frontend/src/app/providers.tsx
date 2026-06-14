import type { ReactNode } from "react";
import { AuthProvider } from "@/app/AuthContext";

interface AppProvidersProps {
  children: ReactNode;
}

/**
 * Global application providers. Currently wires the auth provider that resolves
 * the signed-in user and powers role-aware navigation in the app shell.
 */
export function AppProviders({ children }: AppProvidersProps) {
  return <AuthProvider>{children}</AuthProvider>;
}

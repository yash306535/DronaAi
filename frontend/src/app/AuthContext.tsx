import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { apiClient, type ApiClient } from "@/lib/apiClient";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";
import type { Role, UserRead } from "@/types";

interface AuthContextValue {
  /** The authenticated user, or null when signed out / still resolving. */
  user: UserRead | null;
  /** True while the initial profile fetch is in flight. */
  loading: boolean;
  /** Re-fetch the current user (e.g. right after a login). */
  refresh: () => Promise<void>;
  /** Clear tokens and drop the in-memory user. */
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export interface AuthProviderProps {
  children: ReactNode;
  /** Injectable client + store for tests. */
  api?: Pick<ApiClient, "me">;
  store?: TokenStore;
}

/**
 * Resolves and shares the authenticated user's profile across the app shell.
 * On mount (and whenever asked to refresh) it calls `GET /auth/me` when a token
 * is present, so the shell can show the user and gate role-specific navigation.
 */
export function AuthProvider({
  children,
  api = apiClient,
  store = tokenStore,
}: AuthProviderProps) {
  const [user, setUser] = useState<UserRead | null>(null);
  const [loading, setLoading] = useState<boolean>(
    () => store.getAccessToken() !== null,
  );

  const refresh = useCallback(async () => {
    if (!store.getAccessToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const profile = await api.me();
      setUser(profile);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, [api, store]);

  const logout = useCallback(() => {
    store.clear();
    setUser(null);
  }, [store]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading, refresh, logout }),
    [user, loading, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Access the auth context. Returns a safe default outside a provider. */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    return {
      user: null,
      loading: false,
      refresh: async () => {},
      logout: () => {},
    };
  }
  return ctx;
}

/** Home route for a role, mirrored from LoginView's redirect logic. */
export function homePathForRole(role: Role): string {
  switch (role) {
    case "admin":
      return "/dashboard";
    case "invigilator":
      return "/invigilator";
    default:
      return "/exam";
  }
}

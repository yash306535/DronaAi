// Fetch-based API client for the DRONA backend REST surface.
//
// Responsibilities:
// - Resolve the base URL from import.meta.env (default http://localhost:8000/api/v1).
// - Inject the Authorization: Bearer <access> header from the token store.
// - On a 401, attempt a single token refresh via /auth/refresh using the
//   stored refresh token, then retry the original request once. If refresh
//   fails, clear the store and redirect to the login page.
// - Expose login / refresh / me helpers plus generic get/post helpers.

import type { TokenPair, UserRead } from "@/types";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";

/** Default API base URL when no env override is provided. */
export const DEFAULT_API_BASE_URL = "http://localhost:8000/api/v1";

/** Default path the client redirects to when refresh fails. */
export const DEFAULT_LOGIN_PATH = "/login";

function resolveBaseUrl(): string {
  // import.meta.env is provided by Vite. Guard defensively for non-Vite envs.
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> })
    .env;
  const fromEnv = env?.VITE_API_BASE_URL;
  return (fromEnv && fromEnv.length > 0 ? fromEnv : DEFAULT_API_BASE_URL).replace(
    /\/+$/,
    "",
  );
}

/** Standard backend error envelope: `{ error: { code, message, requestId } }`. */
export interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
    requestId?: string;
  };
}

/** Error thrown for non-2xx responses, carrying the parsed envelope + status. */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    const envelope = body as ApiErrorBody | null;
    this.code = envelope?.error?.code;
    this.requestId = envelope?.error?.requestId;
  }
}

export interface ApiClientOptions {
  baseUrl?: string;
  store?: TokenStore;
  /** Custom fetch (defaults to global fetch); useful for tests. */
  fetchFn?: typeof fetch;
  /** Called when refresh fails; defaults to redirecting to the login path. */
  onAuthFailure?: () => void;
  loginPath?: string;
}

interface RequestOptions {
  /** Skip the Authorization header (e.g. for the public login endpoint). */
  auth?: boolean;
  /** Skip the refresh-on-401 retry (used internally for the refresh call). */
  retryOn401?: boolean;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

async function parseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  const text = await response.text();
  return text.length > 0 ? text : null;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly store: TokenStore;
  private readonly fetchFn: typeof fetch;
  private readonly onAuthFailure: () => void;
  // Shared in-flight refresh so concurrent 401s only trigger one refresh call.
  private refreshInFlight: Promise<boolean> | null = null;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? resolveBaseUrl()).replace(/\/+$/, "");
    this.store = options.store ?? tokenStore;
    this.fetchFn = options.fetchFn ?? globalThis.fetch.bind(globalThis);
    const loginPath = options.loginPath ?? DEFAULT_LOGIN_PATH;
    this.onAuthFailure =
      options.onAuthFailure ??
      (() => {
        if (typeof window !== "undefined") {
          window.location.assign(loginPath);
        }
      });
  }

  private buildUrl(path: string): string {
    if (/^https?:\/\//.test(path)) {
      return path;
    }
    return `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  }

  private buildHeaders(
    options: RequestOptions,
    hasBody: boolean,
  ): Headers {
    const headers = new Headers(options.headers);
    if (hasBody && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (options.auth !== false) {
      const access = this.store.getAccessToken();
      if (access) {
        headers.set("Authorization", `Bearer ${access}`);
      }
    }
    return headers;
  }

  /**
   * Attempt to refresh the access token using the stored refresh token.
   * Concurrent callers share a single in-flight refresh. Returns true on
   * success. On failure clears the store and invokes the auth-failure handler.
   */
  private async tryRefresh(): Promise<boolean> {
    if (this.refreshInFlight) {
      return this.refreshInFlight;
    }
    this.refreshInFlight = (async () => {
      const refreshToken = this.store.getRefreshToken();
      if (!refreshToken) {
        this.handleAuthFailure();
        return false;
      }
      try {
        const response = await this.fetchFn(this.buildUrl("/auth/refresh"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!response.ok) {
          this.handleAuthFailure();
          return false;
        }
        const pair = (await response.json()) as TokenPair;
        this.store.setTokens(pair.access_token, pair.refresh_token);
        return true;
      } catch {
        this.handleAuthFailure();
        return false;
      }
    })();
    try {
      return await this.refreshInFlight;
    } finally {
      this.refreshInFlight = null;
    }
  }

  private handleAuthFailure(): void {
    this.store.clear();
    this.onAuthFailure();
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options: RequestOptions = {},
  ): Promise<T> {
    const hasBody = body !== undefined;
    const init: RequestInit = {
      method,
      headers: this.buildHeaders(options, hasBody),
      ...(hasBody ? { body: JSON.stringify(body) } : {}),
      ...(options.signal ? { signal: options.signal } : {}),
    };

    let response = await this.fetchFn(this.buildUrl(path), init);

    // Refresh-on-401: retry exactly once after a successful refresh.
    if (
      response.status === 401 &&
      options.auth !== false &&
      options.retryOn401 !== false
    ) {
      const refreshed = await this.tryRefresh();
      if (refreshed) {
        const retryInit: RequestInit = {
          ...init,
          headers: this.buildHeaders(options, hasBody),
        };
        response = await this.fetchFn(this.buildUrl(path), retryInit);
      } else {
        const parsed = await parseBody(response);
        throw new ApiError(response.status, "Unauthorized", parsed);
      }
    }

    const parsed = await parseBody(response);
    if (!response.ok) {
      const envelope = parsed as ApiErrorBody | null;
      const message =
        envelope?.error?.message ?? `Request failed with status ${response.status}`;
      throw new ApiError(response.status, message, parsed);
    }
    return parsed as T;
  }

  // --- Generic helpers ------------------------------------------------------

  get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>("GET", path, undefined, options);
  }

  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("POST", path, body, options);
  }

  put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("PUT", path, body, options);
  }

  del<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>("DELETE", path, undefined, options);
  }

  // --- Auth helpers ---------------------------------------------------------

  /** Authenticate and persist the returned token pair (public endpoint). */
  async login(email: string, password: string): Promise<TokenPair> {
    const pair = await this.request<TokenPair>(
      "POST",
      "/auth/login",
      { email, password },
      { auth: false, retryOn401: false },
    );
    this.store.setTokens(pair.access_token, pair.refresh_token);
    return pair;
  }

  /** Explicitly rotate tokens via the refresh endpoint and persist the result. */
  async refresh(): Promise<TokenPair> {
    const refreshToken = this.store.getRefreshToken();
    if (!refreshToken) {
      throw new ApiError(401, "No refresh token available", null);
    }
    const pair = await this.request<TokenPair>(
      "POST",
      "/auth/refresh",
      { refresh_token: refreshToken },
      { auth: false, retryOn401: false },
    );
    this.store.setTokens(pair.access_token, pair.refresh_token);
    return pair;
  }

  /** Fetch the current authenticated user's profile. */
  me(): Promise<UserRead> {
    return this.get<UserRead>("/auth/me");
  }

  /** Clear stored tokens (local logout). */
  logout(): void {
    this.store.clear();
  }
}

/** Shared default client instance using the localStorage-backed token store. */
export const apiClient = new ApiClient();

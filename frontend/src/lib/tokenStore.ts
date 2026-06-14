// Small token store backed by localStorage for access + refresh tokens.
// Used by the API client to inject the Bearer header and to drive the
// refresh-on-401 flow. Reads/writes are guarded so they degrade gracefully in
// non-browser / restricted environments (e.g. SSR, tests without storage).

const ACCESS_KEY = "drona.accessToken";
const REFRESH_KEY = "drona.refreshToken";

function getStorage(): Storage | null {
  try {
    if (typeof localStorage === "undefined") {
      return null;
    }
    return localStorage;
  } catch {
    // Accessing localStorage can throw in sandboxed contexts.
    return null;
  }
}

function readKey(key: string): string | null {
  const storage = getStorage();
  if (!storage) {
    return null;
  }
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function writeKey(key: string, value: string | null): void {
  const storage = getStorage();
  if (!storage) {
    return;
  }
  try {
    if (value === null) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, value);
    }
  } catch {
    // Ignore quota / access errors; the store is best-effort.
  }
}

export interface TokenStore {
  getAccessToken(): string | null;
  getRefreshToken(): string | null;
  setTokens(accessToken: string, refreshToken: string): void;
  clear(): void;
}

/** Default localStorage-backed token store. */
export const tokenStore: TokenStore = {
  getAccessToken: () => readKey(ACCESS_KEY),
  getRefreshToken: () => readKey(REFRESH_KEY),
  setTokens: (accessToken: string, refreshToken: string) => {
    writeKey(ACCESS_KEY, accessToken);
    writeKey(REFRESH_KEY, refreshToken);
  },
  clear: () => {
    writeKey(ACCESS_KEY, null);
    writeKey(REFRESH_KEY, null);
  },
};

/**
 * Build an in-memory token store. Useful for tests and for components that
 * want an isolated store rather than the shared localStorage-backed one.
 */
export function createMemoryTokenStore(
  initial?: { accessToken?: string; refreshToken?: string },
): TokenStore {
  let access: string | null = initial?.accessToken ?? null;
  let refresh: string | null = initial?.refreshToken ?? null;
  return {
    getAccessToken: () => access,
    getRefreshToken: () => refresh,
    setTokens: (accessToken: string, refreshToken: string) => {
      access = accessToken;
      refresh = refreshToken;
    },
    clear: () => {
      access = null;
      refresh = null;
    },
  };
}

export const TOKEN_STORAGE_KEYS = {
  access: ACCESS_KEY,
  refresh: REFRESH_KEY,
} as const;

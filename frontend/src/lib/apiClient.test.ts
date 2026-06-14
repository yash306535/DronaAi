import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "@/lib/apiClient";
import { createMemoryTokenStore } from "@/lib/tokenStore";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const BASE = "http://test.local/api/v1";

describe("ApiClient refresh-on-401 flow", () => {
  it("refreshes the token and retries the original request once on 401", async () => {
    const store = createMemoryTokenStore({
      accessToken: "stale-access",
      refreshToken: "good-refresh",
    });

    const fetchFn = vi
      .fn<typeof fetch>()
      // 1. Original request -> 401
      .mockResolvedValueOnce(jsonResponse(401, { error: { code: "token_expired" } }))
      // 2. Refresh call -> new pair
      .mockResolvedValueOnce(
        jsonResponse(200, {
          access_token: "fresh-access",
          refresh_token: "fresh-refresh",
          token_type: "bearer",
        }),
      )
      // 3. Retried original request -> success
      .mockResolvedValueOnce(jsonResponse(200, { id: "u1", email: "a@b.c" }));

    const client = new ApiClient({
      baseUrl: BASE,
      store,
      fetchFn,
      onAuthFailure: () => {
        throw new Error("should not redirect on successful refresh");
      },
    });

    const result = await client.get<{ id: string }>("/auth/me");

    expect(result).toEqual({ id: "u1", email: "a@b.c" });
    expect(fetchFn).toHaveBeenCalledTimes(3);

    // The retried request must carry the refreshed access token.
    const retryInit = fetchFn.mock.calls[2][1] as RequestInit;
    const retryHeaders = retryInit.headers as Headers;
    expect(retryHeaders.get("Authorization")).toBe("Bearer fresh-access");

    // Store should hold the rotated tokens.
    expect(store.getAccessToken()).toBe("fresh-access");
    expect(store.getRefreshToken()).toBe("fresh-refresh");
  });

  it("clears tokens and invokes the auth-failure handler when refresh fails", async () => {
    const store = createMemoryTokenStore({
      accessToken: "stale-access",
      refreshToken: "bad-refresh",
    });

    const fetchFn = vi
      .fn<typeof fetch>()
      // 1. Original request -> 401
      .mockResolvedValueOnce(jsonResponse(401, { error: { code: "token_expired" } }))
      // 2. Refresh call -> 401 (refresh token invalid)
      .mockResolvedValueOnce(jsonResponse(401, { error: { code: "invalid_refresh_token" } }));

    const onAuthFailure = vi.fn();
    const client = new ApiClient({ baseUrl: BASE, store, fetchFn, onAuthFailure });

    await expect(client.get("/auth/me")).rejects.toBeInstanceOf(ApiError);

    expect(onAuthFailure).toHaveBeenCalledTimes(1);
    expect(store.getAccessToken()).toBeNull();
    expect(store.getRefreshToken()).toBeNull();
    // No retry of the original request occurred (only original + refresh).
    expect(fetchFn).toHaveBeenCalledTimes(2);
  });

  it("does not attempt refresh when the login call returns 401", async () => {
    const store = createMemoryTokenStore();
    const fetchFn = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(401, { error: { code: "invalid_credentials" } }));

    const onAuthFailure = vi.fn();
    const client = new ApiClient({ baseUrl: BASE, store, fetchFn, onAuthFailure });

    await expect(client.login("a@b.c", "password123")).rejects.toBeInstanceOf(ApiError);

    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(onAuthFailure).not.toHaveBeenCalled();
  });

  it("injects the Bearer header from the token store on authed requests", async () => {
    const store = createMemoryTokenStore({
      accessToken: "my-access",
      refreshToken: "my-refresh",
    });
    const fetchFn = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));

    const client = new ApiClient({ baseUrl: BASE, store, fetchFn });
    await client.get("/auth/me");

    const init = fetchFn.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer my-access");
  });
});

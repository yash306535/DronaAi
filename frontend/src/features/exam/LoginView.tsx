import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components";
import { apiClient, ApiError, type ApiClient } from "@/lib/apiClient";

export interface LoginViewProps {
  /** API client (defaults to the shared instance); injectable for tests. */
  api?: Pick<ApiClient, "login">;
  /** Path to navigate to after a successful login. Defaults to `/exam`. */
  redirectTo?: string;
}

/**
 * Student login view: email + password → `apiClient.login` (which persists the
 * access + refresh token pair), then routes into the exam portal.
 *
 * Login is the public entry point wired to the `/login` route. Credential
 * errors surface a non-disclosing message (the backend never reveals whether
 * the email or the password was wrong — Requirement 1.2).
 */
export function LoginView({ api = apiClient, redirectTo = "/exam" }: LoginViewProps) {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.login(email, password);
      navigate(redirectTo);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Incorrect email or password.");
      } else {
        setError("Unable to sign in right now. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="mx-auto max-w-sm">
      <h1 className="text-2xl font-semibold">Login</h1>
      <p className="mt-1 text-sm text-navy-400">
        Sign in to start your exam session.
      </p>

      <form className="mt-6 flex flex-col gap-4" onSubmit={handleSubmit} noValidate>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Email</span>
          <input
            type="email"
            name="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="focus-ring rounded-md border border-navy-600 bg-navy-800 px-3 py-2 text-white placeholder:text-navy-400"
            placeholder="you@example.com"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Password</span>
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="focus-ring rounded-md border border-navy-600 bg-navy-800 px-3 py-2 text-white placeholder:text-navy-400"
            placeholder="••••••••"
          />
        </label>

        {error && (
          <p role="alert" className="text-sm font-medium text-crimson-400">
            {error}
          </p>
        )}

        <Button type="submit" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </section>
  );
}

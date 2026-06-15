import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  AtSign,
  Bot,
  Eye,
  EyeOff,
  GraduationCap,
  Lock,
  LogIn,
  MonitorCheck,
  ScanFace,
  ShieldCheck,
  Sparkles,
  Zap,
} from "lucide-react";
import { Button } from "@/components";
import { apiClient, ApiError, type ApiClient } from "@/lib/apiClient";
import { homePathForRole, useAuth } from "@/app/AuthContext";

export interface LoginViewProps {
  /** API client (defaults to the shared instance); injectable for tests. */
  api?: Pick<ApiClient, "login" | "me">;
  /**
   * Explicit path to navigate to after a successful login. When omitted, the
   * destination is chosen from the authenticated user's role.
   */
  redirectTo?: string;
}

const HIGHLIGHTS = [
  { icon: ScanFace, text: "Two-stage live proctoring" },
  { icon: Bot, text: "Autonomous multi-agent crew" },
  { icon: Sparkles, text: "Explainable integrity scores" },
];

/** One-click demo identities. The student maps to the seeded account that has
 *  not yet started the live exam, so the take-exam flow works out of the box. */
const DEMO_ACCOUNTS = [
  { role: "Admin", email: "admin@drona.ai", password: "AdminPass123!" },
  { role: "Invigilator", email: "invigilator@drona.ai", password: "InvigilatorPass123!" },
  { role: "Student", email: "student4@drona.ai", password: "StudentPass123!" },
] as const;

/**
 * Login view: email + password → `apiClient.login`, then routes the user to
 * their role's home — admins to the dashboard, invigilators to the console,
 * students to the exam portal. Credential errors surface a non-disclosing
 * message (the backend never reveals which field was wrong — Requirement 1.2).
 */
export function LoginView({ api = apiClient, redirectTo }: LoginViewProps) {
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function doLogin(loginEmail: string, loginPassword: string) {
    setError(null);
    setSubmitting(true);
    try {
      await api.login(loginEmail, loginPassword);
      const profile = await api.me();
      await refresh();
      navigate(redirectTo ?? homePathForRole(profile.role));
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

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void doLogin(email, password);
  }

  function handleDemo(account: (typeof DEMO_ACCOUNTS)[number]) {
    setEmail(account.email);
    setPassword(account.password);
    void doLogin(account.email, account.password);
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      {/* Brand hero */}
      <div className="relative hidden flex-col justify-between overflow-hidden bg-brand-gradient p-10 text-white lg:flex">
        <div className="flex items-center gap-3">
          <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-white/15">
            <ShieldCheck className="h-7 w-7" aria-hidden="true" />
          </span>
        </div>

        <div className="max-w-md">
          <h2 className="text-3xl font-bold leading-tight">
            Autonomous intelligence for examination integrity.
          </h2>
          <p className="mt-4 text-white/80">
            A coordinated crew of agents generates unique papers, proctors every
            session live, and surfaces explainable fraud signals in real time.
          </p>
          <ul className="mt-8 space-y-3">
            {HIGHLIGHTS.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-center gap-3 text-white/90">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-white/15">
                  <Icon className="h-5 w-5" aria-hidden="true" />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>

        <p className="text-xs text-white/60">
          Far Away 2026 · Agentic &amp; Autonomous Systems
        </p>
      </div>

      {/* Login card */}
      <div className="flex items-center justify-center p-6">
        <section className="w-full max-w-sm">
          <div className="mb-8 flex items-center gap-2">
            <ShieldCheck className="h-7 w-7 text-navy-800" aria-hidden="true" />
            <span className="text-xl font-bold tracking-wide text-navy-900">
              DRONA AI
            </span>
          </div>

          <h1 className="text-2xl font-semibold text-[#1a1d24]">Login</h1>
          <p className="mt-1 text-sm text-[#5a6270]">
            Sign in to access your DRONA AI workspace.
          </p>

          <form className="mt-6 flex flex-col gap-4" onSubmit={handleSubmit} noValidate>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium text-[#1a1d24]">Email</span>
              <span className="relative">
                <AtSign
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#8a93a2]"
                  aria-hidden="true"
                />
                <input
                  type="email"
                  name="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="focus-ring w-full rounded-md border border-[#cfd6e0] bg-white py-2 pl-9 pr-3 text-[#1a1d24] placeholder:text-[#8a93a2]"
                  placeholder="you@example.com"
                />
              </span>
            </label>

            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium text-[#1a1d24]">Password</span>
              <span className="relative">
                <Lock
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#8a93a2]"
                  aria-hidden="true"
                />
                <input
                  type={showPassword ? "text" : "password"}
                  name="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="focus-ring w-full rounded-md border border-[#cfd6e0] bg-white py-2 pl-9 pr-10 text-[#1a1d24] placeholder:text-[#8a93a2]"
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  className="focus-ring absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-[#8a93a2] hover:text-[#5a6270]"
                >
                  {showPassword ? (
                    <EyeOff className="h-4 w-4" aria-hidden="true" />
                  ) : (
                    <Eye className="h-4 w-4" aria-hidden="true" />
                  )}
                </button>
              </span>
            </label>

            {error && (
              <p
                role="alert"
                className="flex items-center gap-2 rounded-md bg-bg-danger px-3 py-2 text-sm font-medium text-danger"
              >
                {error}
              </p>
            )}

            <Button type="submit" disabled={submitting} className="mt-1">
              <LogIn className="h-4 w-4" aria-hidden="true" />
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          <div className="mt-8">
            <div className="flex items-center gap-3 text-xs text-[#8a93a2]">
              <span className="h-px flex-1 bg-[#e3e8ee]" />
              <span className="flex items-center gap-1.5">
                <Zap className="h-3.5 w-3.5" aria-hidden="true" />
                Quick demo access
              </span>
              <span className="h-px flex-1 bg-[#e3e8ee]" />
            </div>
            <div className="mt-4 grid grid-cols-3 gap-2">
              {DEMO_ACCOUNTS.map((account) => (
                <button
                  key={account.role}
                  type="button"
                  onClick={() => handleDemo(account)}
                  disabled={submitting}
                  className="focus-ring flex flex-col items-center gap-1 rounded-lg border border-[#e3e8ee] bg-white px-2 py-3 text-xs font-medium text-[#1a1d24] transition-colors hover:border-navy-600 hover:bg-[#f4f6f9] disabled:opacity-60"
                >
                  {account.role === "Admin" ? (
                    <ShieldCheck className="h-5 w-5 text-navy-800" aria-hidden="true" />
                  ) : account.role === "Invigilator" ? (
                    <MonitorCheck className="h-5 w-5 text-navy-800" aria-hidden="true" />
                  ) : (
                    <GraduationCap className="h-5 w-5 text-navy-800" aria-hidden="true" />
                  )}
                  {account.role}
                </button>
              ))}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

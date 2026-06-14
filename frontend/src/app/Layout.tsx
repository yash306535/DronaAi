import { useEffect } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  BarChart3,
  LayoutDashboard,
  LogOut,
  type LucideIcon,
  MonitorCheck,
  PenSquare,
  ShieldCheck,
} from "lucide-react";
import { useAuth } from "@/app/AuthContext";
import { tokenStore } from "@/lib/tokenStore";
import { cn } from "@/components/classNames";
import type { Role } from "@/types";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  roles: Role[];
}

/** Primary navigation, filtered by the signed-in user's role. */
const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard, roles: ["admin"] },
  { to: "/analytics", label: "Analytics", icon: BarChart3, roles: ["admin"] },
  {
    to: "/invigilator",
    label: "Invigilator",
    icon: MonitorCheck,
    roles: ["invigilator", "admin"],
  },
  { to: "/exam", label: "Exam", icon: PenSquare, roles: ["student"] },
];

const ROLE_LABEL: Record<Role, string> = {
  admin: "Administrator",
  invigilator: "Invigilator",
  student: "Student",
};

function pageTitle(pathname: string): string {
  const match = NAV_ITEMS.find((item) => pathname.startsWith(item.to));
  return match?.label ?? "DRONA AI";
}

/**
 * Application shell: a light, icon-driven sidebar with role-aware navigation, a
 * top bar showing the active page and the signed-in user, and the routed
 * content outlet. The login route renders bare (no chrome); every other route
 * requires a session and redirects to `/login` when signed out.
 */
export function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, loading, logout } = useAuth();

  const isLogin = location.pathname === "/login";
  const hasToken = tokenStore.getAccessToken() !== null;

  // Guard: bounce to login when visiting an app route without a session.
  useEffect(() => {
    if (!isLogin && !hasToken && !loading) {
      navigate("/login", { replace: true });
    }
  }, [isLogin, hasToken, loading, navigate]);

  // The login screen owns the full viewport — no shell chrome.
  if (isLogin) {
    return (
      <div className="min-h-screen bg-[#f4f6f9]">
        <Outlet />
      </div>
    );
  }

  const role = user?.role;
  const navItems = role
    ? NAV_ITEMS.filter((item) => item.roles.includes(role))
    : [];
  const initials = (user?.full_name ?? user?.email ?? "?")
    .split(/\s+/)
    .slice(0, 2)
    .map((p) => p.charAt(0).toUpperCase())
    .join("");

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex min-h-screen bg-[#f4f6f9] text-[#1a1d24]">
      {/* Sidebar */}
      <aside className="hidden w-64 shrink-0 flex-col border-r border-[#e3e8ee] bg-white md:flex">
        <Link
          to="/"
          className="focus-ring m-3 flex items-center gap-3 rounded-lg bg-brand-gradient px-4 py-4 text-white"
        >
          <ShieldCheck className="h-7 w-7" aria-hidden="true" />
          <div className="leading-tight">
            <div className="text-base font-bold tracking-wide">DRONA AI</div>
            <div className="text-[11px] text-white/70">Exam Integrity</div>
          </div>
        </Link>

        <nav className="flex flex-1 flex-col gap-1 px-3 py-2">
          <p className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[#8a93a2]">
            Menu
          </p>
          {navItems.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "focus-ring flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-navy-800 text-white shadow-sm"
                    : "text-[#5a6270] hover:bg-[#f4f6f9] hover:text-[#1a1d24]",
                )
              }
            >
              <Icon className="h-5 w-5" aria-hidden="true" />
              {label}
            </NavLink>
          ))}
        </nav>

        {user && (
          <div className="m-3 rounded-lg border border-[#e3e8ee] bg-[#f9fafc] p-3">
            <div className="flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-navy-800 text-xs font-semibold text-white">
                {initials}
              </span>
              <div className="min-w-0 flex-1 leading-tight">
                <div className="truncate text-sm font-semibold">
                  {user.full_name}
                </div>
                <div className="truncate text-[11px] text-[#8a93a2]">
                  {ROLE_LABEL[user.role]}
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={handleLogout}
              className="focus-ring mt-3 flex w-full items-center justify-center gap-2 rounded-md border border-[#e3e8ee] bg-white px-3 py-2 text-sm font-medium text-[#5a6270] transition-colors hover:border-crimson-400 hover:text-crimson-600"
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
              Sign out
            </button>
          </div>
        )}
      </aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-4 border-b border-[#e3e8ee] bg-white px-6 py-3.5">
          <h1 className="text-lg font-semibold">{pageTitle(location.pathname)}</h1>
          <div className="ml-auto flex items-center gap-3">
            {user && (
              <span className="hidden items-center gap-2 rounded-full bg-[#f4f6f9] px-3 py-1.5 text-xs font-medium text-[#5a6270] sm:flex">
                <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
                {user.email}
              </span>
            )}
          </div>
        </header>

        <main className="flex-1 overflow-x-hidden p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

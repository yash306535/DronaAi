import { Link, Outlet } from "react-router-dom";

const NAV_LINKS = [
  { to: "/login", label: "Login" },
  { to: "/exam", label: "Exam" },
  { to: "/dashboard", label: "Dashboard" },
  { to: "/invigilator", label: "Invigilator" },
  { to: "/analytics", label: "Analytics" },
];

/**
 * Base application layout: brand header + nav + routed content outlet.
 * Later tasks replace this skeleton with the full design-system shell.
 */
export function Layout() {
  return (
    <div className="min-h-screen bg-navy-900 text-white">
      <header className="flex items-center gap-6 bg-navy-800 px-6 py-4">
        <span className="text-lg font-bold tracking-wider">DRONA AI</span>
        <nav className="flex gap-4 text-sm">
          {NAV_LINKS.map((link) => (
            <Link key={link.to} to={link.to} className="hover:text-crimson-400">
              {link.label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="p-6">
        <Outlet />
      </main>
    </div>
  );
}

import { createBrowserRouter, Navigate } from "react-router-dom";
import { Layout } from "@/app/Layout";
import { ExamView, LoginView } from "@/features/exam";
import { DashboardView } from "@/features/dashboard";
import { InvigilatorView } from "@/features/invigilator";
import { AnalyticsView } from "@/features/analytics";

/**
 * Placeholder route component. Each feature route is filled in by later tasks
 * (admin dashboard, invigilator console, analytics).
 */
function PlaceholderPage({ title }: { title: string }) {
  return (
    <section className="mx-auto max-w-md rounded-lg border border-[#e3e8ee] bg-white p-8 text-center shadow-sm">
      <h1 className="text-2xl font-semibold text-[#1a1d24]">{title}</h1>
      <p className="mt-2 text-[#5a6270]">
        The page you are looking for could not be found.
      </p>
    </section>
  );
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/login" replace /> },
      { path: "login", element: <LoginView /> },
      { path: "exam", element: <ExamView /> },
      { path: "dashboard", element: <DashboardView /> },
      { path: "invigilator", element: <InvigilatorView /> },
      { path: "analytics", element: <AnalyticsView /> },
      { path: "*", element: <PlaceholderPage title="Not Found" /> },
    ],
  },
]);

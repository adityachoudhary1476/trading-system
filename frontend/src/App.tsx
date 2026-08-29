import { useEffect } from "react";
import { Routes, Route, useNavigate } from "react-router-dom";
import { Header, Sidebar } from "@/components/layout/Sidebar";
import { DashboardPage } from "@/pages/Dashboard";
import { MarketsPage } from "@/pages/Markets";
import { SignalsPage } from "@/pages/Signals";
import { SystemPage } from "@/pages/System";

const KEYS: Record<string, string> = {
  "1": "/",
  "2": "/markets",
  "3": "/signals",
  "4": "/system",
};

export function App() {
  const navigate = useNavigate();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const dest = KEYS[e.key];
      if (dest) navigate(dest);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  return (
    <div className="app">
      <Header />
      <Sidebar />
      <div id="scrim" className="scrim" onClick={() => {
        document.getElementById("sidebar")?.classList.remove("open");
        document.getElementById("scrim")?.classList.remove("show");
      }} />
      <main className="app-main">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/markets" element={<MarketsPage />} />
          <Route path="/signals" element={<SignalsPage />} />
          <Route path="/system" element={<SystemPage />} />
          <Route path="*" element={<DashboardPage />} />
        </Routes>
      </main>
    </div>
  );
}

import { Routes, Route } from "react-router-dom";
import { Header, Sidebar } from "@/components/layout/Sidebar";
import { DashboardPage } from "@/pages/Dashboard";
import { MarketsPage } from "@/pages/Markets";
import { SignalsPage } from "@/pages/Signals";
import { SystemPage } from "@/pages/System";

export function App() {
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

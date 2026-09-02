import { useEffect } from "react";
import { Routes, Route, useNavigate } from "react-router-dom";
import { Header, Sidebar } from "@/components/layout/Sidebar";
import { DashboardPage } from "@/pages/Dashboard";
import { MarketsPage } from "@/pages/Markets";
import { SignalsPage } from "@/pages/Signals";
import { SystemPage } from "@/pages/System";
import { BrokerConnectionsPage } from "@/pages/BrokerConnections";
import { PaperTradingPage } from "@/pages/paper/PaperTrading";
import { PaperOverview } from "@/pages/paper/PaperOverview";
import { PaperDeployments } from "@/pages/paper/PaperDeployments";
import { PaperDeploymentDetail } from "@/pages/paper/PaperDeploymentDetail";
import { PaperStrategies } from "@/pages/paper/PaperStrategies";
import { PaperSessions } from "@/pages/paper/PaperSessions";
import { PaperPositions } from "@/pages/paper/PaperPositions";
import { PaperEvents } from "@/pages/paper/PaperEvents";
import { PaperRiskHealth } from "@/pages/paper/PaperRiskHealth";
import { PaperReports } from "@/pages/paper/PaperReports";
import { PaperResearch } from "@/pages/paper/PaperResearch";

const KEYS: Record<string, string> = {
  "1": "/",
  "2": "/markets",
  "3": "/signals",
  "4": "/system",
  "5": "/paper",
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
          <Route path="/broker" element={<BrokerConnectionsPage />} />
          <Route path="/paper" element={<PaperTradingPage />}>
            <Route index element={<PaperOverview />} />
            <Route path="overview" element={<PaperOverview />} />
            <Route path="deployments" element={<PaperDeployments />} />
            <Route path="deployments/:deploymentId" element={<PaperDeploymentDetail />} />
            <Route path="strategies/:strategyId" element={<PaperStrategies />} />
            <Route path="sessions" element={<PaperSessions />} />
            <Route path="sessions/:deploymentId" element={<PaperSessions />} />
            <Route path="positions" element={<PaperPositions />} />
            <Route path="positions/:deploymentId" element={<PaperPositions />} />
            <Route path="events" element={<PaperEvents />} />
            <Route path="events/:deploymentId" element={<PaperEvents />} />
            <Route path="risk" element={<PaperRiskHealth />} />
            <Route path="risk/:deploymentId" element={<PaperRiskHealth />} />
            <Route path="reports" element={<PaperReports />} />
            <Route path="reports/:deploymentId" element={<PaperReports />} />
            <Route path="research" element={<PaperResearch />} />
          </Route>
          <Route path="*" element={<DashboardPage />} />
        </Routes>
      </main>
    </div>
  );
}

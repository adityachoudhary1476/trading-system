import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { AppEnvironment } from "@/types";
import { dataSource } from "@/data/MarketDataSource";

interface AppState {
  selectedSymbol: string;
  setSelectedSymbol: (s: string) => void;
  env: AppEnvironment;
}

const Ctx = createContext<AppState | null>(null);

function buildEnvFromHealth(health: Record<string, unknown> | null): AppEnvironment {
  const mode = dataSource.mode;
  const environment = (health?.environment as AppEnvironment["environment"]) || "development";

  const paperExecRaw = health?.paper_execution as Record<string, unknown> | undefined;
  let paperExecution: AppEnvironment["paperExecution"] = "DISABLED";
  if (paperExecRaw && typeof paperExecRaw.enabled === "boolean") {
    if (paperExecRaw.enabled && paperExecRaw.running) {
      paperExecution = "RUNNING";
    } else if (paperExecRaw.enabled) {
      paperExecution = "ENABLED";
    } else {
      paperExecution = "DISABLED";
    }
  }

  const liveExecRaw = health?.live_execution as Record<string, unknown> | undefined;
  let liveExecution: AppEnvironment["liveExecution"] = "DISABLED";
  if (liveExecRaw && typeof liveExecRaw.enabled === "boolean") {
    liveExecution = liveExecRaw.enabled ? "ENABLED" : "DISABLED";
  }

  const autonomousPaperPipeline = health?.autonomous_paper_pipeline as AppEnvironment["autonomousPaperPipeline"] | undefined;
  const execution = liveExecution === "ENABLED" ? "ENABLED" : "DISABLED";

  return {
    mode,
    environment,
    dataSource: mode === "mock" ? "Mock" : "API",
    execution,
    paperExecution,
    liveExecution,
    autonomousPaperPipeline,
  };
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [selectedSymbol, setSelectedSymbol] = useState<string>("NSE:NIFTY50");
  const [env, setEnv] = useState<AppEnvironment>(() => buildEnvFromHealth(null));

  useEffect(() => {
    let alive = true;
    fetch("/health", { cache: "no-store" })
      .then((r) => r.ok ? r.json() : null)
      .then((data: Record<string, unknown> | null) => {
        if (alive) setEnv(buildEnvFromHealth(data));
      })
      .catch(() => {
        if (alive) setEnv((prev) => ({ ...prev }));
      });
    return () => { alive = false; };
  }, []);

  const value = useMemo<AppState>(
    () => ({ selectedSymbol, setSelectedSymbol, env }),
    [selectedSymbol, env],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp must be used within AppProvider");
  return v;
}

/** Live IST clock (for the header). Updates once per minute. */
export function useIndianClock(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);
  return now;
}

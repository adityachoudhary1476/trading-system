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

const ENV: AppEnvironment = {
  mode: dataSource.mode, // "mock" tonight; flips to "live" with real source
  environment: "development",
  dataSource: dataSource.mode === "mock" ? "Mock" : "WebSocket",
  execution: "DISABLED",
};

export function AppProvider({ children }: { children: ReactNode }) {
  const [selectedSymbol, setSelectedSymbol] = useState<string>("NSE:NIFTY50");
  const value = useMemo<AppState>(
    () => ({ selectedSymbol, setSelectedSymbol, env: ENV }),
    [selectedSymbol],
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

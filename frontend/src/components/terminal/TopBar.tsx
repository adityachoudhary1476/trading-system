"use client";

import { useState, useEffect } from "react";
import { useTerminal } from "@/store/terminal";
import { useQuote } from "@/data/useQuote";
import { paperApi } from "@/lib/paperApi";

function Clock({ tz, label }: { tz: string; label: string }) {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  if (!now) return null;
  return (
    <span className="dim">
      {label} {" "}
      <span className="text-[var(--text)]">
        {now.toLocaleTimeString("en-GB", { timeZone: tz, hour12: false })}
      </span>
    </span>
  );
}

function marketStateIST(): { label: string; open: boolean; phase: string } {
  const ist = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  const day = ist.getDay();
  const mins = ist.getHours() * 60 + ist.getMinutes();
  const preOpen = day >= 1 && day <= 5 && mins >= 9 * 60 && mins < 9 * 60 + 15;
  const regular = day >= 1 && day <= 5 && mins >= 9 * 60 + 15 && mins <= 15 * 60 + 30;
  const postMarket = day >= 1 && day <= 5 && mins > 15 * 60 + 30 && mins < 16 * 60;
  const open = preOpen || regular || postMarket;
  let label = "NSE CLOSED";
  let phase = "closed";
  if (preOpen) { label = "NSE PRE-MARKET"; phase = "pre_market"; }
  else if (regular) { label = "NSE LIVE"; phase = "regular"; }
  else if (postMarket) { label = "NSE POST-MARKET"; phase = "post_market"; }
  else if (day === 0 || day === 6) { label = "NSE WEEKEND"; phase = "holiday"; }
  return { label, open, phase };
}

export default function TopBar() {
  const setCommandOpen = useTerminal((s) => s.setCommandOpen);
  const activeSymbol = useTerminal((s) => s.activeSymbol);
  const { data: quote } = useQuote(activeSymbol);
  const market = marketStateIST();
  const [upstoxStatus, setUpstoxStatus] = useState<{ connected: boolean; latency: number | null }>({ connected: false, latency: null });

  useEffect(() => {
    const checkUpstox = async () => {
      const start = Date.now();
      try {
        const res = await fetch("/api/upstox/status", { cache: "no-store" });
        const data = await res.json();
        setUpstoxStatus({ connected: data.connected === true, latency: Date.now() - start });
      } catch {
        setUpstoxStatus({ connected: false, latency: null });
      }
    };
    checkUpstox();
    const interval = setInterval(checkUpstox, 30_000);
    return () => clearInterval(interval);
  }, []);

  const [paperEquity, setPaperEquity] = useState<string>("—");
  const [paperPnl, setPaperPnl] = useState<string>("—");
  const [botStatus, setBotStatus] = useState<{ state: string; running: boolean }>({ state: "unknown", running: false });

  useEffect(() => {
    const fetchPaper = async () => {
      try {
        const deployments = await paperApi.listDeployments({ limit: 1 });
        if (deployments.ok && deployments.data.deployments.length > 0) {
          const id = deployments.data.deployments[0].deployment_id;
          const dash = await paperApi.getDashboard(id);
          if (dash.ok) {
            setPaperEquity(`₹${dash.data.account.equity?.toLocaleString("en-IN") ?? "—"}`);
            const pnl = dash.data.performance.total_pnl ?? 0;
            setPaperPnl(`${pnl >= 0 ? "+" : ""}${pnl.toLocaleString("en-IN")}`);
          }
        }
      } catch { /* ignore */ }
    };
    const fetchBot = async () => {
      try {
        const res = await paperApi.getAutonomousBot();
        if (res.ok && res.data.bot) {
          setBotStatus({ state: res.data.bot.state, running: res.data.bot.state === "running" });
        }
      } catch { /* ignore */ }
    };
    fetchPaper();
    fetchBot();
    const interval = setInterval(() => { fetchPaper(); fetchBot(); }, 15_000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header className="flex items-center gap-3 px-3 h-8 bg-[var(--panel-2)] border-b border-[var(--border)] text-[11px] shrink-0">
      <span className="amber font-bold tracking-widest text-[12px]">FINOVA TERMINAL</span>
      <span className={market.open ? "up" : "down"}>● {market.label}</span>
      <Clock tz="Asia/Kolkata" label="IST" />
      <span className="dim">|</span>
      <Clock tz="America/New_York" label="NY" />
      <span className="dim">|</span>
      <Clock tz="Europe/London" label="LDN" />
      <span className="dim">|</span>
      <Clock tz="Asia/Tokyo" label="TYO" />
      <button
        className="term-btn flex-1 max-w-md text-left dim"
        onClick={() => setCommandOpen(true)}
      >
        {activeSymbol} — search symbol… <span className="float-right">⌘K</span>
      </button>
      <span className="dim ml-2 mx-1">
        {quote ? (
          <>
            <Flash value={quote.price} className="text-[var(--text)] font-bold">{`₹${quote.price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}</Flash>
            <Flash value={quote.changePct} className={`ml-2 ${quote.changePct && quote.changePct >= 0 ? "up" : "down"}`}>
              {quote.changePct !== undefined ? `${quote.changePct >= 0 ? "+" : ""}${quote.changePct.toFixed(2)}%` : "—"}
            </Flash>
          </>
        ) : (
          <span className="dim">Loading quote…</span>
        )}
      </span>
      <span className="dim ml-2 mx-1">
        Equity: <span className="text-[var(--text)] font-mono">{paperEquity}</span>
      </span>
      <span className="dim ml-2 mx-1">
        P&L: <span className={`font-mono ${paperPnl.startsWith("+") || paperPnl.startsWith("-") ? paperPnl.startsWith("+") ? "up" : "down" : "dim"}`}>{paperPnl}</span>
      </span>
      <span className={`dim ml-2 mx-1 ${botStatus.running ? "up" : "down"}`}>
        Bot: {botStatus.state.toUpperCase()}
      </span>
      <span className={`dim ml-2 mx-1 ${upstoxStatus.connected ? "up" : "down"}`}>
        Upstox: {upstoxStatus.connected ? `● ${upstoxStatus.latency}ms` : "○ OFFLINE"}
      </span>
    </header>
  );
}

function Flash({ value, className, children }: { value: number | undefined | null; className?: string; children: React.ReactNode }) {
  const [prev, setPrev] = useState<number | null>(null);
  useEffect(() => {
    if (value !== null && value !== undefined && prev !== null && prev !== value) {
      const el = document.querySelector(`.flash-${value}`);
      if (el) {
        el.classList.add("flash");
        setTimeout(() => el.classList.remove("flash"), 500);
      }
    }
    if (value !== null && value !== undefined) setPrev(value);
  }, [value]);
  return <span className={className}>{children}</span>;
}
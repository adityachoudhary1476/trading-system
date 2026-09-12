"use client";

import { useTerminal, type WidgetType } from "@/store/terminal";

const WIDGET_CATEGORIES = [
  {
    title: "MARKET DATA",
    items: [
      { type: "chart" as WidgetType, label: "CHART", key: "⌥1" },
      { type: "quote" as WidgetType, label: "QUOTE", key: "⌥2" },
      { type: "watchlist" as WidgetType, label: "WATCHLIST", key: "⌥3" },
      { type: "options" as WidgetType, label: "OPTIONS CHAIN", key: "⌥4" },
      { type: "market-regime" as WidgetType, label: "MARKET REGIME", key: "⌥5" },
    ],
  },
  {
    title: "TRADING",
    items: [
      { type: "positions" as WidgetType, label: "POSITIONS", key: "⌥6" },
      { type: "orders" as WidgetType, label: "ORDERS", key: "⌥7" },
      { type: "paper-pnl" as WidgetType, label: "PAPER P&L", key: "⌥8" },
    ],
  },
  {
    title: "RESEARCH & INTELLIGENCE",
    items: [
      { type: "news" as WidgetType, label: "NEWS", key: "⌥9" },
      { type: "signals" as WidgetType, label: "SIGNALS", key: "" },
      { type: "strategies" as WidgetType, label: "STRATEGIES", key: "" },
      { type: "tournament" as WidgetType, label: "TOURNAMENT", key: "" },
      { type: "backtest-comparison" as WidgetType, label: "BACKTEST vs PAPER", key: "" },
      { type: "ai-research" as WidgetType, label: "AI RESEARCH", key: "" },
    ],
  },
  {
    title: "SYSTEM",
    items: [
      { type: "events" as WidgetType, label: "EVENTS", key: "" },
      { type: "scheduler" as WidgetType, label: "SCHEDULER", key: "" },
      { type: "safety" as WidgetType, label: "SAFETY / KILL SWITCH", key: "" },
      { type: "upstox-health" as WidgetType, label: "UPSTOX HEALTH", key: "" },
    ],
  },
];

export default function Sidebar() {
  const addWidget = useTerminal((s) => s.addWidget);
  const resetWorkspace = useTerminal((s) => s.resetWorkspace);
  const activeSymbol = useTerminal((s) => s.activeSymbol);

  return (
    <nav className="w-32 bg-[var(--panel)] border-r border-[var(--border)] flex flex-col shrink-0 overflow-y-auto">
      <div className="p-2 border-b border-[var(--border)]">
        <div className="font-bold text-[11px] tracking-wider text-[var(--text)]">FINOVA</div>
        <div className="dim text-[9px] uppercase tracking-wider">PAPER TERMINAL</div>
      </div>

      <div className="px-2 py-1 border-b border-[var(--border)]">
        <div className="dim text-[10px] uppercase tracking-wider mb-1">Active Symbol</div>
        <div className="text-[var(--text)] font-mono text-[11px] truncate">{activeSymbol}</div>
      </div>

      {WIDGET_CATEGORIES.map((cat) => (
        <div key={cat.title} className="px-2 py-1">
          <div className="dim text-[9px] uppercase tracking-wider mb-1 px-1">{cat.title}</div>
          {cat.items.map((item) => (
            <button
              key={item.type}
              onClick={() => addWidget(item.type)}
              className="w-full text-left px-2 py-1.5 text-[11px] hover:bg-[#1a1a1a] hover:text-[var(--amber)] flex justify-between transition-colors"
              title={`Add ${item.label} widget${item.key ? ` (${item.key})` : ""}`}
            >
              <span>{item.label}</span>
              {item.key && <span className="dim text-[9px]">{item.key}</span>}
            </button>
          ))}
        </div>
      ))}

      <div className="mt-auto border-t border-[var(--border)] p-2">
        <button
          onClick={resetWorkspace}
          className="w-full text-left px-2 py-1.5 text-[11px] dim hover:text-[var(--down)]"
        >
          RESET LAYOUT
        </button>
      </div>
    </nav>
  );
}
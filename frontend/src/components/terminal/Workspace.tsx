"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { Responsive, GridLayout } from "react-grid-layout";
import WidthProvider from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import { useTerminal, type WidgetInstance, type WidgetType } from "@/store/terminal";
import { ChartWidget } from "./widgets/ChartWidget";
import { QuoteWidget } from "./widgets/QuoteWidget";
import { WatchlistWidget } from "./widgets/WatchlistWidget";
import { NewsWidget } from "./widgets/NewsWidget";
import { OptionsWidget } from "./widgets/OptionsWidget";
import { PositionsWidget } from "./widgets/PositionsWidget";
import { OrdersWidget } from "./widgets/OrdersWidget";
import { EventsWidget } from "./widgets/EventsWidget";
import { SignalsWidget } from "./widgets/SignalsWidget";
import { SchedulerWidget } from "./widgets/SchedulerWidget";
import { MarketRegimeWidget } from "./widgets/MarketRegimeWidget";
import { StrategiesWidget } from "./widgets/StrategiesWidget";
import { TournamentWidget } from "./widgets/TournamentWidget";
import { BacktestComparisonWidget } from "./widgets/BacktestComparisonWidget";
import { SafetyWidget } from "./widgets/SafetyWidget";
import { UpstoxHealthWidget } from "./widgets/UpstoxHealthWidget";
import { PaperPnlWidget } from "./widgets/PaperPnlWidget";
import { AiResearchWidget } from "./widgets/AiResearchWidget";

const ResponsiveGridLayout = WidthProvider(Responsive(GridLayout));

const WIDGET_COMPONENTS: Record<WidgetType, React.ComponentType<{ widget: WidgetInstance }>> = {
  chart: ChartWidget,
  quote: QuoteWidget,
  watchlist: WatchlistWidget,
  news: NewsWidget,
  options: OptionsWidget,
  positions: PositionsWidget,
  orders: OrdersWidget,
  events: EventsWidget,
  signals: SignalsWidget,
  scheduler: SchedulerWidget,
  "market-regime": MarketRegimeWidget,
  strategies: StrategiesWidget,
  tournament: TournamentWidget,
  "backtest-comparison": BacktestComparisonWidget,
  safety: SafetyWidget,
  "upstox-health": UpstoxHealthWidget,
  "paper-pnl": PaperPnlWidget,
  "ai-research": AiResearchWidget,
};

const WIDGET_TITLES: Record<WidgetType, string> = {
  chart: "Chart",
  quote: "Quote",
  watchlist: "Watchlist",
  news: "News",
  options: "Option Chain",
  positions: "Positions",
  orders: "Orders",
  events: "Events",
  signals: "Signals",
  scheduler: "Scheduler",
  "market-regime": "Market Regime",
  strategies: "Strategies",
  tournament: "Tournament",
  "backtest-comparison": "Backtest vs Paper",
  safety: "Safety / Kill Switch",
  "upstox-health": "Upstox Health",
  "paper-pnl": "Paper P&L",
  "ai-research": "AI Research",
};

const SYMBOL_AWARE_TYPES: WidgetType[] = ["chart", "quote", "options", "positions", "orders", "signals", "paper-pnl"];

function WidgetBody({ widget }: { widget: WidgetInstance }) {
  const Component = WIDGET_COMPONENTS[widget.type];
  if (!Component) return <div className="p-2 dim">Unknown widget: {widget.type}</div>;
  return <Component widget={widget} />;
}

function SymbolTag({ widget, activeSymbol }: { widget: WidgetInstance; activeSymbol: string }) {
  const setWidgetSymbol = useTerminal((s) => s.setWidgetSymbol);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const shown = widget.linked ? activeSymbol : widget.symbol ?? activeSymbol;

  useEffect(() => {
    if (!editing) return;
    setDraft(shown);
    requestAnimationFrame(() => inputRef.current?.select());
  }, [editing, shown]);

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value.toUpperCase())}
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            const v = draft.trim();
            if (v) setWidgetSymbol(widget.id, v);
            setEditing(false);
          }
          if (e.key === "Escape") setEditing(false);
        }}
        onBlur={() => setEditing(false)}
        className="ml-2 w-20 !border-0 !border-b !border-[var(--amber-dim)] bg-transparent text-[var(--text)] px-0 py-0 text-[12px] leading-none outline-none"
      />
    );
  }

  return (
    <span
      className="ml-2 text-[var(--text)] cursor-pointer hover:text-[var(--amber)]"
      title="Click to set this widget's ticker"
      onMouseDown={(e) => e.stopPropagation()}
      onClick={() => setEditing(true)}
    >
      {shown}
    </span>
  );
}

export default function Workspace() {
  const widgets = useTerminal((s) => s.widgets);
  const layout = useTerminal((s) => s.layout);
  const setLayout = useTerminal((s) => s.setLayout);
  const removeWidget = useTerminal((s) => s.removeWidget);
  const toggleLinked = useTerminal((s) => s.toggleLinked);
  const activeSymbol = useTerminal((s) => s.activeSymbol);
  const [mounted, setMounted] = useState(false);

  useEffect(() => { setMounted(true); }, []);

  const onLayoutChange = useCallback((newLayout: { i: string; x: number; y: number; w: number; h: number }[]) => {
    setLayout(newLayout.map(({ i, x, y, w, h }) => ({ i, x, y, w, h })));
  }, [setLayout]);

  if (!mounted) {
    return <div className="flex-1 flex items-center justify-center dim">Initializing workspace…</div>;
  }

  return (
    <ResponsiveGridLayout
      className="layout flex-1"
      layouts={{ lg: layout }}
      breakpoints={{ lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 }}
      cols={{ lg: 12, md: 10, sm: 6, xs: 4, xxs: 2 }}
      rowHeight={30}
      margin={[4, 4]}
      containerPadding={[4, 4]}
      draggableHandle=".panel-title"
      onLayoutChange={onLayoutChange}
      preventCollision={false}
      compactType="vertical"
    >
      {widgets.map((w) => (
        <div key={w.id} data-grid={{ i: w.id, x: w.x, y: w.y, w: w.w, h: w.h, minW: 3, minH: 3 }}>
          <div className="terminal-panel h-full flex flex-col">
            <div className="panel-title flex items-center justify-between px-2 py-1 border-b border-[var(--border)] bg-[var(--bg-elev)] shrink-0">
              <span className="flex items-center gap-2">
                <span className="font-mono text-[11px] text-[var(--text-dim)]">{WIDGET_TITLES[w.type]}</span>
                {SYMBOL_AWARE_TYPES.includes(w.type) && <SymbolTag widget={w} activeSymbol={activeSymbol} />}
              </span>
              <span className="flex items-center gap-1">
                {SYMBOL_AWARE_TYPES.includes(w.type) && (
                  <button
                    title={w.linked ? "Linked to active symbol (click to unlink)" : "Unlinked (click to link)"}
                    onMouseDown={(e) => e.stopPropagation()}
                    onClick={() => toggleLinked(w.id)}
                    className={`p-1 rounded transition-colors ${w.linked ? "text-[var(--amber)]" : "dim hover:text-[var(--amber)]"}`}
                  >
                    ⛓
                  </button>
                )}
                <button
                  onMouseDown={(e) => e.stopPropagation()}
                  onClick={() => removeWidget(w.id)}
                  className="dim hover:text-[var(--down)] p-1 rounded transition-colors"
                  title="Remove widget"
                >
                  ✕
                </button>
              </span>
            </div>
            <div className="flex-1 overflow-auto min-h-0">
              <WidgetBody widget={w} />
            </div>
          </div>
        </div>
      ))}
    </ResponsiveGridLayout>
  );
}
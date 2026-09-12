import { create } from "zustand";
import { persist } from "zustand/middleware";

export type WidgetType =
  | "chart"
  | "quote"
  | "watchlist"
  | "news"
  | "options"
  | "positions"
  | "orders"
  | "events"
  | "signals"
  | "scheduler"
  | "market-regime"
  | "strategies"
  | "tournament"
  | "backtest-comparison"
  | "safety"
  | "upstox-health"
  | "paper-pnl"
  | "ai-research";

export interface WidgetInstance {
  id: string;
  type: WidgetType;
  x: number;
  y: number;
  w: number;
  h: number;
  symbol?: string;
  linked?: boolean;
  config?: Record<string, unknown>;
}

export interface LayoutItem {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
  minW?: number;
  minH?: number;
  maxW?: number;
  maxH?: number;
  static?: boolean;
}

interface TerminalState {
  activeSymbol: string;
  setActiveSymbol: (symbol: string) => void;

  widgets: WidgetInstance[];
  addWidget: (type: WidgetType, symbol?: string) => void;
  removeWidget: (id: string) => void;
  updateWidget: (id: string, updates: Partial<WidgetInstance>) => void;
  setWidgetSymbol: (id: string, symbol: string) => void;
  toggleLinked: (id: string) => void;

  layout: LayoutItem[];
  setLayout: (layout: LayoutItem[]) => void;
  resetWorkspace: () => void;

  commandOpen: boolean;
  setCommandOpen: (open: boolean) => void;

  // Bottom terminal tabs
  bottomTab: "trades" | "events" | "scheduler" | "signals" | "errors" | "pnl" | "safety";
  setBottomTab: (tab: TerminalState["bottomTab"]) => void;
}

const DEFAULT_WIDGETS: WidgetInstance[] = [
  { id: "chart-1", type: "chart", x: 0, y: 0, w: 8, h: 12, symbol: "NSE:NIFTY50", linked: true },
  { id: "quote-1", type: "quote", x: 8, y: 0, w: 4, h: 5, symbol: "NSE:NIFTY50", linked: true },
  { id: "options-1", type: "options", x: 8, y: 5, w: 4, h: 7, symbol: "NSE:NIFTY50", linked: true },
  { id: "watchlist-1", type: "watchlist", x: 0, y: 12, w: 4, h: 8 },
  { id: "news-1", type: "news", x: 4, y: 12, w: 4, h: 8 },
  { id: "positions-1", type: "positions", x: 8, y: 12, w: 4, h: 8 },
];

const DEFAULT_LAYOUT: LayoutItem[] = DEFAULT_WIDGETS.map((w) => ({
  i: w.id,
  x: w.x,
  y: w.y,
  w: w.w,
  h: w.h,
  minW: 3,
  minH: 3,
}));

export const useTerminal = create<TerminalState>()(
  persist(
    (set) => ({
      activeSymbol: "NSE:NIFTY50",
      setActiveSymbol: (symbol) => set({ activeSymbol: symbol }),

      widgets: DEFAULT_WIDGETS,
      addWidget: (type, symbol) => {
        const id = `${type}-${Date.now()}`;
        const newWidget: WidgetInstance = {
          id,
          type,
          x: 0,
          y: 0,
          w: type === "chart" ? 8 : 4,
          h: type === "chart" ? 12 : 8,
          symbol: symbol || "NSE:NIFTY50",
          linked: type !== "watchlist" && type !== "news" && type !== "scheduler" && type !== "market-regime" && type !== "strategies" && type !== "tournament" && type !== "backtest-comparison" && type !== "safety" && type !== "upstox-health" && type !== "paper-pnl",
        };
        set((state) => ({
          widgets: [...state.widgets, newWidget],
          layout: [
            ...state.layout,
            { i: id, x: 0, y: 0, w: newWidget.w, h: newWidget.h, minW: 3, minH: 3 },
          ],
        }));
      },
      removeWidget: (id) =>
        set((state) => ({
          widgets: state.widgets.filter((w) => w.id !== id),
          layout: state.layout.filter((l) => l.i !== id),
        })),
      updateWidget: (id, updates) =>
        set((state) => ({
          widgets: state.widgets.map((w) => (w.id === id ? { ...w, ...updates } : w)),
        })),
      setWidgetSymbol: (id, symbol) =>
        set((state) => ({
          widgets: state.widgets.map((w) => (w.id === id ? { ...w, symbol } : w)),
        })),
      toggleLinked: (id) =>
        set((state) => ({
          widgets: state.widgets.map((w) =>
            w.id === id ? { ...w, linked: !w.linked } : w
          ),
        })),

      layout: DEFAULT_LAYOUT,
      setLayout: (layout) => set({ layout }),
      resetWorkspace: () => set({ widgets: DEFAULT_WIDGETS, layout: DEFAULT_LAYOUT }),

      commandOpen: false,
      setCommandOpen: (open) => set({ commandOpen: open }),

      bottomTab: "trades",
      setBottomTab: (tab) => set({ bottomTab: tab }),
    }),
    {
      name: "finova-terminal-workspace",
      partialize: (state) => ({
        widgets: state.widgets,
        layout: state.layout,
        activeSymbol: state.activeSymbol,
      }),
    }
  )
);

export function useWidgetSymbol(widget: WidgetInstance) {
  const activeSymbol = useTerminal((s) => s.activeSymbol);
  return widget.linked ? activeSymbol : widget.symbol ?? activeSymbol;
}
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTerminal } from "@/store/terminal";
import { useApp } from "@/store/AppContext";
import { paperApi } from "@/lib/paperApi";

interface CommandItem {
  id: string;
  label: string;
  description: string;
  category: string;
  keywords: string[];
  action: () => void;
}

const CATEGORIES = [
  { id: "symbols", label: "SYMBOLS", icon: "📈" },
  { id: "watchlists", label: "WATCHLISTS", icon: "⭐" },
  { id: "charts", label: "CHARTS", icon: "📊" },
  { id: "options", label: "OPTIONS", icon: "⛓" },
  { id: "strategies", label: "STRATEGIES", icon: "🧠" },
  { id: "bots", label: "BOTS", icon: "🤖" },
  { id: "research", label: "RESEARCH", icon: "🔬" },
  { id: "reports", label: "REPORTS", icon: "📄" },
  { id: "settings", label: "SETTINGS", icon: "⚙" },
] as const;

export default function CommandPalette() {
  const { commandOpen, setCommandOpen } = useTerminal();
  const { selectedSymbol, setSelectedSymbol } = useApp();
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [items, setItems] = useState<CommandItem[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (commandOpen) {
      setQuery("");
      setSelectedIndex(0);
      const input = inputRef.current;
      if (input) input.focus();
    }
  }, [commandOpen]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!commandOpen) return;
      if (e.key === "Escape") {
        setCommandOpen(false);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((prev) => Math.min(prev + 1, filteredItems.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((prev) => Math.max(prev - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (filteredItems[selectedIndex]) {
          filteredItems[selectedIndex].action();
          setCommandOpen(false);
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [commandOpen, selectedIndex]);

  const filteredItems = useMemo(() => {
    if (!query.trim()) return items;
    const q = query.toLowerCase();
    return items.filter(
      (item) =>
        item.label.toLowerCase().includes(q) ||
        item.description.toLowerCase().includes(q) ||
        item.keywords.some((k) => k.toLowerCase().includes(q))
    );
  }, [items, query]);

  // Build command items
  useEffect(() => {
    const buildItems = async () => {
      const cmdItems: CommandItem[] = [];

      // Symbols - common NSE symbols
      const symbols = [
        "NSE:NIFTY50",
        "NSE:NIFTYBANK",
        "NSE:FINNIFTY",
        "NSE:RELIANCE",
        "NSE:TCS",
        "NSE:HDFCBANK",
        "NSE:ICICIBANK",
        "NSE:INFY",
        "NSE:ITC",
        "NSE:SBIN",
        "NSE:BHARTIARTL",
        "NSE:KOTAKBANK",
      ];
      symbols.forEach((sym) => {
        cmdItems.push({
          id: `symbol-${sym}`,
          label: sym.replace("NSE:", ""),
          description: `Switch to ${sym}`,
          category: "symbols",
          keywords: [sym.replace("NSE:", ""), "symbol", "ticker"],
          action: () => setSelectedSymbol(sym),
        });
      });

      // Watchlists
      cmdItems.push({
        id: "watchlist-add",
        label: "Add Symbol to Watchlist",
        description: "Add a new symbol to your watchlist",
        category: "watchlists",
        keywords: ["watchlist", "add", "symbol"],
        action: () => {
          const sym = prompt("Enter symbol (e.g., NSE:RELIANCE):");
          if (sym) {
            const stored = localStorage.getItem("finova-watchlist");
            const list = stored ? JSON.parse(stored) : [];
            if (!list.includes(sym.toUpperCase())) {
              list.push(sym.toUpperCase());
              localStorage.setItem("finova-watchlist", JSON.stringify(list));
            }
          }
        },
      });

      // Charts
      ["1D", "5D", "1M", "6M", "1Y", "5Y"].forEach((tf) => {
        cmdItems.push({
          id: `chart-${tf}`,
          label: `${tf} Chart`,
          description: `Open ${tf} chart for ${selectedSymbol}`,
          category: "charts",
          keywords: ["chart", tf.toLowerCase(), "timeframe"],
          action: () => {
            const { addWidget } = useTerminal.getState();
            addWidget("chart", selectedSymbol);
          },
        });
      });

      // Options
      cmdItems.push({
        id: "options-chain",
        label: "Open Option Chain",
        description: "Open NIFTY option chain widget",
        category: "options",
        keywords: ["options", "chain", "nifty", "strike"],
        action: () => {
          const { addWidget } = useTerminal.getState();
          addWidget("options", "NSE:NIFTY50");
        },
      });

      // Strategies
      try {
        const strategies = await paperApi.getStrategies();
        if (strategies.ok && strategies.data) {
          strategies.data.forEach((s: any) => {
            cmdItems.push({
              id: `strategy-${s.strategy_id}`,
              label: s.spec_name,
              description: `${s.name} · ${s.symbol} · ${s.timeframe}`,
              category: "strategies",
              keywords: ["strategy", s.name, s.spec_name],
              action: () => {
                window.location.href = `/paper/strategies/${s.name}`;
              },
            });
          });
        }
      } catch {
        /* ignore */
      }

      // Bots
      cmdItems.push(
        {
          id: "bot-start",
          label: "Start Autonomous Bot",
          description: "Start the autonomous trading bot",
          category: "bots",
          keywords: ["bot", "start", "autonomous", "run"],
          action: async () => {
            await paperApi.setAutonomousBotLifecycle("start");
          },
        },
        {
          id: "bot-pause",
          label: "Pause Autonomous Bot",
          description: "Pause the autonomous trading bot",
          category: "bots",
          keywords: ["bot", "pause", "autonomous", "stop"],
          action: async () => {
            await paperApi.setAutonomousBotLifecycle("pause");
          },
        },
        {
          id: "bot-scan",
          label: "Run Market Scan",
          description: "Trigger an immediate market scan",
          category: "bots",
          keywords: ["scan", "market", "signals"],
          action: async () => {
            await paperApi.getAutonomousScan();
          },
        },
        {
          id: "bot-decide",
          label: "Run Decisions",
          description: "Generate trading decisions from current scan",
          category: "bots",
          keywords: ["decide", "decisions", "trade"],
          action: async () => {
            await paperApi.getAutonomousDecisions();
          },
        }
      );

      // Research
      cmdItems.push(
        {
          id: "research-artifacts",
          label: "View Research Artifacts",
          description: "Open AI Research widget - Artifacts tab",
          category: "research",
          keywords: ["research", "artifacts", "ai"],
          action: () => {
            const { addWidget } = useTerminal.getState();
            addWidget("ai-research");
          },
        },
        {
          id: "research-evolution",
          label: "Strategy Evolution Lab",
          description: "Open AI Research widget - Evolution tab",
          category: "research",
          keywords: ["evolution", "lab", "genetic"],
          action: () => {
            const { addWidget } = useTerminal.getState();
            addWidget("ai-research");
          },
        }
      );

      // Reports
      cmdItems.push(
        {
          id: "report-overview",
          label: "Paper Trading Overview",
          description: "Open paper trading overview page",
          category: "reports",
          keywords: ["overview", "paper", "trading"],
          action: () => {
            window.location.href = "/paper/overview";
          },
        },
        {
          id: "report-deployments",
          label: "Paper Deployments",
          description: "View all paper deployments",
          category: "reports",
          keywords: ["deployments", "paper"],
          action: () => {
            window.location.href = "/paper/deployments";
          },
        },
        {
          id: "report-autonomous",
          label: "Autonomous Center",
          description: "Open autonomous trading operations center",
          category: "reports",
          keywords: ["autonomous", "center", "operations"],
          action: () => {
            window.location.href = "/paper/autonomous";
          },
        }
      );

      // Settings
      cmdItems.push(
        {
          id: "settings-reset",
          label: "Reset Workspace Layout",
          description: "Reset all widget positions to default",
          category: "settings",
          keywords: ["reset", "layout", "workspace", "default"],
          action: () => {
            const { resetWorkspace } = useTerminal.getState();
            resetWorkspace();
          },
        },
        {
          id: "settings-theme",
          label: "Toggle Theme (not implemented)",
          description: "Switch between dark/light theme",
          category: "settings",
          keywords: ["theme", "dark", "light"],
          action: () => alert("Theme toggle not yet implemented"),
        }
      );

      setItems(cmdItems);
    };
    buildItems();
  }, [selectedSymbol]);

  if (!commandOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-20">
      <div className="bg-[var(--bg-elev)] border border-[var(--border)] rounded-lg shadow-[0_0_0_1px_rgba(0,0,0,0.5),0_20px_40px_rgba(0,0,0,0.4)] w-[600px] max-w-[90vw] overflow-hidden animate-in fade-in-20 duration-150">
        <div className="p-3 border-b border-[var(--border)]">
          <div className="flex items-center gap-2">
            <span className="dim text-[11px]">⌘K</span>
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSelectedIndex(0);
              }}
              placeholder="Search commands, symbols, widgets…"
              className="flex-1 bg-[var(--bg-elev-2)] border border-[var(--border)] rounded px-3 py-2 text-[var(--text)] text-[13px] outline-none focus:border-[var(--amber)]"
              autoFocus
            />
            <span className="dim text-[11px]">{filteredItems.length} results</span>
          </div>
        </div>
        <div className="max-h-[50vh] overflow-auto">
          {CATEGORIES.map((cat) => {
            const catItems = filteredItems.filter((i) => i.category === cat.id);
            if (catItems.length === 0) return null;
            return (
              <div key={cat.id}>
                <div className="px-3 py-1.5 bg-[var(--bg-elev-2)] border-b border-[var(--border-soft)]">
                  <span className="flex items-center gap-2 text-[10px] uppercase tracking-wider dim">
                    <span>{cat.icon}</span>
                    <span>{cat.label}</span>
                    <span className="ml-auto text-[var(--amber)]">{catItems.length}</span>
                  </span>
                </div>
                {catItems.slice(0, 8).map((item) => {
                  const globalIndex = filteredItems.indexOf(item);
                  return (
                    <button
                      key={item.id}
                      onClick={() => {
                        item.action();
                        setCommandOpen(false);
                      }}
                      className={`w-full px-3 py-2 text-left hover:bg-[#1a1a1a] transition-colors flex items-center gap-3 ${
                        selectedIndex === globalIndex ? "bg-[var(--accent-soft)]" : ""
                      }`}
                    >
                      <span className="font-mono text-[12px] text-[var(--text)] min-w-[120px]">{item.label}</span>
                      <span className="dim text-[11px] flex-1 truncate">{item.description}</span>
                      <span className="dim text-[9px]">{item.category.toUpperCase()}</span>
                    </button>
                  );
                })}
              </div>
            );
          })}
          {filteredItems.length === 0 && query && (
            <div className="p-4 text-center dim">
              No matches for "{query}"
            </div>
          )}
          {filteredItems.length === 0 && !query && (
            <div className="p-4 text-center dim">
              Type to search… (try "NIFTY", "chart", "bot", "strategy")
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
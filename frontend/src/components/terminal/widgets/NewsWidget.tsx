"use client";

import { useQuery } from "@tanstack/react-query";
import { useState, useMemo } from "react";
import { apiGet } from "@/lib/api";
import { useWidgetSymbol, type WidgetInstance } from "@/store/terminal";

type NewsItem = {
  title: string;
  link: string;
  publisher: string;
  publishedAt: string | null;
  source: string;
  symbols?: string[];
};

type NewsResponse = {
  items: NewsItem[];
  cached: boolean;
  fetchedAt: number;
};

const RSS_SOURCES = [
  { name: "Moneycontrol", url: "https://www.moneycontrol.com/rss/latestnews.xml" },
  { name: "Economic Times", url: "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms" },
  { name: "Business Standard", url: "https://www.business-standard.com/rss/markets-106.rss" },
  { name: "LiveMint", url: "https://www.livemint.com/rss/markets" },
  { name: "Zee Business", url: "https://www.zeebiz.com/rss/market-news" },
];

function dedupeNews(items: NewsItem[]): NewsItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = item.title.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 60);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function filterBySymbol(items: NewsItem[], symbol: string | "GLOBAL"): NewsItem[] {
  if (symbol === "GLOBAL") return items;
  const sym = symbol.replace("NSE:", "").toUpperCase();
  return items.filter((item) =>
    item.symbols?.some((s) => s.toUpperCase().includes(sym)) ||
    item.title.toUpperCase().includes(sym)
  );
}

export function NewsWidget({ widget }: { widget: WidgetInstance }) {
  const widgetSymbol = useWidgetSymbol(widget);
  const [mode, setMode] = useState<"symbol" | "global">("symbol");
  const [selectedSources, setSelectedSources] = useState<string[]>(
    () => {
      try {
        const stored = localStorage.getItem("finova-news-sources");
        return stored ? JSON.parse(stored) : RSS_SOURCES.map((s) => s.name);
      } catch {
        return RSS_SOURCES.map((s) => s.name);
      }
    }
  );

  const { data, isLoading, error } = useQuery({
    queryKey: ["news", mode, widgetSymbol, selectedSources.join(",")],
    queryFn: async () => {
      // In a real implementation, this would fetch from a backend that aggregates RSS
      // For now, we'll simulate with a mock response
      const res = await apiGet<NewsResponse>(mode === "symbol" 
        ? `/api/news?symbol=${encodeURIComponent(widgetSymbol)}` 
        : "/api/news"
      ).catch(() => ({ items: [], cached: false, fetchedAt: Date.now() }));
      return res;
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const items = useMemo(() => {
    if (!data?.items) return [];
    let filtered = dedupeNews(data.items);
    filtered = filterBySymbol(filtered, mode === "symbol" ? widgetSymbol : "GLOBAL");
    return filtered;
  }, [data?.items, mode, widgetSymbol]);

  const toggleSource = (name: string) => {
    setSelectedSources((prev) =>
      prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]
    );
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex gap-1 p-1 shrink-0 border-b border-[var(--border)]">
        <button
          className={`term-btn ${mode === "symbol" ? "active" : ""}`}
          onClick={() => setMode("symbol")}
        >
          {widgetSymbol.replace("NSE:", "")}
        </button>
        <button
          className={`term-btn ${mode === "global" ? "active" : ""}`}
          onClick={() => setMode("global")}
        >
          GLOBAL
        </button>
        <span className="flex-1" />
        <details className="relative">
          <summary className="term-btn text-[10px] px-2 py-1 cursor-pointer list-none">
            Sources ({selectedSources.length})
            <span className="ml-1">▼</span>
          </summary>
          <div className="absolute right-0 top-full mt-1 bg-[var(--bg-elev)] border border-[var(--border)] rounded p-2 min-w-[180px] z-10">
            {RSS_SOURCES.map((src) => (
              <label key={src.name} className="flex items-center gap-2 text-[11px] py-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={selectedSources.includes(src.name)}
                  onChange={() => toggleSource(src.name)}
                  className="accent-[var(--amber)]"
                />
                {src.name}
              </label>
            ))}
            <button
              onClick={() => {
                const all = RSS_SOURCES.map((s) => s.name);
                setSelectedSources(all.length === selectedSources.length ? [] : all);
                localStorage.setItem("finova-news-sources", JSON.stringify(
                  all.length === selectedSources.length ? [] : all
                ));
              }}
              className="w-full text-left text-[10px] dim mt-1 px-1 py-0.5"
            >
              {selectedSources.length === RSS_SOURCES.length ? "Deselect All" : "Select All"}
            </button>
          </div>
        </details>
      </div>

      {isLoading && <div className="p-2 dim">Loading news…</div>}
      {error && <div className="p-2 down">Error: {(error as Error).message}</div>}

      <div className="flex-1 overflow-auto">
        {items.length === 0 ? (
          <div className="p-4 text-center dim">
            No news items found
            {mode === "symbol" && <div className="text-[11px] mt-1">Try GLOBAL mode or check back later</div>}
          </div>
        ) : (
          <div className="divide-y divide-[var(--border-soft)]">
            {items.slice(0, 50).map((item, i) => (
              <a
                key={i}
                href={item.link}
                target="_blank"
                rel="noopener noreferrer"
                className="block px-2 py-2 hover:bg-[#161616] transition-colors"
              >
                <div className="truncate text-[12px] leading-snug mb-1">{item.title}</div>
                <div className="flex items-center gap-2 text-[10px] dim">
                  <span className="font-mono">{item.source}</span>
                  {item.publishedAt && (
                    <span>
                      · {new Date(item.publishedAt).toLocaleString(undefined, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  )}
                </div>
              </a>
            ))}
          </div>
        )}
      </div>

      <div className="p-1 border-t border-[var(--border)] dim text-[10px] text-center shrink-0">
        {data?.cached ? "Cached" : "Live"} · Updated {data?.fetchedAt ? new Date(data.fetchedAt).toLocaleTimeString() : "—"} ·{" "}
        <span className="text-[var(--amber)]">Note: News is informational only — never triggers trades</span>
      </div>
    </div>
  );
}
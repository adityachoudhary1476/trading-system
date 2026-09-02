import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * These tests assert that the Dashboard's user-facing timeframe options are
 * EXACTLY the keys accepted by the Vercel OHLCV handler
 * (frontend/api/market/ohlcv.ts). The handler is the source of truth — the
 * Dashboard must never expose (or default to) a timeframe the handler
 * rejects with 400 unsupported_timeframe.
 */

function readSource(relPath: string): string {
  // tests live in frontend/src/pages/__tests__, so the workspace root is
  // three levels up from this file.
  return readFileSync(
    join(process.cwd(), relPath),
    "utf8",
  );
}

const DASHBOARD_SRC = readSource("src/pages/Dashboard.tsx");
const HANDLER_SRC = readSource("api/market/ohlcv.ts");

function extractArrayLiteral(source: string, name: string): string[] {
  const re = new RegExp(`const\\s+${name}\\s*=\\s*\\[([^\\]]*)\\]`);
  const m = source.match(re);
  if (!m) throw new Error(`Could not find const ${name} = [ ... ] in source`);
  const items = Array.from(m[1].matchAll(/"([^"]+)"/g)).map((x) => x[1]);
  return items;
}

function extractUseStateStringLiteral(source: string, name: string): string {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(
    `const\\s+\\[${escaped}[^\\]]*\\]\\s*=\\s*useState<\\w+>\\("([^"]+)"\\)`,
  );
  const m = source.match(re);
  if (!m) throw new Error(`Could not find useState binding for ${name}`);
  return m[1];
}

function extractHandlerKeys(source: string): string[] {
  const re = /const\s+INTERVAL_MAP\s*:\s*Record<string,\s*\{[^}]*\}>\s*=\s*\{([\s\S]*?)\n\}/;
  const m = source.match(re);
  if (!m) throw new Error("Could not find INTERVAL_MAP in ohlcv.ts");
  const keys = Array.from(m[1].matchAll(/"([^"]+)":\s*\{/g)).map((x) => x[1]);
  return keys;
}

describe("Dashboard timeframe contract (mirrors Upstox/Vercel OHLCV support)", () => {
  it("default timeframe is a key the Vercel OHLCV handler accepts", () => {
    const defaultTf = extractUseStateStringLiteral(DASHBOARD_SRC, "tf");
    const handlerKeys = extractHandlerKeys(HANDLER_SRC);
    expect(handlerKeys).toContain(defaultTf);
  });

  it("only exposes timeframe buttons the Vercel OHLCV handler accepts", () => {
    const exposed = extractArrayLiteral(DASHBOARD_SRC, "TIMEFRAMES");
    const handlerKeys = extractHandlerKeys(HANDLER_SRC);
    for (const t of exposed) {
      expect(handlerKeys).toContain(t);
    }
  });

  it("default timeframe is also present in the exposed TIMEFRAMES list", () => {
    const defaultTf = extractUseStateStringLiteral(DASHBOARD_SRC, "tf");
    const exposed = extractArrayLiteral(DASHBOARD_SRC, "TIMEFRAMES");
    expect(exposed).toContain(defaultTf);
  });

  it("does not expose any of the previously-broken unsupported timeframes", () => {
    // These timeframes were exposed by the previous Dashboard implementation
    // and rejected with 400 unsupported_timeframe by the Vercel handler.
    const exposed = extractArrayLiteral(DASHBOARD_SRC, "TIMEFRAMES");
    for (const unsupported of ["5m", "15m", "1h", "4h"]) {
      expect(exposed).not.toContain(unsupported);
    }
  });

  it("exposes at least one of the canonical supported keys", () => {
    // The Vercel handler accepts these four user-facing keys. The
    // Dashboard should expose a meaningful subset that includes at least
    // 1D (the default-friendly intraday) and one of the other three.
    const exposed = extractArrayLiteral(DASHBOARD_SRC, "TIMEFRAMES");
    expect(exposed).toContain("1D");
    const hasAnother = ["1m", "1W", "1M"].some((k) => exposed.includes(k));
    expect(hasAnother).toBe(true);
  });
});

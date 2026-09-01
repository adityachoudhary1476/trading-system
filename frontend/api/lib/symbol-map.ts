/**
 * Internal symbol → Upstox instrument key mapping.
 *
 * Internal format: "NSE:SBIN", "NSE:NIFTY50"
 * Upstox format:   "NSE_EQ|SBIN", "NSE_INDEX|NIFTY50"
 *
 * Mirrors the Python symbol_map.py logic for the server-side API.
 */

export function toUpstoxSymbol(internalSymbol: string): string {
  const parts = internalSymbol.split(":");
  if (parts.length !== 2) {
    throw new Error(`Invalid internal symbol format: ${internalSymbol}`);
  }
  const [exchange, symbol] = parts;
  const ex = exchange.toUpperCase();

  if (symbol === "NIFTY50" || symbol === "BANKNIFTY" || symbol === "FINNIFTY" || symbol.endsWith("INDEX")) {
    return `${ex}_INDEX|${symbol}`;
  }

  return `${ex}_EQ|${symbol}`;
}

export function parseInternalSymbol(internalSymbol: string): { exchange: string; symbol: string } {
  const parts = internalSymbol.split(":");
  if (parts.length !== 2) {
    throw new Error(`Invalid internal symbol format: ${internalSymbol}`);
  }
  return { exchange: parts[0].toUpperCase(), symbol: parts[1] };
}

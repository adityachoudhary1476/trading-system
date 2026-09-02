/**
 * Internal symbol → Upstox instrument key mapping.
 *
 * Internal format: "NSE:SBIN", "NSE:NIFTY50"
 * Upstox V2 format:  "NSE_EQ|INE062A01020", "NSE_INDEX|Nifty 50"
 *
 * Mirrors the Python symbol_map.py logic for the server-side API.
 * Known index names use the exact Upstox V2 instrument_key strings.
 * Known equities use their ISIN-based instrument keys.
 */
const ISIN_MAP: Record<string, string> = {
  NSE_SBIN: "INE062A01020",
  NSE_RELIANCE: "INE002A01018",
  NSE_INFY: "INE009A01021",
  NSE_TCS: "INE007A01025",
  NSE_HDFCBANK: "INE040A01034",
  NSE_ICICIBANK: "INE090A01021",
  NSE_KOTAKBANK: "INE237A01028",
  NSE_AXISBANK: "INE238A01034",
  NSE_LT: "INE018A01030",
  NSE_WIPRO: "INE075A01022",
};

const INDEX_NAME_MAP: Record<string, string> = {
  NIFTY50: "Nifty 50",
  BANKNIFTY: "Nifty Bank",
  FINNIFTY: "Nifty Fin Service",
};

export function toUpstoxSymbol(internalSymbol: string): string {
  const parts = internalSymbol.split(":");
  if (parts.length !== 2) {
    throw new Error(`Invalid internal symbol format: ${internalSymbol}`);
  }
  const [exchange, symbol] = parts;
  const ex = exchange.toUpperCase();
  const sym = symbol.toUpperCase();

  if (sym in INDEX_NAME_MAP) {
    return `${ex}_INDEX|${INDEX_NAME_MAP[sym]}`;
  }

  const isin = ISIN_MAP[`${ex}_${sym}`];
  if (isin) {
    return `${ex}_EQ|${isin}`;
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
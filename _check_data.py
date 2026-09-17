import sqlite3, pandas as pd
conn = sqlite3.connect('data/market_data.db')

print("=== All symbols (provider, timeframe, symbol, n_bars, date_range) ===")
symbols = pd.read_sql("""
SELECT provider, timeframe, symbol, COUNT(*) as n_bars,
       MIN(timestamp) as first_ts, MAX(timestamp) as last_ts
FROM market_data
GROUP BY provider, timeframe, symbol
ORDER BY provider, timeframe, symbol
""", conn)
for _, r in symbols.iterrows():
    print(f"  {r['provider']:14s} {r['timeframe']:4s} {r['symbol']:20s} {r['n_bars']:5d}  {r['first_ts']} -> {r['last_ts']}")

print(f"\nTotal rows: {len(pd.read_sql('SELECT 1 FROM market_data', conn))}")

# Check overlap for upstox 1d (most promising cross-section)
print("\n=== Upstox 1d symbols coverage ===")
upstox = pd.read_sql("""
SELECT symbol, COUNT(*) as n_bars, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts
FROM market_data WHERE provider='upstox' AND timeframe='1d'
GROUP BY symbol ORDER BY symbol
""", conn)
print(upstox.to_string())

conn.close()

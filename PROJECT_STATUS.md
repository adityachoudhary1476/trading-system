# Project Status

Observed at project start (Day 1). This is the environment as found — not an
aspiration.

## Environment

| Item | Value |
|---|---|
| OS | Windows 10 (10.0.19045) |
| Architecture | AMD64 |
| Python | 3.11.16 (used via per-project venv `.venv`) |
| CPU | 8 logical cores |
| RAM | ~8 GB total (8,362,713,088 bytes) |
| GPU | Intel Iris Xe (integrated, **no CUDA** — unfit for local LLM training/inference at scale) |
| Disk | C: 221 GB total, 92 GB free |
| Git | 2.55.0.windows.4 (present) |
| Docker | **Not installed** |
| Ollama / local LLM runtime | **Not installed** |
| Node.js | v24.19.0 (present, unused) |
| Network | Outbound HTTPS works (pip, public APIs reachable) |

## Existing software

- No pre-existing trading/quant project was found in the home directory.
- A fresh project was created at `C:\Users\Owner\trading-system`.
- Python dependencies installed: pandas 3.0.5, numpy 2.4.6, SQLAlchemy 2.0.52,
  python-dotenv, requests, tabulate, pydantic, pytest 9.1.1.

## Available resources

- Free, key-less public market data (Binance REST) — verified reachable.
- SQLite for local storage (sufficient for Day 1 single-user research).
- 92 GB disk and 8 GB RAM are adequate for daily-bar research over many symbols.

## Assumptions

- Researcher runs this on a single Windows machine; no containerization needed yet.
- Day 1 is research-only: no live money, no broker, no execution.
- Future AI analyst will run against structured market context, not raw feeds.
- Environment variables / a `.env` file hold all secrets (none required on Day 1).

## Potential limitations

- **No GPU** → any future local LLM must be small (e.g. quantized 7–13B) or
  hosted remotely; heavy inference is not viable locally.
- **No Docker** → can't easily replicate a containerized broker/sandbox yet;
  install if broker testing is needed.
- **No local LLM runtime** → AI analyst component is deferred until a model
  (Ollama/remote) is available; the contract (`MarketView`) is defined now.
- **Binance = crypto only** → no equities/forex coverage on Day 1.
- **8 GB RAM** → very large in-memory backtests should be chunked.
- Windows path quirks: run Python via the project `.venv`; use `PYTHONPATH=src`.

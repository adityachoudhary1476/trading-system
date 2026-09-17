## Notes

### Reality-Check Phase 2 COMPLETE (revised)
Repo is a **MONOREPO** at `C:\Users\Owner\OneDrive\Desktop\trading-system`.

**CORRECTIONS to Phase 2 (stale):**
- Tests: 276 → **2491 all passing** (exit 0)
- Execution: "none" → **paper-only** (PaperBroker, safety boundary INTACT)
- Data: 1 instrument → **18+ instruments** across multiple TFs
- Frontend: "not wired" → **IS wired**
- Cost model: "missing" → **EXISTS** (research/costs.py:IndiaTransactionCostModel)
- FYERS token: **still expired**

**NEW findings:**
- **CI MISSING**: No .github/workflows/ — AGENTS.md claim is FALSE
- **Evidence forensics**: fabricated evidence detected + quarantined (REJECTED/score=37.0 vs fake PAPER_APPROVED)
- **Paper sessions inactive**: 0 bars, 0 signals, 0 orders across all 6 sessions
- backend/data/market_data.db: 0 rows (empty)
- Phase 7 safety layer: robust (kill switch, validation, idempotency, emergency invariants)

**Legacy sandbox (ZAM8) bugs still valid** (root-level scripts, NOT main project).

### Action Items
- @developer: Add CI workflow enforcing look-ahead + warmup tests on every PR
- @researcher: Investigate evidence fabrication root cause
- @quant: Independently validate Phase 7 safety layer
- @architect: Review monorepo structure (legacy sandbox hazard)

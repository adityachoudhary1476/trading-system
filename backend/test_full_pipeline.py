#!/usr/bin/env python3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime, timezone, timedelta, date
from trading_system.india.instrument_repository import InstrumentRepository
from tests.fixtures.india_fixtures import INSTRUMENT_MASTER_ROWS
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.research.strategy_intelligence import EvidenceRequirement, EvidenceFreshnessConfig
from trading_system.autonomous.bot_config import AutonomousBotConfig, BotMode, TradingMode, Source, UserConstraints
from trading_system.autonomous.controller import AutonomousController
from sqlalchemy import create_engine

# Create repo with NIFTY options
future_expiry = (date.today() + timedelta(days=30)).isoformat()
future_expiry2 = (date.today() + timedelta(days=60)).isoformat()
nifty_options = [
    {'symbol': f'NIFTY{future_expiry[2:4]}{future_expiry[5:7].upper()}25000CE', 'exch': 'NSE', 'token': '99998001', 'instrument': 'NIFTY', 'expiry': future_expiry, 'strike': '25000', 'option_type': 'CE', 'lot_size': '75'},
    {'symbol': f'NIFTY{future_expiry[2:4]}{future_expiry[5:7].upper()}25000PE', 'exch': 'NSE', 'token': '99998002', 'instrument': 'NIFTY', 'expiry': future_expiry, 'strike': '25000', 'option_type': 'PE', 'lot_size': '75'},
    {'symbol': f'NIFTY{future_expiry[2:4]}{future_expiry[5:7].upper()}25100CE', 'exch': 'NSE', 'token': '99998003', 'instrument': 'NIFTY', 'expiry': future_expiry, 'strike': '25100', 'option_type': 'CE', 'lot_size': '75'},
    {'symbol': f'NIFTY{future_expiry[2:4]}{future_expiry[5:7].upper()}25100PE', 'exch': 'NSE', 'token': '99998004', 'instrument': 'NIFTY', 'expiry': future_expiry, 'strike': '25100', 'option_type': 'PE', 'lot_size': '75'},
    {'symbol': f'NIFTY{future_expiry2[2:4]}{future_expiry2[5:7].upper()}25000CE', 'exch': 'NSE', 'token': '99998005', 'instrument': 'NIFTY', 'expiry': future_expiry2, 'strike': '25000', 'option_type': 'CE', 'lot_size': '75'},
    {'symbol': f'NIFTY{future_expiry2[2:4]}{future_expiry2[5:7].upper()}25000PE', 'exch': 'NSE', 'token': '99998006', 'instrument': 'NIFTY', 'expiry': future_expiry2, 'strike': '25000', 'option_type': 'PE', 'lot_size': '75'},
]
all_rows = INSTRUMENT_MASTER_ROWS + nifty_options
header = 'Symbol,Exch,Token,Instrument,Expiry,StrikePrice,OptionType,LotSize'
lines = [header]
for r in all_rows:
    lines.append(f"{r['symbol']},{r['exch']},{r['token']},{r['instrument']},{r['expiry']},{r['strike']},{r['option_type']},{r['lot_size']}")
csv_text = "\n".join(lines)
repo = InstrumentRepository.from_fyers_csv(csv_text)
print('NIFTY options:', len(repo.list_options('NIFTY')))

# Create mock OHLCV data - use SMA crossover pattern
def mock_load_market_data(symbol, timeframe):
    if symbol != 'NSE:NIFTY50':
        return None
    now = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
    bars = []
    base_price = 25000
    for i in range(100):
        ts = now - timedelta(days=99-i)
        if i < 95:
            price = base_price + i * 2
        else:
            price = base_price + i * 2 + (i - 94) * 20
        bars.append({
            'open': price,
            'high': price + 50,
            'low': price - 30,
            'close': price + 20,
            'volume': 100000
        })
    df = pd.DataFrame(bars, index=pd.date_range(end=now, periods=100, freq='D', tz='UTC'))
    return df

# Build control center with mock
engine = create_engine('sqlite:///./data/market_data.db', connect_args={'check_same_thread': False})

from trading_system.research.strategy_intelligence import EvidenceRequirement, EvidenceFreshnessConfig
requirement = EvidenceRequirement(require_walk_forward=False, require_validation=False, require_recent_evidence=False, min_validation_trades=0)
freshness = EvidenceFreshnessConfig(max_age_days=180)

from trading_system.paper.control import PaperTradingControlCenter
center = PaperTradingControlCenter.from_engine(
    engine,
    requirement=requirement,
    freshness_config=freshness,
    market_data_provider=mock_load_market_data,
)
print('Control center created')

# Build controller
from trading_system.autonomous.bot_config import AutonomousBotConfig, BotMode, TradingMode, Source, UserConstraints
from trading_system.autonomous.controller import AutonomousController

bot_config = AutonomousBotConfig(
    bot_id='bot-nifty-options',
    name='Test Bot',
    mode=BotMode.AUTONOMOUS,
    trading_mode=TradingMode.PAPER,
    enabled=True,
    user_constraints=UserConstraints(
        allowed_symbols=frozenset({'NSE:NIFTY50'}),
        allowed_strategy_ids=frozenset(),
        allowed_timeframes=frozenset({'1d'}),
        allowed_option_underlyings=frozenset({'NIFTY'}),
    ),
    max_simultaneous_positions=5,
    source=Source.AUTONOMOUS,
)

controller = AutonomousController(config=bot_config, control_center=center)
print('Controller created')

# Debug scan
center.load_market_data = mock_load_market_data

print('Running scan...')
scan = controller.scan_market()
print(f'Scan: {scan.eligible_count} eligible, {scan.rejected_count} rejected')
for c in scan.candidates:
    print(f'  Candidate: {c.symbol} timeframe={c.timeframe} eligibility={c.eligibility}')
for r in scan.rejections:
    print(f'  Rejection: {r.symbol} reason={r.reason} detail={r.detail}')

print('\nRunning rank...')
ranking = controller.rank_candidates(scan)
print(f'Ranking: {len(ranking.opportunities)} opportunities')
for r in ranking.opportunities[:3]:
    print(f'  {r.symbol} opp_score={r.opportunity_score:.3f}')

print('\nRunning compat...')
compat = controller.evaluate_strategy_compatibility(ranking)
print(f'Compat: {len(compat.compatible)} compatible')
for c in compat.compatible[:3]:
    print(f'  {c.opportunity_symbol} strategy={c.strategy_id} compat_score={c.compatibility_score:.3f}')

print('\nRunning decisions...')
decisions = controller.generate_strategy_decisions(compat)
print(f'Decisions: {len(decisions.decisions)} decisions')
for d in decisions.decisions:
    print(f'  Decision: {d.opportunity_symbol} action={d.signal.action if d.signal else None} valid={d.is_valid} reason={d.rejection_reason if hasattr(d, "rejection_reason") else "N/A"}')
    if d.selected_configuration:
        print(f'    strategy={d.selected_configuration.strategy_id} timeframe={d.selected_configuration.timeframe}')
    if d.signal and hasattr(d.signal, 'option_intent'):
        print(f'    option_intent={d.signal.option_intent}')
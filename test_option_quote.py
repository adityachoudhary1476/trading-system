#!/usr/bin/env python3
import os
import re
from datetime import date, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from trading_system.india.instrument_repository import InstrumentRepository
from tests.fixtures.india_fixtures import INSTRUMENT_MASTER_ROWS

# Load real token
with open('.env', 'r') as f:
    content = f.read()
    match = re.search(r'UPSTOX_SERVICE_ACCOUNT_TOKEN=(.+)', content)
    token = match.group(1).strip() if match else None

# Create repo with NIFTY options
future_expiry = (date.today() + timedelta(days=30)).isoformat()
nifty_options = [
    {'symbol': f'NIFTY{future_expiry[2:4]}{future_expiry[5:7].upper()}25000CE', 'exch': 'NSE', 'token': '99998001', 'instrument': 'NIFTY', 'expiry': future_expiry, 'strike': '25000', 'option_type': 'CE', 'lot_size': '75'},
    {'symbol': f'NIFTY{future_expiry[2:4]}{future_expiry[5:7].upper()}25000PE', 'exch': 'NSE', 'token': '99998002', 'instrument': 'NIFTY', 'expiry': future_expiry, 'strike': '25000', 'option_type': 'PE', 'lot_size': '75'},
]
all_rows = INSTRUMENT_MASTER_ROWS + nifty_options
header = 'Symbol,Exch,Token,Instrument,Expiry,StrikePrice,OptionType,LotSize'
lines = [header]
for r in all_rows:
    lines.append(f"{r['symbol']},{r['exch']},{r['token']},{r['instrument']},{r['expiry']},{r['strike']},{r['option_type']},{r['lot_size']}")
csv_text = "\n".join(lines)
repo = InstrumentRepository.from_fyers_csv(csv_text)
print('NIFTY options:', len(repo.list_options('NIFTY')))
for o in repo.list_options('NIFTY'):
    print(f'  {o.key} strike={o.strike} expiry={o.expiry} type={o.instrument_type}')

# Test option quote retrieval
from trading_system.india.upstox import UpstoxMarketDataProvider
from trading_system.india.option_quotes import CurrentOptionQuoteProvider

md = UpstoxMarketDataProvider(
    client_id='ca148fa3-d9e9-42f2-b6dd-8fc13da5a306',
    access_token=token
)
print('\nMarket data provider authenticated:', md.is_authenticated)

quote_provider = CurrentOptionQuoteProvider(md, max_quote_age_seconds=300)
print('Quote provider authenticated:', quote_provider.is_authenticated)

options = repo.list_options('NIFTY')
if options:
    opt = options[0]
    print(f'\nTesting quote for: {opt.key}')
    quote = quote_provider.get_quote(opt)
    if quote:
        print(f'  LTP: {quote.ltp}')
        print(f'  Bid: {quote.bid}')
        print(f'  Ask: {quote.ask}')
        print(f'  OI: {quote.oi}')
        print(f'  Volume: {quote.volume}')
        print(f'  Timestamp: {quote.timestamp}')
        print(f'  Fetched at: {quote.fetched_at}')
        print(f'  Age seconds: {quote.age_seconds}')
        print(f'  Fresh: {quote_provider.is_fresh(quote, max_age_seconds=300)}')
    else:
        print('  No quote returned (None)')
else:
    print('No options found')
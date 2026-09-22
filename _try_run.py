"""Quick smoke test: build the full scheduler controller stack and run one tick."""
import os, sys
from dotenv import load_dotenv
load_dotenv('.env')

sys.path.insert(0, 'src')
sys.path.insert(0, 'backend')

from backend.autonomous_scheduler import (
    _build_engine, _build_control_center, _build_controller,
    _env_portfolio, _env_phase22, _env_phase22_options,
)
from trading_system.autonomous.persistence import AutonomousBotStateStore

db_url = os.environ.get('MARKET_DATA_DB_URL', 'sqlite:///./data/market_data.db')
engine = _build_engine(db_url)
center, md_provider, md_callable, _ = _build_control_center(engine)
persistence = AutonomousBotStateStore(engine)
phase22_enabled = _env_phase22()
portfolio_enabled = _env_portfolio()

controller = _build_controller(
    center, md_provider, md_callable, 'bot-nifty-options',
    persistence=persistence,
    phase22_enabled=phase22_enabled,
    phase22_options_enabled=False,
)
controller._portfolio_enabled = portfolio_enabled
print(f'Controller built. portfolio_enabled={portfolio_enabled}, phase22_enabled={phase22_enabled}')
print(f'Is halted: {controller.is_halted}')
print(f'Kill switch: {controller.kill_switch.state.value}')

dep = controller.portfolio.find_portfolio_deployment()
print(f'Existing portfolio deployment: {dep}')

# Check fresh market data
from backend.autonomous_scheduler import _has_fresh_data
fresh = _has_fresh_data(center, 'NSE:NIFTY', '1d')
print(f'Fresh market data for NSE:NIFTY: {fresh}')

# Try one portfolio tick
try:
    result = controller.portfolio.tick()
    print(f'TICK RESULT: {result}')
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f'TICK FAILED: {type(e).__name__}: {e}')

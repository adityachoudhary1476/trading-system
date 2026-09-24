import sys
sys.path.insert(0, 'src')
import os
os.environ['PAPER_CAPITAL'] = '100000.0'

from trading_system.execution.orders import OrderIntent, OrderType, Side
from trading_system.execution.paper_broker import PaperBroker

# Create a broker like the one used by submit_order_intent
broker = PaperBroker.initial_cash(100000.0)

# Create an order like the one used by enter_opportunities
order = OrderIntent(
    symbol='NIFTY',
    side=Side.BUY,
    quantity=1.0,
    order_type=OrderType.MARKET,
    options_contract_id='xxx',
    strike=23450,
    expiry='2026-10-06',
    option_type='CE',
    contract_size=50,
    client_order_id=None,
)

# Submit the order (this is what submit_order_intent does)
result = broker.submit_order(
    symbol=order.symbol,
    side=order.side,
    quantity=order.quantity,
    order_type=order.order_type,
    options_contract_id=order.options_contract_id,
    strike=order.strike,
    expiry=order.expiry,
    option_type=order.option_type,
    contract_size=order.contract_size,
)

print('Order status:', result.status)
print('Order filled_quantity:', result.filled_quantity)
print('Order avg_fill_price:', result.avg_fill_price)
print()
print('PaperBroker._positions:', broker._positions)
print('PaperBroker._positions keys:', list(broker._positions.keys()))
for k, v in broker._positions.items():
    print(f'  {k}: {v}')

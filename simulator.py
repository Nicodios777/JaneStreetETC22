"""Offline, single-symbol experiment for the original MarketMaker.

Run: python3 simulator.py --seeds 100 --events 5000 --start-seed 0
Each seed is an independent round. The reference midpoint starts at $1000.
External liquidity always quotes one share at midpoint +/- $1. Each event is
a fair coin toss between an incoming one-share buy and sell. The best price
wins; ties go to the strategy with the scenario's specified probability.
The actual trade price is broadcast to the original strategy after matching.
All cancellations and replacements take effect before the next event. Only
incoming market orders trigger fills; quote updates alone never trigger trades.

After each trade there is a 20% chance the midpoint moves $1. In the first
two scenarios its direction is independent of trade side. In the third it
follows the incoming order. All scenarios share the same random draws per seed.
Positions start at zero and have no cap. Final P&L marks inventory at the
closing midpoint and deducts fees. Liquidation costs are excluded.
"""
import argparse
import math
import random
import statistics

from market_maker import Dir, MarketMaker


class SimulatedExchange:
    def __init__(self):
        self.quotes = {}
        self.position = 0
        self.cash = 0
        self.fills = 0
        self.max_inventory = 0

    def send_cancel_message(self, order_id):
        self.quotes.pop(order_id, None)

    def send_add_message(self, order_id, symbol, dir, price, size):
        if symbol != 'GS' or size != 1:
            raise ValueError('The simulator supports one-share GS orders.')
        self.quotes[order_id] = (dir, price)

    def match(self, midpoint, buyer, tie_draw, tie_probability):
        """Execute one incoming share before the strategy sees its price."""
        side = Dir.SELL if buyer else Dir.BUY
        external_price = midpoint + (1 if buyer else -1)
        for order_id, (quote_side, price) in self.quotes.items():
            if quote_side != side:
                continue
            better = price < external_price if buyer else price > external_price
            if better or (price == external_price and tie_draw < tie_probability):
                self.quotes.pop(order_id)
                quantity = -1 if buyer else 1
                self.position += quantity
                self.cash -= quantity * price
                self.fills += 1
                self.max_inventory = max(self.max_inventory, abs(self.position))
                return price
        return external_price


def round_result(seed, events, tie_probability, informed, fee):
    rng = random.Random(seed)
    exchange = SimulatedExchange()
    strategy = MarketMaker(exchange)
    midpoint = 1000
    for _ in range(events):
        buyer = rng.random() < 0.5
        tie_draw = rng.random()
        move = rng.random() < 0.2
        up = rng.random() < 0.5
        traded = exchange.match(midpoint, buyer, tie_draw, tie_probability)
        strategy.listen({'type': 'trade', 'symbol': 'GS', 'price': traded})
        if move:
            direction_up = buyer if informed else up
            midpoint = max(2, midpoint + (1 if direction_up else -1))
    gross = exchange.cash + exchange.position * midpoint
    return gross - fee * exchange.fills, exchange.fills, exchange.max_inventory


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--seeds', type=positive_int, default=100)
    parser.add_argument('--events', type=positive_int, default=5000)
    parser.add_argument('--start-seed', type=int, default=0)
    parser.add_argument('--fee', type=float, default=0.01, help='dollars per filled share')
    args = parser.parse_args()
    if not math.isfinite(args.fee) or args.fee < 0:
        parser.error('--fee must be finite and nonnegative')
    print(f'{args.seeds} rounds x {args.events} events; first seed {args.start_seed}; '
          f'fee per fill ${args.fee:g}; no-trade P&L $0')
    print('scenario              mean net    P10 net    P90 net  loss %  mean fills  peak |pos|*')
    scenarios = [('Neutral flow', 0.5, False), ('Queue disadvantage', 0.1, False),
                 ('Directional flow', 0.1, True)]
    for label, tie, informed in scenarios:
        results = [round_result(seed, args.events, tie, informed, args.fee)
                   for seed in range(args.start_seed, args.start_seed + args.seeds)]
        pnl, fills, peaks = zip(*results)
        ranked = sorted(pnl)
        low = ranked[math.ceil(0.1 * len(pnl)) - 1]
        high = ranked[math.ceil(0.9 * len(pnl)) - 1]
        losses = sum(value < 0 for value in pnl) / len(pnl)
        print(f'{label:20} {statistics.mean(pnl):9.2f} {low:10.2f} {high:10.2f} '
              f'{losses:7.1%} {statistics.mean(fills):11.1f} {max(peaks):12d}')
    print('* Largest absolute position (shares) seen across all rounds.')


if __name__ == '__main__':
    main()

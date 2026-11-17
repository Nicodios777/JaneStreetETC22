"""Paper replay. python3 market_replay.py gs 2026-11-11 2026-11-17
python3 market_replay.py ibm   # pinned October 7-11, 2013 tick sample
GS uses Yahoo Finance one-minute bars. Quotes refresh on each usable minute
close. A subsequent consecutive minute must trade strictly through an order
for a one-share modeled fill at its limit. Skip both sides on ambiguous bars,
and skip fill inference across missing minutes. Ignore zero-volume bars.
IBM refreshes on every eligible historical print, including same-timestamp
prints, but counts fills only on a strictly later timestamp. Restrict trades
to regular/no-condition, unflagged positive-size on-exchange regular hours.
In both models, fills are hypothetical. There is no queue or latency model.
Use one cent per share in fees each way. Each session starts and ends flat:
GS exits at the last usable close with one cent slippage; IBM exits at the
last observed NYSE bid/ask, without a depth or stale-quote adjustment.
For GS, rerun within the provider's intraday retention window. Full sessions
only. Requires Python 3.9+. No credentials, broker connections or orders.
"""
import argparse
import csv
import io
import json
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from market_maker import Dir, MarketMaker

NY = ZoneInfo('America/New_York')
CENT = Decimal('0.01')
SCALE = Decimal(10000)
LEAN = ('https://raw.githubusercontent.com/QuantConnect/Lean/'
        'e534cddb1a364519f7d5ddc318994a2c06d72354/Data/equity/usa/tick/ibm/')


def get(url):
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=45) as reply:
        return reply.read()


class PaperBook:
    def __init__(self):
        self.orders = {}
        self.position = self.fills = self.ambiguous = 0
        self.cash = Decimal(0)
        self.strategy = MarketMaker(self)

    def send_add_message(self, order_id, symbol, dir, price, size):
        assert size == 1
        rounding = ROUND_FLOOR if dir == Dir.BUY else ROUND_CEILING
        self.orders[order_id] = (dir, price.quantize(CENT, rounding=rounding))

    def send_cancel_message(self, order_id):
        self.orders.pop(order_id, None)

    def refresh(self, price):
        # GS is an allowed symbol in the upstream strategy. For the one-symbol
        # IBM experiment it is an internal routing alias; all prices are IBM.
        self.strategy.listen({'type': 'trade', 'symbol': 'GS', 'price': price})

    def match(self, low, high):
        crossed = [(key, side, price) for key, (side, price) in self.orders.items()
                   if (side == Dir.BUY and low < price) or (side == Dir.SELL and high > price)]
        if len(crossed) == 2:
            self.ambiguous += 1
            return
        for key, side, price in crossed:
            quantity = 1 if side == Dir.BUY else -1
            self.cash -= quantity * price + CENT
            self.position += quantity
            self.fills += 1
            del self.orders[key]

    def finish(self, liquidation_price):
        return self.cash + self.position * liquidation_price - abs(self.position) * CENT


def gs_session(day):
    start = datetime.combine(day, time(9, 30), NY)
    end = start.replace(hour=16, minute=0)
    if datetime.now(NY) < end:
        raise ValueError('session has not closed')
    query = urlencode(dict(period1=int(start.timestamp()), period2=int(end.timestamp()),
                           interval='1m', includePrePost='false'))
    chart = json.loads(get('https://query1.finance.yahoo.com/v8/finance/chart/GS?' + query))
    data = chart['chart']['result'][0]
    quote = data['indicators']['quote'][0]
    book = PaperBook()
    previous = last = None
    observations = 0
    for index, stamp in enumerate(data.get('timestamp', [])):
        if not start.timestamp() <= stamp < end.timestamp():
            continue
        values = [quote[k][index] for k in ('low', 'high', 'close')]
        if None in values or not quote['volume'][index]:
            continue
        # Remove binary-float noise, preserving sub-cent stock prints.
        low, high, last = (Decimal(str(x)).quantize(Decimal('0.0001')) for x in values)
        if not low <= last <= high or last <= 0:
            raise ValueError('invalid market bar')
        if previous is not None and stamp <= previous:
            raise ValueError('unordered market bars')
        if previous is not None and stamp == previous + 60:
            book.match(low, high)
        book.refresh(last)
        previous = stamp
        observations += 1
    if observations < 380 or previous != int(end.timestamp()) - 60:
        raise ValueError(f'incomplete full session: {observations} usable bars')
    # Approximate end-of-day liquidation one cent worse than last close.
    liquidation = last - CENT if book.position > 0 else last + CENT
    return observations, book, book.finish(liquidation)


def ticks(day, kind):
    with zipfile.ZipFile(io.BytesIO(get(LEAN + day.strftime('%Y%m%d') + '_' + kind + '.zip'))) as archive:
        with archive.open(archive.namelist()[0]) as raw:
            yield from csv.reader(io.TextIOWrapper(raw))


def ibm_session(day):
    book = PaperBook()
    previous = None
    count = 0
    for ts, price, volume, venue, condition, suspicious in ticks(day, 'trade'):
        ms = Decimal(ts)
        if not 34200000 <= ms < 57600000 or suspicious != '0' or int(volume) <= 0:
            continue
        if condition not in ('0', '1') or venue == 'D':
            continue
        price = Decimal(price) / SCALE
        if previous is not None and ms < previous:
            raise ValueError('unordered trades')
        if previous is not None and ms > previous:
            book.match(price, price)
        book.refresh(price)
        previous = ms
        count += 1
    # Mark the closing inventory against the final available NYSE bid/ask.
    bid = ask = None
    for ts, bp, bs, ap, az, venue, condition, suspicious in ticks(day, 'quote'):
        if not 34200000 <= Decimal(ts) < 57600000 or venue != 'N' or suspicious != '0':
            continue
        if int(bp):
            bid = Decimal(bp) / SCALE if int(bs) > 0 else None
        if int(ap):
            ask = Decimal(ap) / SCALE if int(az) > 0 else None
    if not count or not bid or not ask or bid > ask:
        raise ValueError('missing session trades or closing quote')
    return count, book, book.finish(bid if book.position > 0 else ask)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['gs', 'ibm'])
    p.add_argument('dates', nargs='*', type=date.fromisoformat)
    args = p.parse_args()
    if args.mode == 'ibm' and not args.dates:
        start, end = date(2013, 10, 7), date(2013, 10, 11)
    elif args.mode == 'gs' and len(args.dates) in (1, 2):
        start, end = args.dates[0], args.dates[-1]
    else:
        p.error('pass one date or a date range for gs, and no dates for ibm')
    if start > end:
        p.error('end precedes start')
    print('date,observations,fills,ambiguous_bars,shares_before_close,net_usd')
    day = start
    while day <= end:
        if day.weekday() < 5:
            observations, book, pnl = (gs_session if args.mode == 'gs' else ibm_session)(day)
            print(f'{day},{observations},{book.fills},{book.ambiguous},{book.position},{pnl:.2f}', flush=True)
        day += timedelta(days=1)


if __name__ == '__main__':
    main()

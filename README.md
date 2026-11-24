# Bitcoin Market Intelligence

Public Kraken spot BTC/USD data. GitHub Actions collects 15-minute, hourly,
4-hour, daily and weekly candles at minutes 7, 22, 37 and 52 UTC. The standard
runner in this public repository is free and runs independently of your devices.
GitHub can delay or skip scheduled runs.

## Read the data

Start with [status.json](data/btc-usd/status.json). It records the latest observed
Kraken price, the most recent fully successful collection, per-timeframe
counts and freshness, detected gaps and errors. Compare the current UTC time
with the relevant `fresh_until`, since a static file cannot update its freshness
label while collection is stopped. Each timeframe tracks its own last success.

| Timeframe | Recent JSON |
| --- | --- |
| 15 minutes | [15m](data/btc-usd/15m/recent.json) |
| 1 hour | [1h](data/btc-usd/1h/recent.json) |
| 4 hours | [4h](data/btc-usd/4h/recent.json) |
| 1 day | [1d](data/btc-usd/1d/recent.json) |
| 1 week | [1w](data/btc-usd/1w/recent.json) |

For public reads, use
`https://raw.githubusercontent.com/alexmassey1981-ux/bitcoin-market-intelligence/main/data/btc-usd/status.json`.
Resolve timeframe `url_path` values relative to the status file. Resolve the
archive paths in each timeframe relative to its recent file. Archived files
partition completed candles by the UTC month in which they opened.

Rows follow `columns`. Timestamps are UTC Unix seconds at candle open.
Prices and VWAP are decimal strings in USD, volume is a decimal string in BTC,
and trade counts are integers. Each recent file contains up to 720 completed
candles and one separately identified forming candle. Weekly boundaries are
Thursday at 00:00 UTC, as returned by Kraken. The latest price is the observed
close of the forming 15-minute candle. It is refreshed on collection.

## Collection and recovery

The collector uses Python's standard library and unauthenticated public Kraken
OHLC requests. It validates shape, decimal fields, OHLC bounds, ordering,
timestamps and the current forming slot. Requests are spaced at least one second
apart, with up to three attempts per timeframe. Valid completed candles merge
by opening timestamp. A failed timeframe keeps its previous data, successful
timeframes can advance, and the workflow publishes errors before marking the run
failed. A stale snapshot can still contain a formerly forming candle. Use its
observation time and the timeframe's freshness deadline.

Kraken exposes only a rolling recent window. Collection begins with the
available window and retains validated candles thereafter. An outage longer
than the available window can leave gaps that require a separate backfill.
The gap count begins at the first archived completed candle and includes gaps
through the most recently elapsed candle slot. Status lists up to five missing
ranges. Timestamps in the archive can be used to find all ranges.

For a manual run, open **Actions**, select **Collect Kraken BTC/USD**, then tap
**Run workflow**. The job requests repository-scoped write access using GitHub's
built-in workflow token. The default branch must allow that token to push.
Locally, run `python3 collect.py --output /tmp/kraken-btc-usd`. Exit code 2
indicates a handled collection error and a written status snapshot.

"""Publish Kraken spot candles as public JSON files."""
import argparse
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PAIR = "BTC/USD"
INTERVALS = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
COLUMNS = ["timestamp", "open", "high", "low", "close", "vwap", "volume", "trades"]
FRESH_SECONDS = 45 * 60
RECENT_COUNT = 720


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc)


def iso(timestamp):
    return utc(timestamp).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    content = json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def validate_row(row, step, offset):
    if not isinstance(row, list) or len(row) != len(COLUMNS):
        raise ValueError("invalid candle shape")
    timestamp, *values, trades = row
    if type(timestamp) is not int or timestamp <= 0 or (timestamp - offset) % step:
        raise ValueError("invalid candle timestamp")
    if type(trades) is not int or trades < 0:
        raise ValueError("invalid trade count")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)
           for value in values):
        raise ValueError("invalid candle decimal")
    opened, high, low, closed, _, _ = map(Decimal, values)
    if not (0 < low <= min(opened, closed) <= max(opened, closed) <= high):
        raise ValueError("inconsistent OHLC prices")


def fetch(minutes, offset):
    step = minutes * 60
    for attempt in range(3):
        # Ask for one overlapping window. Kraken retains only its rolling recent history.
        since = max(0, int(time.time()) - 722 * step)
        url = "https://api.kraken.com/0/public/OHLC?" + urlencode(
            {"pair": "XBTUSD", "interval": minutes, "since": since}
        )
        try:
            request = Request(url, headers={"Cache-Control": "no-cache"})
            with urlopen(request, timeout=15) as response:
                result = json.load(response)
            if result["error"] != []:
                raise ValueError(f"Kraken reported {result['error']!r}")
            rows = result["result"]["XXBTZUSD"]
            if not isinstance(rows, list) or len(rows) < 2:
                raise ValueError("expected completed and forming candles")
            for row in rows:
                validate_row(row, step, offset)
            timestamps = [row[0] for row in rows]
            if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
                raise ValueError("unordered or duplicate candle timestamps")
            now = int(time.time())
            if not timestamps[-1] <= now < timestamps[-1] + step:
                raise ValueError("Kraken forming candle is stale or in the future")
            time.sleep(1)
            return rows[:-1], rows[-1], now
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as error:
            time.sleep(2 ** (attempt + 1))
            last_error = f"{type(error).__name__}: {error}"[:250]
    raise ValueError(last_error)


def gap_summary(candles, step, offset, now):
    if not candles:
        return None, []
    # Compare with the last fully elapsed slot, even if this collection failed.
    current_slot = now - (now - offset) % step
    gaps = []
    missing = 0
    for left, right in zip(candles, candles[1:] + [[current_slot]]):
        count = max(0, (right[0] - left[0]) // step - 1)
        if count:
            missing += count
            if len(gaps) < 5:
                gaps.append({"from": iso(left[0] + step),
                             "through": iso(right[0] - step), "count": count})
    return missing, gaps


def collect(root):
    status_path = root / "status.json"
    previous_status = read_json(status_path, {})
    status = {
        "schema_version": 1, "source": "Kraken", "pair": PAIR,
        "last_successful_update": previous_status.get("last_successful_update"),
        "latest_price": previous_status.get("latest_price"),
        "freshness_threshold_seconds": FRESH_SECONDS,
        "timeframes": {}, "collection_errors": {},
    }
    for name, minutes in INTERVALS.items():
        step = minutes * 60
        # Kraken's weekly candles start Thursday at 00:00 UTC (Unix epoch anchor).
        offset = 0
        directory = root / name
        partitions = {}
        metadata = {"schema_version": 1, "pair": PAIR, "interval_minutes": minutes,
                    "columns": COLUMNS}
        for path in sorted((directory / "archive").glob("*.json")):
            partitions[path.stem] = read_json(path, None)["completed"]
        previous = read_json(directory / "recent.json", {})
        updated = previous.get("last_successful_update")
        try:
            rows, forming, observed_at = fetch(minutes, offset)
        except ValueError as error:
            status["collection_errors"][name] = str(error)
        else:
            merged = {row[0]: row for rows_in_month in partitions.values() for row in rows_in_month}
            if merged and forming[0] <= max(merged):
                status["collection_errors"][name] = "response predates archived completed candles"
            else:
                # Preserve the first validated version of each completed candle.
                for row in rows:
                    merged.setdefault(row[0], row)
                partitions = {}
                for timestamp in sorted(merged):
                    month = utc(timestamp).strftime("%Y-%m")
                    partitions.setdefault(month, []).append(merged[timestamp])
                updated = iso(observed_at)
                for month, month_rows in partitions.items():
                    write_json(directory / "archive" / f"{month}.json",
                               {**metadata, "completed": month_rows})
                completed = [row for month_rows in partitions.values() for row in month_rows]
                write_json(directory / "recent.json", {
                    **metadata, "last_successful_update": updated,
                    "completed": completed[-RECENT_COUNT:], "forming": forming,
                    "archive": [f"archive/{month}.json" for month in partitions],
                })
                if name == "15m":
                    status["latest_price"] = {
                        "usd": forming[4], "observed_at": updated,
                        "source": "Kraken 15-minute forming candle close",
                        "fresh_until": iso(observed_at + FRESH_SECONDS),
                    }
        completed = [row for month_rows in partitions.values() for row in month_rows]
        missing, gaps = gap_summary(completed, step, offset, int(time.time()))
        status["timeframes"][name] = {
            "url_path": f"{name}/recent.json", "completed_count": len(completed),
            "forming_count": 1 if updated else 0, "missing_completed_count": missing,
            "first_five_missing_ranges": gaps,
            "coverage_start": iso(completed[0][0]) if completed else None,
            "latest_completed_start": iso(completed[-1][0]) if completed else None,
            "last_successful_update": updated,
            "fresh_until": iso(int(datetime.fromisoformat(updated).timestamp()) + FRESH_SECONDS)
            if updated else None,
        }
    now = int(time.time())
    status["last_attempt"] = iso(now)
    if not status["collection_errors"]:
        status["last_successful_update"] = iso(now)
    status["freshness_at_last_attempt"] = (
        "unknown" if not status["latest_price"]
        else "stale" if status["latest_price"]["fresh_until"] <= iso(now)
        else "fresh"
    )
    write_json(status_path, status)
    print(json.dumps(status, indent=2))
    return bool(status["collection_errors"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/btc-usd"))
    raise SystemExit(2 if collect(parser.parse_args().output) else 0)

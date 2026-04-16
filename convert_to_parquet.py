"""
Convert XAUUSD raw tick CSVs (or ZIPs) to Parquet format.

Produces two outputs per year:
  1. Tick-level Parquet (full fidelity, compressed)
  2. OHLCV Parquet per timeframe (M1, M5, M15, H1) — backtest inputs

Usage:
    python convert_to_parquet.py
"""

import sys
import time
import zipfile
from pathlib import Path

try:
    import polars as pl
except ImportError:
    print("Installing polars...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "polars", "pyarrow"])
    import polars as pl

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data"

YEARS = [2021, 2022, 2023, 2024, 2025, 2026]

# Timeframes to aggregate: label → truncation string for Polars
TIMEFRAMES = {
    "M1":  "1m",
    "M5":  "5m",
    "M15": "15m",
    "H1":  "1h",
    "H4":  "4h",
}

# Output directories
TICKS_DIR = DATA_DIR / "parquet" / "ticks"
OHLCV_DIR = DATA_DIR / "parquet" / "ohlcv"

CSV_SCHEMA = {
    "Bid": pl.Float64,
    "Ask": pl.Float64,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_size(path: Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def find_source(year: int) -> tuple[Path, str]:
    """Return (path, kind) where kind is 'csv' or 'zip'."""
    csv = DATA_DIR / f"Exness_XAUUSD_Raw_Spread_{year}.csv"
    zp  = DATA_DIR / f"Exness_XAUUSD_Raw_Spread_{year}.zip"
    if csv.exists():
        return csv, "csv"
    if zp.exists():
        return zp, "zip"
    raise FileNotFoundError(f"No CSV or ZIP found for year {year} in {DATA_DIR}")


def read_ticks(source: Path, kind: str) -> pl.LazyFrame:
    """Return a LazyFrame of cleaned tick data from CSV or ZIP source."""
    if kind == "csv":
        lf = pl.scan_csv(source, schema_overrides=CSV_SCHEMA)
    else:
        # Read the single CSV member from the ZIP into memory, then go lazy
        with zipfile.ZipFile(source) as zf:
            member = zf.namelist()[0]
            print(f"    (reading '{member}' from ZIP ...)")
            with zf.open(member) as f:
                lf = pl.read_csv(f, schema_overrides=CSV_SCHEMA).lazy()

    return (
        lf
        .rename({"Timestamp": "timestamp", "Bid": "bid", "Ask": "ask"})
        .select(["timestamp", "bid", "ask"])
        .with_columns(
            pl.col("timestamp")
            .str.to_datetime("%Y-%m-%d %H:%M:%S%.3fZ", time_unit="ms", time_zone="UTC")
        )
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def convert_ticks(year: int) -> Path:
    """Convert raw source to tick-level Parquet. Returns output path."""
    out_path = TICKS_DIR / f"XAUUSD_ticks_{year}.parquet"
    if out_path.exists():
        print(f"  [skip] tick Parquet already exists: {out_path.name}")
        return out_path

    source, kind = find_source(year)
    print(f"  Reading {source.name} ({fmt_size(source)}) via {kind.upper()} ...")
    t0 = time.time()

    read_ticks(source, kind).sink_parquet(out_path, compression="zstd", compression_level=3)

    elapsed = time.time() - t0
    print(f"  Written: {out_path.name} ({fmt_size(out_path)})  [{fmt_elapsed(elapsed)}]")
    return out_path


def build_ohlcv(year: int, tick_parquet: Path, tf_label: str, tf_truncate: str) -> Path:
    """Aggregate tick Parquet → OHLCV Parquet for one timeframe."""
    tf_dir = OHLCV_DIR / tf_label
    tf_dir.mkdir(parents=True, exist_ok=True)
    out_path = tf_dir / f"XAUUSD_{tf_label}_{year}.parquet"

    if out_path.exists():
        print(f"    [skip] {out_path.name}")
        return out_path

    t0 = time.time()

    (
        pl.scan_parquet(tick_parquet)
        .with_columns(
            pl.col("timestamp").dt.truncate(tf_truncate).alias("time"),
            pl.col("bid").alias("price"),
            (pl.col("ask") - pl.col("bid")).alias("spread"),
        )
        .group_by("time")
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.len().alias("ticks"),
            pl.col("spread").mean().alias("avg_spread"),
        )
        .sort("time")
        .sink_parquet(out_path, compression="zstd", compression_level=3)
    )

    elapsed = time.time() - t0
    rows = pl.scan_parquet(out_path).select(pl.len()).collect().item()
    print(f"    {tf_label}: {rows:,} bars → {out_path.name} ({fmt_size(out_path)})  [{fmt_elapsed(elapsed)}]")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    TICKS_DIR.mkdir(parents=True, exist_ok=True)
    OHLCV_DIR.mkdir(parents=True, exist_ok=True)

    total_start = time.time()

    for year in YEARS:
        print(f"\n{'='*60}")
        print(f"  Year {year}")
        print(f"{'='*60}")

        try:
            tick_parquet = convert_ticks(year)
        except FileNotFoundError as e:
            print(f"  [WARN] {e}")
            continue

        print(f"  Aggregating OHLCV bars ...")
        for tf_label, tf_truncate in TIMEFRAMES.items():
            build_ohlcv(year, tick_parquet, tf_label, tf_truncate)

    print(f"\nDone in {fmt_elapsed(time.time() - total_start)}.")
    print(f"\nOutput layout:")
    print(f"  {TICKS_DIR}  ← full tick data")
    print(f"  {OHLCV_DIR}  ← OHLCV by timeframe")

    print("\nFile sizes:")
    for p in sorted(DATA_DIR.rglob("*.parquet")):
        rel = p.relative_to(DATA_DIR)
        print(f"  {rel}  {fmt_size(p)}")


if __name__ == "__main__":
    main()

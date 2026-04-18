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

MT5_SCHEMA = {
    "<BID>": pl.Float64,
    "<ASK>": pl.Float64,
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


def find_monthly_sources(year: int) -> list[Path]:
    """Return sorted list of monthly zip files like Exness_XAUUSD_Raw_Spread_2025_07.zip."""
    return sorted(DATA_DIR.glob(f"Exness_XAUUSD_Raw_Spread_{year}_??.zip"))


def find_mt5_exports() -> list[Path]:
    """Return all MT5-format tick CSVs matching XAUUSD_*.csv in DATA_DIR."""
    return sorted(DATA_DIR.glob("XAUUSD_*.csv"))


def read_ticks(source: Path, kind: str) -> pl.LazyFrame:
    """Return a LazyFrame of cleaned tick data from Exness CSV or ZIP source."""
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


def read_mt5_ticks(source: Path) -> pl.DataFrame:
    """
    Read an MT5 History Center tick export.

    Expected format (tab-separated):
        <DATE>\\t<TIME>\\t<BID>\\t<ASK>\\t<LAST>\\t<VOLUME>\\t<FLAGS>
        2026.04.02\\t00:00:00.248\\t4784.332\\t4784.429\\t\\t\\t6
    """
    df = pl.read_csv(
        source,
        separator="\t",
        schema_overrides=MT5_SCHEMA,
        null_values=[""],
    )
    return (
        df
        .select(["<DATE>", "<TIME>", "<BID>", "<ASK>"])
        .rename({"<BID>": "bid", "<ASK>": "ask"})
        .with_columns(
            (pl.col("<DATE>") + pl.lit(" ") + pl.col("<TIME>"))
            .str.to_datetime("%Y.%m.%d %H:%M:%S%.3f", time_unit="ms")
            .dt.replace_time_zone("UTC")
            .alias("timestamp")
        )
        .select(["timestamp", "bid", "ask"])
    )


def build_tick_parquet_from_mt5(exports: list[Path]) -> dict[int, Path]:
    """
    Merge one or more MT5 tick export files, split by year, and write tick
    Parquets.  Existing tick Parquets for a year are merged with new data
    (duplicates removed by timestamp).  Returns {year: parquet_path}.
    """
    print(f"\n  Reading {len(exports)} MT5 export file(s) ...")
    frames = [read_mt5_ticks(p) for p in exports]
    combined = pl.concat(frames).sort("timestamp").unique(subset=["timestamp"], keep="first")

    years = combined.with_columns(pl.col("timestamp").dt.year().alias("_year"))["_year"].unique().to_list()

    TICKS_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[int, Path] = {}

    for year in sorted(years):
        out_path = TICKS_DIR / f"XAUUSD_ticks_{year}.parquet"
        year_df = combined.filter(pl.col("timestamp").dt.year() == year)

        if out_path.exists():
            existing = pl.read_parquet(out_path)
            year_df = (
                pl.concat([existing, year_df])
                .sort("timestamp")
                .unique(subset=["timestamp"], keep="first")
            )
            print(f"    Merged year {year}: {len(year_df):,} ticks → {out_path.name}")
        else:
            print(f"    Created year {year}: {len(year_df):,} ticks → {out_path.name}")

        year_df.write_parquet(out_path, compression="zstd", compression_level=3)
        result[year] = out_path

    return result


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def merge_monthly_ticks(year: int, monthly_zips: list[Path]) -> tuple[Path, bool]:
    """
    Read monthly Exness zip files, merge with existing tick Parquet (if any),
    deduplicate by timestamp, and write the result.

    Returns (out_path, was_updated) — was_updated is True if new rows were added.
    """
    out_path = TICKS_DIR / f"XAUUSD_ticks_{year}.parquet"

    print(f"  Merging {len(monthly_zips)} monthly zip(s) into {out_path.name} ...")
    frames: list[pl.DataFrame] = []

    if out_path.exists():
        frames.append(pl.read_parquet(out_path))
        existing_max = frames[0]["timestamp"].max()
        print(f"    Existing data up to: {existing_max}")

    for zp in monthly_zips:
        print(f"    Reading {zp.name} ({fmt_size(zp)}) ...")
        t0 = time.time()
        df = read_ticks(zp, "zip").collect()
        print(f"      {len(df):,} ticks  [{fmt_elapsed(time.time() - t0)}]")
        frames.append(df)

    combined = (
        pl.concat(frames)
        .sort("timestamp")
        .unique(subset=["timestamp"], keep="first")
    )

    before = len(frames[0]) if out_path.exists() else 0
    added = len(combined) - before
    print(f"    Total: {len(combined):,} ticks ({added:+,} new) → {out_path.name}")

    combined.write_parquet(out_path, compression="zstd", compression_level=3)
    return out_path, added > 0


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

    # ------------------------------------------------------------------
    # Phase 1: ingest any MT5 History Center exports (XAUUSD_*.csv)
    # ------------------------------------------------------------------
    mt5_exports = find_mt5_exports()
    mt5_years: set[int] = set()
    if mt5_exports:
        print(f"\nFound {len(mt5_exports)} MT5 export file(s):")
        for p in mt5_exports:
            print(f"  {p.name}")
        mt5_tick_paths = build_tick_parquet_from_mt5(mt5_exports)
        mt5_years = set(mt5_tick_paths.keys())
        # Rebuild OHLCV for affected years
        for year, tick_parquet in mt5_tick_paths.items():
            print(f"\n  Rebuilding OHLCV for year {year} ...")
            for tf_label, tf_truncate in TIMEFRAMES.items():
                # Delete stale OHLCV so it gets rebuilt
                stale = OHLCV_DIR / tf_label / f"XAUUSD_{tf_label}_{year}.parquet"
                if stale.exists():
                    stale.unlink()
                build_ohlcv(year, tick_parquet, tf_label, tf_truncate)

    # ------------------------------------------------------------------
    # Phase 2: process Exness CSV/ZIP sources for remaining years
    # ------------------------------------------------------------------
    for year in YEARS:
        if year in mt5_years:
            continue  # already handled above

        print(f"\n{'='*60}")
        print(f"  Year {year}")
        print(f"{'='*60}")

        # Check for monthly zip supplements (e.g. 2025_07.zip … 2025_12.zip)
        monthly_zips = find_monthly_sources(year)

        try:
            if monthly_zips:
                tick_parquet, updated = merge_monthly_ticks(year, monthly_zips)
                if not updated:
                    print(f"  [skip] no new ticks found in monthly zips — OHLCV unchanged")
                    continue
                # Rebuild OHLCV since tick data changed
                print(f"  Rebuilding OHLCV bars ...")
                for tf_label, tf_truncate in TIMEFRAMES.items():
                    stale = OHLCV_DIR / tf_label / f"XAUUSD_{tf_label}_{year}.parquet"
                    if stale.exists():
                        stale.unlink()
                    build_ohlcv(year, tick_parquet, tf_label, tf_truncate)
            else:
                tick_parquet = convert_ticks(year)
                print(f"  Aggregating OHLCV bars ...")
                for tf_label, tf_truncate in TIMEFRAMES.items():
                    build_ohlcv(year, tick_parquet, tf_label, tf_truncate)
        except FileNotFoundError as e:
            print(f"  [WARN] {e}")
            continue

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

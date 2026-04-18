import type { BacktestResults, TradeRecord } from '../api/types';

function holdMinutes(t: TradeRecord): number {
  return (new Date(t.exit_time).getTime() - new Date(t.entry_time).getTime()) / 60_000;
}

function fmtDuration(mins: number): string {
  if (mins < 60) return `${Math.round(mins)}m`;
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  if (h >= 24) {
    const d = Math.floor(h / 24);
    const rh = h % 24;
    return rh > 0 ? `${d}d ${rh}h` : `${d}d`;
  }
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

interface Props {
  results: BacktestResults;
  stoppedOut?: boolean;
}

interface MetricTileProps {
  label: string;
  value: string;
  good?: boolean | null; // true=green, false=red, null=neutral
  sub?: string;
}

function MetricTile({ label, value, good, sub }: MetricTileProps) {
  const color =
    good === true
      ? 'text-emerald-400'
      : good === false
      ? 'text-red-400'
      : 'text-slate-100';
  return (
    <div className="bg-slate-800 rounded-xl p-4 flex flex-col gap-1">
      <span className="text-xs text-slate-500 uppercase tracking-wide">{label}</span>
      <span className={`text-2xl font-bold ${color}`}>{value}</span>
      {sub && <span className="text-xs text-slate-500">{sub}</span>}
    </div>
  );
}

export function ResultCard({ results, stoppedOut }: Props) {
  const {
    total_return_pct,
    win_rate_pct,
    profit_factor,
    max_drawdown_pct,
    total_trades,
    risk_pct,
    initial_capital,
    final_capital,
    avg_win_r,
    avg_loss_r,
    max_consec_wins,
    max_consec_losses,
    compound,
    trades,
  } = results;

  // Hold period stats
  const holdTimes = trades.map(holdMinutes);
  const avgHold  = holdTimes.length ? holdTimes.reduce((a, b) => a + b, 0) / holdTimes.length : 0;
  const maxHold  = holdTimes.length ? Math.max(...holdTimes) : 0;
  const minHold  = holdTimes.length ? Math.min(...holdTimes) : 0;

  const winHolds  = trades.filter((t) => t.exit_reason === 'tp').map(holdMinutes);
  const lossHolds = trades.filter((t) => t.exit_reason !== 'tp').map(holdMinutes);
  const avgWinHold  = winHolds.length  ? winHolds.reduce((a, b)  => a + b, 0) / winHolds.length  : null;
  const avgLossHold = lossHolds.length ? lossHolds.reduce((a, b) => a + b, 0) / lossHolds.length : null;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      <MetricTile
        label="Total Return"
        value={stoppedOut ? '-100%' : `${total_return_pct >= 0 ? '+' : ''}${total_return_pct.toFixed(2)}%`}
        good={total_return_pct >= 0}
        sub={stoppedOut
          ? `$${initial_capital.toLocaleString()} → $0 · STOP OUT`
          : `$${initial_capital.toLocaleString()} → $${final_capital.toLocaleString()}`}
      />
      <MetricTile
        label="Win Rate"
        value={`${win_rate_pct.toFixed(1)}%`}
        good={win_rate_pct >= 50}
      />
      <MetricTile
        label="Profit Factor"
        value={profit_factor.toFixed(3)}
        good={profit_factor >= 1}
      />
      <MetricTile
        label="Max Drawdown"
        value={`-${max_drawdown_pct.toFixed(2)}%`}
        good={max_drawdown_pct < 20}
      />
      <MetricTile
        label="Total Trades"
        value={String(total_trades)}
        good={null}
      />
      <MetricTile
        label="Risk / Trade"
        value={`${(risk_pct * 100).toFixed(1)}%`}
        good={null}
        sub={compound ? 'compounding' : 'fixed'}
      />
      <MetricTile
        label="Avg Win"
        value={`${avg_win_r.toFixed(2)}R`}
        good={true}
      />
      <MetricTile
        label="Avg Loss"
        value={`${avg_loss_r.toFixed(2)}R`}
        good={false}
      />
      <MetricTile
        label="Max Consec. Wins"
        value={String(max_consec_wins ?? 0)}
        good={null}
      />
      <MetricTile
        label="Max Consec. Losses"
        value={String(max_consec_losses ?? 0)}
        good={null}
      />
      <MetricTile
        label="Avg Hold"
        value={holdTimes.length ? fmtDuration(avgHold) : '—'}
        good={null}
        sub={[
          avgWinHold  != null ? `W: ${fmtDuration(avgWinHold)}`  : null,
          avgLossHold != null ? `L: ${fmtDuration(avgLossHold)}` : null,
        ].filter(Boolean).join(' · ')}
      />
      <MetricTile
        label="Longest Hold"
        value={holdTimes.length ? fmtDuration(maxHold) : '—'}
        good={null}
      />
      <MetricTile
        label="Shortest Hold"
        value={holdTimes.length ? fmtDuration(minHold) : '—'}
        good={null}
      />
    </div>
  );
}

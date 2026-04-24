import type { BacktestResults, TradeRecord } from '../api/types';

function holdMinutes(t: TradeRecord): number {
  return (new Date(t.exit_time).getTime() - new Date(t.entry_time).getTime()) / 60_000;
}

function computePeriodDrawdown(
  trades: TradeRecord[],
  initialCapital: number,
  getKey: (t: TradeRecord) => string,
): number {
  if (trades.length === 0) return 0;
  const sorted = [...trades].sort((a, b) => a.trade - b.trade);
  const groups = new Map<string, TradeRecord[]>();
  for (const t of sorted) {
    const key = getKey(t);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(t);
  }
  let worstPct = 0;
  let capitalBefore = initialCapital;
  for (const periodTrades of groups.values()) {
    let peak = capitalBefore;
    let maxDd = 0;
    for (const t of periodTrades) {
      if (t.capital_after > peak) peak = t.capital_after;
      const dd = (peak - t.capital_after) / peak;
      if (dd > maxDd) maxDd = dd;
    }
    if (maxDd * 100 > worstPct) worstPct = maxDd * 100;
    capitalBefore = periodTrades[periodTrades.length - 1].capital_after;
  }
  return worstPct;
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

function HeroTile({
  label, value, sub, color,
}: {
  label: string;
  value: string;
  sub?: string;
  color: string;
}) {
  return (
    <div className="bg-slate-800 rounded-xl px-4 py-3 flex flex-col gap-0.5">
      <span className="text-xs text-slate-500 uppercase tracking-wide">{label}</span>
      <span className={`text-xl font-bold leading-tight ${color}`}>{value}</span>
      {sub && <span className="text-xs text-slate-500 leading-tight">{sub}</span>}
    </div>
  );
}

function StatRow({
  label, value, color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-1 border-b border-slate-700/40 last:border-0">
      <span className="text-xs text-slate-500 whitespace-nowrap">{label}</span>
      <span className={`text-xs font-semibold tabular-nums ${color ?? 'text-slate-200'}`}>{value}</span>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-slate-800/50 border border-slate-700/40 rounded-xl px-4 py-3">
      <p className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">{title}</p>
      {children}
    </div>
  );
}

export function ResultCard({ results, stoppedOut }: Props) {
  const {
    total_return_pct = 0,
    win_rate_pct = 0,
    profit_factor = 0,
    max_drawdown_pct = 0,
    total_trades = 0,
    risk_pct = 0,
    initial_capital = 0,
    final_capital = 0,
    avg_win_r = 0,
    avg_loss_r = 0,
    max_consec_wins = 0,
    max_consec_losses = 0,
    compound = false,
    trades = [],
  } = results;

  const maxDailyDd = computePeriodDrawdown(trades, initial_capital, (t) => t.exit_time.slice(0, 10));
  const maxMonthlyDd = computePeriodDrawdown(trades, initial_capital, (t) => t.exit_time.slice(0, 7));

  const holdTimes = trades.map(holdMinutes);
  const avgHold = holdTimes.length ? holdTimes.reduce((a, b) => a + b, 0) / holdTimes.length : 0;
  const maxHold = holdTimes.length ? Math.max(...holdTimes) : 0;
  const minHold = holdTimes.length ? Math.min(...holdTimes) : 0;
  const winHolds = trades.filter((t) => t.exit_reason === 'tp').map(holdMinutes);
  const lossHolds = trades.filter((t) => t.exit_reason !== 'tp').map(holdMinutes);
  const avgWinHold = winHolds.length ? winHolds.reduce((a, b) => a + b, 0) / winHolds.length : null;
  const avgLossHold = lossHolds.length ? lossHolds.reduce((a, b) => a + b, 0) / lossHolds.length : null;

  return (
    <div className="space-y-3">
      {/* Hero row — 4 primary KPIs */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <HeroTile
          label="Total Return"
          value={stoppedOut ? '-100%' : `${total_return_pct >= 0 ? '+' : ''}${total_return_pct.toFixed(2)}%`}
          color={total_return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}
          sub={stoppedOut
            ? `$${initial_capital.toLocaleString()} → $0 · STOP OUT`
            : `$${initial_capital.toLocaleString()} → $${final_capital.toLocaleString()}`}
        />
        <HeroTile
          label="Win Rate"
          value={`${win_rate_pct.toFixed(1)}%`}
          color={win_rate_pct >= 50 ? 'text-emerald-400' : 'text-red-400'}
        />
        <HeroTile
          label="Profit Factor"
          value={profit_factor.toFixed(3)}
          color={profit_factor >= 1 ? 'text-emerald-400' : 'text-red-400'}
        />
        <HeroTile
          label="Max Drawdown"
          value={`-${max_drawdown_pct.toFixed(2)}%`}
          color={max_drawdown_pct < 20 ? 'text-slate-100' : 'text-red-400'}
        />
      </div>

      {/* Secondary row — 3 compact stat panels */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <Panel title="Drawdown">
          <StatRow
            label="Max DD (peak-to-trough)"
            value={max_drawdown_pct > 0 ? `-${max_drawdown_pct.toFixed(2)}%` : '—'}
            color={max_drawdown_pct < 5 ? 'text-slate-200' : 'text-red-400'}
          />
          <StatRow
            label="Max DD vs Initial"
            value={`-${(results.max_drawdown_from_initial_pct ?? 0).toFixed(2)}%`}
            color={(results.max_drawdown_from_initial_pct ?? 0) < 20 ? 'text-slate-200' : 'text-red-400'}
          />
          <StatRow
            label="Max Daily DD"
            value={maxDailyDd > 0 ? `-${maxDailyDd.toFixed(2)}%` : '—'}
            color={maxDailyDd < 5 ? 'text-slate-200' : 'text-red-400'}
          />
          <StatRow
            label="Max Monthly DD"
            value={maxMonthlyDd > 0 ? `-${maxMonthlyDd.toFixed(2)}%` : '—'}
            color={maxMonthlyDd < 15 ? 'text-slate-200' : 'text-red-400'}
          />
        </Panel>

        <Panel title="Trade Quality">
          <StatRow label="Avg Win" value={`+${avg_win_r.toFixed(2)}R`} color="text-emerald-400" />
          <StatRow label="Avg Loss" value={`${avg_loss_r.toFixed(2)}R`} color="text-red-400" />
          <StatRow label="Max Streak W" value={String(max_consec_wins ?? 0)} />
          <StatRow label="Max Streak L" value={String(max_consec_losses ?? 0)} />
        </Panel>

        <Panel title="Activity & Hold">
          <StatRow
            label="Trades"
            value={`${total_trades} · ${(risk_pct * 100).toFixed(1)}% risk${compound ? ' (comp.)' : ''}`}
          />
          {results.risk_recovery_pct > 0 && (
            <StatRow
              label="Recovery Risk"
              value={`${(results.risk_recovery_pct * 100).toFixed(1)}% when underwater`}
              color="text-amber-400"
            />
          )}
          <StatRow
            label="Avg Hold"
            value={holdTimes.length ? fmtDuration(avgHold) : '—'}
          />
          {(avgWinHold != null || avgLossHold != null) && (
            <StatRow
              label="W / L Hold"
              value={[
                avgWinHold != null ? fmtDuration(avgWinHold) : null,
                avgLossHold != null ? fmtDuration(avgLossHold) : null,
              ].filter(Boolean).join(' / ')}
            />
          )}
          <StatRow
            label="Hold Range"
            value={holdTimes.length ? `${fmtDuration(minHold)} – ${fmtDuration(maxHold)}` : '—'}
          />
        </Panel>
      </div>
    </div>
  );
}

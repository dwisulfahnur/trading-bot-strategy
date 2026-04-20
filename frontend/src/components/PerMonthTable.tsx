import { useState } from 'react';
import type { TradeRecord } from '../api/types';

interface MonthData {
  total_trades: number;
  wins: number;
  win_rate_pct: number;
  return_pct: number;
  gain_usd: number;
}

interface Props {
  trades: TradeRecord[];
  initialCapital: number;
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function computePerMonth(
  trades: TradeRecord[],
  initialCapital: number,
): Record<string, MonthData> {
  if (trades.length === 0) return {};

  // Group trades by "YYYY-MM" key using entry_time
  const groups = new Map<string, TradeRecord[]>();
  for (const t of trades) {
    const key = t.entry_time.slice(0, 7);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(t);
  }

  const months = [...groups.keys()].sort();
  let prevCapital = initialCapital;
  const result: Record<string, MonthData> = {};

  for (const month of months) {
    const mt = groups.get(month)!;
    const wins = mt.filter((t) => t.exit_reason === 'tp').length;
    const endCapital = mt[mt.length - 1].capital_after;
    const gain_usd = mt.reduce((sum, t) => sum + t.profit_usd, 0);
    result[month] = {
      total_trades: mt.length,
      wins,
      win_rate_pct: (wins / mt.length) * 100,
      return_pct: ((endCapital - prevCapital) / prevCapital) * 100,
      gain_usd,
    };
    prevCapital = endCapital;
  }

  return result;
}

export function PerMonthTable({ trades, initialCapital }: Props) {
  const perMonth = computePerMonth(trades, initialCapital);
  const allMonths = Object.keys(perMonth).sort();
  if (allMonths.length === 0) return null;

  // Group months by year
  const byYear = new Map<string, string[]>();
  for (const m of allMonths) {
    const yr = m.slice(0, 4);
    if (!byYear.has(yr)) byYear.set(yr, []);
    byYear.get(yr)!.push(m);
  }
  const years = [...byYear.keys()].sort();

  // Default: expand all if single year, collapse all if multiple
  const [openYears, setOpenYears] = useState<Set<string>>(
    () => new Set(years.length === 1 ? years : []),
  );

  const toggleYear = (yr: string) => {
    setOpenYears((prev) => {
      const next = new Set(prev);
      if (next.has(yr)) next.delete(yr);
      else next.add(yr);
      return next;
    });
  };

  // Year-level aggregates
  const yearTotals = (yr: string): { trades: number; wins: number; return_pct: number; gain_usd: number } => {
    const months = byYear.get(yr)!;
    let trades = 0, wins = 0, gain_usd = 0;
    let factor = 1;
    for (const m of months) {
      const d = perMonth[m];
      trades += d.total_trades;
      wins += d.wins;
      gain_usd += d.gain_usd;
      factor *= 1 + d.return_pct / 100;
    }
    return { trades, wins, return_pct: (factor - 1) * 100, gain_usd };
  };

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-700">
      <table className="w-full text-sm text-left">
        <thead className="bg-slate-800 text-slate-400 uppercase text-xs tracking-wide">
          <tr>
            <th className="px-4 py-2">Period</th>
            <th className="px-4 py-2 text-right">Trades</th>
            <th className="px-4 py-2 text-right">Wins</th>
            <th className="px-4 py-2 text-right">Win Rate</th>
            <th className="px-4 py-2 text-right">Gain (USD)</th>
            <th className="px-4 py-2 text-right">Return</th>
          </tr>
        </thead>
        <tbody>
          {years.map((yr) => {
            const isOpen = openYears.has(yr);
            const tot = yearTotals(yr);
            const months = byYear.get(yr)!;

            return [
              // Year header row
              <tr
                key={yr}
                onClick={() => toggleYear(yr)}
                className="border-t border-slate-700 bg-slate-800/60 cursor-pointer hover:bg-slate-800 select-none"
              >
                <td className="px-4 py-2 font-bold text-slate-100 flex items-center gap-2">
                  <span className="text-slate-500 text-xs w-3">{isOpen ? '▾' : '▸'}</span>
                  {yr}
                </td>
                <td className="px-4 py-2 text-right text-slate-300">{tot.trades}</td>
                <td className="px-4 py-2 text-right text-slate-400">{tot.wins}</td>
                <td className="px-4 py-2 text-right text-slate-300">
                  {tot.trades > 0 ? ((tot.wins / tot.trades) * 100).toFixed(1) : '—'}%
                </td>
                <td className={`px-4 py-2 text-right font-semibold ${tot.gain_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {tot.gain_usd >= 0 ? '+' : ''}${tot.gain_usd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td className={`px-4 py-2 text-right font-semibold ${tot.return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {tot.return_pct >= 0 ? '+' : ''}{tot.return_pct.toFixed(2)}%
                </td>
              </tr>,

              // Month rows (only when expanded)
              ...(isOpen
                ? months.map((m) => {
                    const d = perMonth[m];
                    const monthNum = parseInt(m.slice(5, 7), 10);
                    const label = `${MONTH_NAMES[monthNum - 1]} ${m.slice(0, 4)}`;
                    return (
                      <tr key={m} className="border-t border-slate-800 hover:bg-slate-900/40">
                        <td className="px-4 py-1.5 text-slate-400 pl-10">{label}</td>
                        <td className="px-4 py-1.5 text-right text-slate-400">{d.total_trades}</td>
                        <td className="px-4 py-1.5 text-right text-slate-500">{d.wins}</td>
                        <td className="px-4 py-1.5 text-right text-slate-300">{d.win_rate_pct.toFixed(1)}%</td>
                        <td className={`px-4 py-1.5 text-right font-medium ${d.gain_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {d.gain_usd >= 0 ? '+' : ''}${d.gain_usd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </td>
                        <td className={`px-4 py-1.5 text-right font-medium ${d.return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {d.return_pct >= 0 ? '+' : ''}{d.return_pct.toFixed(2)}%
                        </td>
                      </tr>
                    );
                  })
                : []),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

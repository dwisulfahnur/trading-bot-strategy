interface PerYearData {
  total_trades: number;
  win_rate_pct: number;
  return_pct: number;
}

interface Props {
  perYear: Record<string, PerYearData>;
}

export function PerYearTable({ perYear }: Props) {
  const years = Object.keys(perYear).sort();
  if (years.length <= 1) return null;

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-700">
      <table className="w-full text-sm text-left">
        <thead className="bg-slate-800 text-slate-400 uppercase text-xs tracking-wide">
          <tr>
            <th className="px-4 py-2">Year</th>
            <th className="px-4 py-2">Trades</th>
            <th className="px-4 py-2">Win Rate</th>
            <th className="px-4 py-2">Return</th>
          </tr>
        </thead>
        <tbody>
          {years.map((yr) => {
            const d = perYear[yr];
            return (
              <tr key={yr} className="border-t border-slate-800">
                <td className="px-4 py-2 font-semibold text-slate-200">{yr}</td>
                <td className="px-4 py-2 text-slate-400">{d.total_trades}</td>
                <td className="px-4 py-2 text-slate-300">{d.win_rate_pct.toFixed(1)}%</td>
                <td className={`px-4 py-2 font-semibold ${d.return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {d.return_pct >= 0 ? '+' : ''}{d.return_pct.toFixed(2)}%
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

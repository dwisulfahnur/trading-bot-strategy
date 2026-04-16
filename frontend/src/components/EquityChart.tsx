import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts';
import type { EquityPoint } from '../api/types';

interface Props {
  data: EquityPoint[];
  initialCapital: number;
  onTradeClick?: (tradeIndex: number) => void;
  highlightedTrade?: number | null;
}

const DOT_COLORS: Record<string, string> = {
  tp: '#10b981',
  sl: '#ef4444',
  be: '#f59e0b',
  end_of_data: '#64748b',
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function CustomDot(props: any) {
  const { cx, cy, payload, highlightedTrade } = props;
  if (payload.trade === highlightedTrade) {
    return <circle cx={cx} cy={cy} r={6} fill="#3b82f6" stroke="#fff" strokeWidth={2} />;
  }
  return null;
}

function formatExitTime(iso: string): string {
  // "2025-03-14T10:00:00+00:00" → "Mar 14, 2025 10:00"
  const d = new Date(iso);
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'UTC',
  });
}

function formatTickDate(iso: string): string {
  // Short label for x-axis tick: "Mar 14"
  const d = new Date(iso);
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function CustomTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const p: EquityPoint = payload[0].payload;
  return (
    <div className="bg-slate-800 border border-slate-600 rounded-lg p-3 text-sm shadow-xl">
      <div className="text-slate-400 mb-1">Trade #{p.trade} · {formatExitTime(p.exit_time)}</div>
      <div className="text-slate-100 font-semibold">${p.capital.toLocaleString()}</div>
      <div className="flex gap-2 mt-1">
        <span
          className={`px-1.5 py-0.5 rounded text-xs font-medium ${
            p.direction === 'long' ? 'bg-emerald-900 text-emerald-300' : 'bg-red-900 text-red-300'
          }`}
        >
          {p.direction.toUpperCase()}
        </span>
        <span
          style={{ backgroundColor: DOT_COLORS[p.exit_reason] + '33', color: DOT_COLORS[p.exit_reason] }}
          className="px-1.5 py-0.5 rounded text-xs font-medium"
        >
          {p.exit_reason.toUpperCase()}
        </span>
        <span className={`text-xs ${p.pnl_r >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          {p.pnl_r >= 0 ? '+' : ''}{p.pnl_r.toFixed(2)}R
        </span>
      </div>
    </div>
  );
}

export function EquityChart({ data, initialCapital, onTradeClick, highlightedTrade }: Props) {
  const minVal = Math.min(...data.map((d) => d.capital), initialCapital);
  const maxVal = Math.max(...data.map((d) => d.capital), initialCapital);
  const padding = (maxVal - minVal) * 0.05;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleClick = (d: any) => {
    if (d?.activePayload?.[0] && onTradeClick) {
      onTradeClick((d.activePayload[0].payload as EquityPoint).trade - 1);
    }
  };

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 16, bottom: 0 }} onClick={handleClick}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis
          dataKey="exit_time"
          tick={{ fill: '#64748b', fontSize: 11 }}
          tickLine={false}
          tickFormatter={formatTickDate}
          interval="preserveStartEnd"
          minTickGap={60}
        />
        <YAxis
          domain={[minVal - padding, maxVal + padding]}
          tick={{ fill: '#64748b', fontSize: 11 }}
          tickLine={false}
          tickFormatter={(v) => `$${(v / 1000).toFixed(1)}k`}
          width={60}
        />
        <Tooltip content={<CustomTooltip />} />
        <ReferenceLine y={initialCapital} stroke="#475569" strokeDasharray="4 4" />
        <Line
          type="monotone"
          dataKey="capital"
          stroke="#3b82f6"
          strokeWidth={2}
          dot={<CustomDot highlightedTrade={highlightedTrade} />}
          activeDot={{ r: 5, fill: '#3b82f6' }}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

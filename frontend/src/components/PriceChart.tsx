import { useEffect, useRef, useCallback } from 'react';
import {
  createChart,
  CrosshairMode,
  CandlestickSeries,
  LineSeries,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type LineData,
  type Time,
  type ISeriesMarkersPluginApi,
} from 'lightweight-charts';
import type { OhlcvBar, TradeRecord } from '../api/types';

interface Props {
  bars: OhlcvBar[];
  trades: TradeRecord[];
  highlightedTrade: number | null;
  onTradeClick?: (index: number) => void;
  height?: number;
  emaPeriod?: number;
}

function toTime(isoStr: string): Time {
  return Math.floor(new Date(isoStr).getTime() / 1000) as Time;
}

function computeEMA(bars: OhlcvBar[], period: number): LineData<Time>[] {
  const alpha = 2 / (period + 1);
  const result: LineData<Time>[] = [];
  let ema = 0;
  for (let i = 0; i < bars.length; i++) {
    ema = i === 0 ? bars[i].close : alpha * bars[i].close + (1 - alpha) * ema;
    // Skip warmup period so the initial flat line doesn't show
    if (i >= period - 1) {
      result.push({ time: toTime(bars[i].time), value: ema });
    }
  }
  return result;
}

export function PriceChart({ bars, trades, highlightedTrade, onTradeClick, height = 480, emaPeriod = 200 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const emaSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // Build chart once
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: '#0f1117' },
        textColor: '#94a3b8',
      },
      grid: {
        vertLines: { color: '#1e293b' },
        horzLines: { color: '#1e293b' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#334155' },
      timeScale: {
        borderColor: '#334155',
        timeVisible: true,
        secondsVisible: false,
      },
      width: containerRef.current.clientWidth,
      height,
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981',
      downColor: '#ef4444',
      borderUpColor: '#10b981',
      borderDownColor: '#ef4444',
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444',
    });

    const emaSeries = chart.addSeries(LineSeries, {
      color: '#f59e0b',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    emaSeriesRef.current = emaSeries;

    // Resize observer — tracks both width and height
    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight || height,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      emaSeriesRef.current = null;
      markersPluginRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load candle data + EMA
  useEffect(() => {
    if (!candleSeriesRef.current || bars.length === 0) return;
    const data: CandlestickData[] = bars.map((b) => ({
      time: toTime(b.time),
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
    candleSeriesRef.current.setData(data);
    emaSeriesRef.current?.setData(computeEMA(bars, emaPeriod));
    chartRef.current?.timeScale().fitContent();
  }, [bars, emaPeriod]);

  // Draw trade markers directly on the candlestick series
  useEffect(() => {
    if (!candleSeriesRef.current) return;

    // Detach old plugin
    if (markersPluginRef.current) {
      markersPluginRef.current.detach();
      markersPluginRef.current = null;
    }

    if (trades.length === 0 || bars.length === 0) return;

    const markers = trades.flatMap((t, idx) => {
      const isLong = t.direction === 'long';
      const isHighlighted = idx === highlightedTrade;
      const entryMarker = {
        time: toTime(t.entry_time),
        position: isLong ? ('belowBar' as const) : ('aboveBar' as const),
        color: isHighlighted ? '#3b82f6' : isLong ? '#10b981' : '#ef4444',
        shape: isLong ? ('arrowUp' as const) : ('arrowDown' as const),
        text: `#${t.trade}`,
        size: isHighlighted ? 2 : 1,
      };
      const exitMarker = {
        time: toTime(t.exit_time),
        position: isLong ? ('aboveBar' as const) : ('belowBar' as const),
        color: t.exit_reason === 'tp' ? '#10b981' : t.exit_reason === 'sl' ? '#ef4444' : t.exit_reason === 'be' ? '#f59e0b' : '#64748b',
        shape: 'circle' as const,
        text: t.exit_reason.toUpperCase(),
        size: isHighlighted ? 2 : 1,
      };
      return [entryMarker, exitMarker];
    });

    // Sort by time (required by lightweight-charts)
    markers.sort((a, b) => (a.time as number) - (b.time as number));

    markersPluginRef.current = createSeriesMarkers(candleSeriesRef.current, markers);
  }, [trades, bars, highlightedTrade]);


  const handleContainerClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (!chartRef.current || !onTradeClick) return;
      const rect = containerRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const time = chartRef.current.timeScale().coordinateToTime(x);
      if (!time) return;
      const clickedTs = time as number;
      let bestIdx = -1;
      let bestDiff = Infinity;
      trades.forEach((t, idx) => {
        const ts = Math.floor(new Date(t.entry_time).getTime() / 1000);
        const diff = Math.abs(ts - clickedTs);
        if (diff < bestDiff) {
          bestDiff = diff;
          bestIdx = idx;
        }
      });
      if (bestIdx >= 0 && bestDiff < 86400) onTradeClick(bestIdx);
    },
    [trades, onTradeClick]
  );

  return (
    <div
      ref={containerRef}
      onClick={handleContainerClick}
      style={{ height }}
      className="w-full rounded-xl overflow-hidden border border-slate-700 cursor-crosshair"
    />
  );
}

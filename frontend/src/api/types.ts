export interface ParameterSpec {
  name: string;
  type: 'int' | 'float' | 'bool' | 'str';
  default: number | boolean | string;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
}

export interface StrategyMeta {
  name: string;
  display_name: string;
  parameters: ParameterSpec[];
}

export interface SymbolData {
  timeframes: string[];
  years: number[];
}

export interface DataAvailable {
  symbols: Record<string, SymbolData>;
}

export interface BacktestRequest {
  strategy: string;
  years: number[];
  timeframe: string;
  symbol: string;
  initial_capital: number;
  risk_pct: number;
  compound: boolean;
  breakeven_r: number | null;
  breakeven_sl_r: number;
  commission_per_lot: number;
  max_sl_per_period: number | null;
  sl_period: string;
  params: Record<string, number | boolean | string>;
}

export interface JobStatus {
  job_id: string;
  status: 'running' | 'done' | 'error';
  result_id?: string;
  error?: string;
}

export interface ResultSummary {
  id: string;
  created_at: string;
  strategy: string;
  symbol?: string;
  timeframe: string;
  years: number[];
  total_return_pct: number;
  win_rate_pct: number;
  max_drawdown_pct: number;
  profit_factor: number;
  total_trades: number;
  parameters: Record<string, unknown>;
}

export interface EquityPoint {
  trade: number;
  capital: number;
  direction: 'long' | 'short';
  exit_reason: 'tp' | 'sl' | 'be' | 'end_of_data';
  pnl_r: number;
  exit_time: string;
}

export interface TradeRecord {
  trade: number;
  year: number;
  direction: 'long' | 'short';
  entry_time: string;
  entry_price: number;
  sl: number;
  tp: number;
  exit_time: string;
  exit_price: number;
  exit_reason: 'tp' | 'sl' | 'be' | 'end_of_data';
  pnl_r: number;
  lot_size: number;
  commission_usd: number;
  profit_usd: number;
  capital_after: number;
}

export interface BacktestResults {
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  total_return_pct: number;
  initial_capital: number;
  final_capital: number;
  max_drawdown_pct: number;
  risk_pct: number;
  avg_win_r: number;
  avg_loss_r: number;
  max_consec_wins: number;
  max_consec_losses: number;
  compound: boolean;
  stopped_out: boolean;
  symbol?: string;
  pip_mult?: number;
  per_year: Record<string, { total_trades: number; win_rate_pct: number; return_pct: number }>;
  equity_curve: EquityPoint[];
  trades: TradeRecord[];
}

export interface BacktestResult {
  id: string;
  created_at: string;
  strategy: string;
  parameters: Record<string, unknown>;
  results: BacktestResults;
}

export interface OhlcvBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

export type EAPlatform = 'MT4' | 'MT5';

export interface EARequest {
  result_id: string;
  platform: EAPlatform;
}

export interface EAResponse {
  code: string;
  platform: EAPlatform;
  filename: string;
  prompt: string;
}

import axios from 'axios';
import type {
  AuthUser,
  BacktestRequest,
  BacktestResult,
  DataAvailable,
  EARequest,
  EAResponse,
  JobStatus,
  OhlcvBar,
  ResultSummary,
  StrategyMeta,
  TokenResponse,
} from './types';

const http = axios.create({ baseURL: '/api' });

// Attach Bearer token from localStorage on every request
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

export const api = {
  // Auth
  register: (email: string, password: string) =>
    http.post<TokenResponse>('/auth/register', { email, password }).then((r) => r.data),
  login: (email: string, password: string) =>
    http.post<TokenResponse>('/auth/login', { email, password }).then((r) => r.data),
  me: () => http.get<AuthUser>('/auth/me').then((r) => r.data),

  // Metadata
  getStrategies: () => http.get<StrategyMeta[]>('/strategies').then((r) => r.data),
  getDataAvailable: () => http.get<DataAvailable>('/data/available').then((r) => r.data),

  // Backtest execution
  runBacktest: (req: BacktestRequest) =>
    http.post<JobStatus>('/backtest/run', req).then((r) => r.data),
  getJobStatus: (jobId: string) =>
    http.get<JobStatus>(`/backtest/status/${jobId}`).then((r) => r.data),
  getUnsavedResult: (id: string) =>
    http.get<BacktestResult>(`/backtest/result/${id}`).then((r) => r.data),

  // Results (saved in MongoDB)
  listResults: () => http.get<ResultSummary[]>('/results').then((r) => r.data),
  getResult: (id: string) => http.get<BacktestResult>(`/results/${id}`).then((r) => r.data),
  saveResult: (result_id: string, name: string) =>
    http.post<BacktestResult>('/results/save', { result_id, name }).then((r) => r.data),
  deleteResult: (id: string) => http.delete(`/results/${id}`).then((r) => r.data),
  deleteResults: (ids: string[]) => http.delete('/results', { data: ids }).then((r) => r.data),

  // EA generation
  generateEA: (req: EARequest) =>
    http.post<EAResponse>('/ea/generate', req).then((r) => r.data),

  // OHLCV
  getOhlcv: (timeframe: string, years: number[], symbol: string = 'XAUUSD', dateFrom?: string, dateTo?: string) => {
    const params: Record<string, string> = {
      timeframe,
      years: years.join(','),
      symbol,
    };
    if (dateFrom) params.date_from = dateFrom;
    if (dateTo) params.date_to = dateTo;
    return http.get<OhlcvBar[]>('/ohlcv', { params }).then((r) => r.data);
  },
};

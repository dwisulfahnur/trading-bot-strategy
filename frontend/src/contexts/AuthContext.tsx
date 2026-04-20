import { createContext, useContext, useState, useCallback, type ReactNode } from 'react';
import type { AuthUser, TokenResponse } from '../api/types';

interface AuthContextValue {
  user: AuthUser | null;
  token: string | null;
  setAuth: (data: TokenResponse) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem('access_token'));
  const [user, setUser] = useState<AuthUser | null>(() => {
    const stored = localStorage.getItem('auth_user');
    return stored ? JSON.parse(stored) : null;
  });

  const setAuth = useCallback((data: TokenResponse) => {
    localStorage.setItem('access_token', data.access_token);
    const u: AuthUser = { user_id: data.user_id, email: data.email };
    localStorage.setItem('auth_user', JSON.stringify(u));
    setToken(data.access_token);
    setUser(u);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('access_token');
    localStorage.removeItem('auth_user');
    setToken(null);
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, token, setAuth, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}

// Nur lesende Aufrufe: es gibt ausschließlich GET. Ein Token kommt aus ?token= und bleibt
// in sessionStorage (keine Cookies).
const KEY = 'smart-bridge-ui-token';

export function token(): string | null {
  const fromUrl = new URLSearchParams(location.search).get('token');
  if (fromUrl) sessionStorage.setItem(KEY, fromUrl);
  return sessionStorage.getItem(KEY);
}

export function apiUrl(path: string, params: Record<string, string | number | undefined | null> = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  const s = q.toString();
  return `ui/api${path}${s ? `?${s}` : ''}`;
}

export async function get<T>(path: string, params: Record<string, string | number | undefined | null> = {}): Promise<T> {
  const t = token();
  const res = await fetch(apiUrl(path, params), { headers: t ? { Authorization: `Bearer ${t}` } : {} });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export function eventsUrl(): string {
  const t = token();
  return apiUrl('/events', { token: t });
}

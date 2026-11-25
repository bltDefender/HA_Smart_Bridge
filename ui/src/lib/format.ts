export const TIER_LABEL = ['Stufe 0', 'Stufe 1', 'Stufe 2'];
export const STATUS_LABEL: Record<string, string> = {
  active: 'aktiv',
  disabled: 'deaktiviert',
  shadow: 'shadow',
  error: 'Fehler'
};
export const DOMAIN_ICON: Record<string, string> = {
  light: '💡',
  switch: '🔌',
  lock: '🔒',
  cover: '🪟',
  binary_sensor: '◉',
  sensor: '📈',
  climate: '🌡',
  input_boolean: '☑',
  button: '⏺',
  number: '#',
  select: '▾',
  alarm_control_panel: '🚨'
};

export function icon(domain: string): string {
  return DOMAIN_ICON[domain] ?? '•';
}

export function when(iso: string | null | undefined): string {
  if (!iso) return '–';
  return new Date(iso).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'medium' });
}

export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Einfaches YAML-Highlighting; Ausgabe ist escaped, daher sicher für {@html}.
export function highlightYaml(text: string): string {
  return text
    .split('\n')
    .map((line) => {
      const safe = escapeHtml(line);
      if (/^\s*#/.test(line)) return `<span class="y-c">${safe}</span>`;
      const m = safe.match(/^(\s*(?:- )*)([\w.\-"]+)(:)(\s.*|$)/);
      if (!m) return highlightValue(safe);
      return `${m[1]}<span class="y-k">${m[2]}</span>${m[3]}${highlightValue(m[4])}`;
    })
    .join('\n');
}

function highlightValue(v: string): string {
  return v
    .replace(/(&quot;.*?&quot;|"[^"]*"|'[^']*')/g, '<span class="y-s">$1</span>')
    .replace(/\b(true|false|null)\b/g, '<span class="y-b">$1</span>')
    .replace(/(?<![\w">])(-?\d+(?:\.\d+)?)(?![\w"<])/g, '<span class="y-n">$1</span>');
}

export function diffClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'd-h';
  if (line.startsWith('@@')) return 'd-h';
  if (line.startsWith('+')) return 'd-add';
  if (line.startsWith('-')) return 'd-del';
  return '';
}

// Farbe der Heatmap-Zelle (0 = leer).
export function heat(count: number, max: number): number {
  return count <= 0 || max <= 0 ? 0 : Math.max(0.15, count / max);
}

export function lastDays(days: number, now: Date = new Date()): string[] {
  const out: string[] = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

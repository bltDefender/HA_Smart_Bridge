import { describe, expect, it } from 'vitest';
import { diffClass, escapeHtml, heat, highlightYaml, lastDays } from './format';

describe('highlightYaml', () => {
  it('escapes markup before highlighting', () => {
    const html = highlightYaml('title: "<script>alert(1)</script>"');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });
  it('marks keys, comments and numbers', () => {
    const html = highlightYaml('# Hinweis\nabove: 0.1');
    expect(html).toContain('<span class="y-c"># Hinweis</span>');
    expect(html).toContain('<span class="y-k">above</span>');
    expect(html).toContain('<span class="y-n">0.1</span>');
  });
});

describe('helpers', () => {
  it('classifies diff lines', () => {
    expect(diffClass('+neu')).toBe('d-add');
    expect(diffClass('-alt')).toBe('d-del');
    expect(diffClass('--- v1')).toBe('d-h');
    expect(diffClass(' gleich')).toBe('');
  });
  it('scales the heat value', () => {
    expect(heat(0, 5)).toBe(0);
    expect(heat(5, 5)).toBe(1);
    expect(heat(1, 100)).toBe(0.15);
  });
  it('lists UTC days ending today', () => {
    expect(lastDays(3, new Date('2026-03-01T10:00:00Z'))).toEqual(['2026-02-27', '2026-02-28', '2026-03-01']);
  });
  it('escapes html', () => expect(escapeHtml('<a&b>')).toBe('&lt;a&amp;b&gt;'));
});

import { expect, test, type Page, type Request } from '@playwright/test';

const rule = {
  id: 'regen_alert', title: 'Regen', intent: 'Warne bei Regen', status: 'active', tier: 0, tags: ['wetter'],
  created_by: 'claude', version: 2, target: 'zone.hof', severity: 'warning', areas: ['Hof'],
  has_actions: false, alert_active: true, last_fired: '2026-01-10T11:00:00+00:00'
};
const shadow = { ...rule, id: 'nachts_verriegeln', title: 'Nachts verriegeln', status: 'shadow', tier: 2, alert_active: false };
const graph = {
  rule_id: 'regen_alert', status: 'active', tier: 0, alert_active: true,
  layout_hints: { elk: { 'elk.algorithm': 'layered', 'elk.direction': 'RIGHT' } },
  nodes: [
    { id: 'trigger:derived', kind: 'trigger', type: 'state', label: 'Regenrate ändert sich', column: 0, data: {} },
    { id: 'cond:c', kind: 'condition', type: 'numeric_state', label: 'Regenrate > 0.1', column: 1, data: {} },
    { id: 'entity:sensor.regen_rate', kind: 'entity', type: 'sensor', label: 'Regenrate', column: 2,
      data: { entity_id: 'sensor.regen_rate', domain: 'sensor', area: 'Hof', state: '0.0', stale: false, known: true } },
    { id: 'alert', kind: 'alert', type: 'alert', label: 'Alert zone.hof', column: 3, data: { severity: 'warning' } }
  ],
  edges: [
    { id: 'trigger:derived->cond:c', source: 'trigger:derived', target: 'cond:c', kind: 'flow', label: null },
    { id: 'cond:c->alert', source: 'cond:c', target: 'alert', kind: 'flow', label: null },
    { id: 'cond:c->entity:sensor.regen_rate', source: 'cond:c', target: 'entity:sensor.regen_rate', kind: 'uses', label: null }
  ]
};
const run = {
  run_id: 'r1', rule_id: 'regen_alert', phase: 'on_activate', result: 'ok', duration_ms: 12, executor: 'mcp',
  started_at: '2026-01-10T11:00:00+00:00', trigger: { type: 'state', entity_id: 'sensor.regen_rate' }
};
const traced = {
  ...graph,
  run: {
    run_id: 'r1', phase: 'on_activate', result: 'ok',
    nodes: { 'cond:c': { result: true, detail: '0.3 > 0.1' } },
    highlight: { nodes: ['trigger:derived', 'cond:c', 'alert'], edges: ['trigger:derived->cond:c', 'cond:c->alert'] }
  }
};

async function mockApi(page: Page, writes: string[]) {
  page.on('request', (r: Request) => {
    if (r.url().includes('/ui/api') && r.method() !== 'GET') writes.push(`${r.method()} ${r.url()}`);
  });
  const json = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/ui/api/**', (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname.replace('/ui/api', '');
    if (p === '/events')
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'event: hello\ndata: {}\n\nevent: alerts\ndata: [{"id":"regen_alert"}]\n\n'
      });
    if (p === '/rules') return route.fulfill(json({ rules: [rule, shadow], semantic: false }));
    if (p === '/rules/regen_alert')
      return route.fulfill(json({
        ...rule, yaml: 'rules:\n- id: regen_alert\n  title: "Regen"\n  above: 0.1\n',
        versions: [{ version: 1 }, { version: 2 }], entities: {}
      }));
    if (p === '/rules/regen_alert/graph')
      return route.fulfill(json(url.searchParams.get('run_id') ? traced : graph));
    if (p === '/rules/regen_alert/runs') return route.fulfill(json({ runs: [run] }));
    if (p === '/rules/regen_alert/diff') return route.fulfill(json({ from: 1, to: 2, diff: '--- v1\n+++ v2\n-a: 1\n+a: 2' }));
    if (p === '/entities')
      return route.fulfill(json({ entities: [{ entity_id: 'sensor.regen_rate', name: 'Regenrate', area: 'Hof', domain: 'sensor', state: '0.0', stale: false, rule_count: 1 }] }));
    if (p === '/entities/sensor.regen_rate/rules')
      return route.fulfill(json({ entity_id: 'sensor.regen_rate', entity: { name: 'Regenrate' }, rules: [{ ...rule, roles: ['condition', 'trigger'] }] }));
    if (p === '/overview')
      return route.fulfill(json({
        groups: { area: { Hof: ['regen_alert'] }, tag: { wetter: ['regen_alert'] }, target: { 'zone.hof': ['regen_alert'] } },
        heatmap: { days: 14, since: '2026-01-01T00:00:00+00:00', rules: { regen_alert: { '2026-01-10': 3 } } },
        highlight: { shadow: ['nachts_verriegeln'], error: [] }, rules: [rule, shadow]
      }));
    return route.fulfill({ status: 404, ...json({ detail: 'x' }) });
  });
}

test('list, detail, trace, devices and overview render without writes', async ({ page }) => {
  const writes: string[] = [];
  await mockApi(page, writes);

  await page.goto('/');
  await expect(page.getByTestId('rule-table')).toContainText('Regen');
  await expect(page.getByTestId('rule-table')).toContainText('Stufe 2');
  await expect(page.getByTestId('alert-count')).toHaveText('1 Alert(s)');

  await page.getByRole('link', { name: 'Regen', exact: true }).first().click();
  await expect(page.getByTestId('intent')).toHaveText('Warne bei Regen');
  await expect(page.getByTestId('yaml')).toContainText('above');
  await expect(page.locator('.fnode')).toHaveCount(4);
  await expect(page.locator('.fnode.k-entity')).toContainText('0.0');

  await page.getByRole('button', { name: 'Trace', exact: true }).click();
  await expect(page.locator('.fnode.taken')).toHaveCount(3);
  await expect(page.locator('.fnode.true')).toContainText('0.3 > 0.1');
  await expect(page.locator('.fnode.dim')).toHaveCount(1);

  await page.getByRole('button', { name: 'Diff anzeigen' }).click();
  await expect(page.locator('pre.diff .d-add')).toContainText('+a: 2');

  await page.getByRole('link', { name: 'Geräte' }).click();
  await page.getByRole('link', { name: /Regenrate/ }).click();
  await expect(page.getByTestId('entity-rules')).toContainText('Bedingung, Trigger');

  await page.getByRole('link', { name: 'Übersicht' }).click();
  await expect(page.getByTestId('highlights')).toContainText('Shadow: nachts_verriegeln');
  await expect(page.getByTestId('heatmap')).toContainText('3');
  await page.getByRole('button', { name: 'Tag' }).click();
  await expect(page.getByTestId('groups')).toContainText('wetter');

  expect(writes).toEqual([]); // die UI sendet nur GET
});

test('no external requests', async ({ page }) => {
  const external: string[] = [];
  await mockApi(page, []);
  page.on('request', (r) => {
    if (!r.url().startsWith('http://127.0.0.1:4173')) external.push(r.url());
  });
  await page.goto('/#/overview');
  await expect(page.getByTestId('heatmap')).toBeVisible();
  expect(external).toEqual([]);
});

export interface RuleSummary {
  id: string;
  title: string | null;
  intent: string | null;
  status: string;
  tier: number;
  tags: string[];
  created_by: string;
  version: number;
  target: string | null;
  severity: string | null;
  areas: string[];
  has_actions: boolean;
  alert_active: boolean;
  last_fired: string | null;
  roles?: string[];
}

export interface RuleDetail extends RuleSummary {
  yaml: string;
  versions: { version: number; [k: string]: unknown }[];
  entities: Record<string, string[]>;
}

export interface GraphNode {
  id: string;
  kind: string;
  type: string;
  label: string;
  column: number;
  data: Record<string, any>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: 'flow' | 'branch' | 'uses';
  label: string | null;
}

export interface RunOverlay {
  run_id: string;
  phase: string;
  result: string;
  nodes: Record<string, { result: unknown; detail: string; status?: string; duration_ms?: number }>;
  highlight: { nodes: string[]; edges: string[] };
}

export interface Graph {
  rule_id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  layout_hints: { elk: Record<string, string> };
  run?: RunOverlay;
  status: string;
  tier: number;
  alert_active: boolean;
}

export interface RunRow {
  run_id: string;
  rule_id: string;
  phase: string;
  result: string;
  duration_ms: number;
  executor: string | null;
  started_at: string;
  trigger: { type: string; at?: string; entity_id?: string };
}

export interface EntityRow {
  entity_id: string;
  name: string;
  area: string | null;
  domain: string;
  state: string | null;
  stale: boolean;
  rule_count: number;
}

export interface Overview {
  groups: Record<'area' | 'tag' | 'target', Record<string, string[]>>;
  heatmap: { days: number; since: string; rules: Record<string, Record<string, number>> };
  highlight: { shadow: string[]; error: string[] };
  rules: RuleSummary[];
}

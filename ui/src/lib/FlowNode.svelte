<script lang="ts">
  import { Handle, Position } from '@xyflow/svelte';
  import { icon } from './format';

  let { data }: { data: any } = $props();
  const node = $derived(data.node);
  const trace = $derived(data.trace);
  const live = $derived(data.live ?? node.data.state);
  const stale = $derived(node.data.stale);
</script>

<div
  class="fnode k-{node.kind} t-{node.type}"
  class:dim={data.dim}
  class:taken={data.taken}
  class:true={trace?.result === true}
  class:false={trace?.result === false}
  class:failed={trace?.status === 'error'}
  data-node-id={node.id}
>
  <Handle type="target" position={Position.Left} />
  <div class="row">
    {#if node.kind === 'entity'}<span class="ico">{icon(node.data.domain)}</span>{/if}
    <span class="lbl" title={node.label}>{node.label}</span>
    {#if node.kind === 'entity' && live != null}
      <span class="badge" class:stale>{live}</span>
    {/if}
    {#if node.kind === 'alert'}<span class="badge sev-{node.data.severity}">{node.data.severity}</span>{/if}
  </div>
  {#if node.kind === 'entity' && node.data.area}<div class="sub">{node.data.area}</div>{/if}
  {#if trace}
    <div class="sub trace" title={trace.detail}>
      {trace.status ?? (trace.result === true ? 'wahr' : trace.result === false ? 'falsch' : '–')}
      {#if trace.detail}· {trace.detail}{/if}
    </div>
  {/if}
  <Handle type="source" position={Position.Right} />
</div>

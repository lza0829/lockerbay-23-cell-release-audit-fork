<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const verify = ref(null)
const proofs = ref([])
const selected = ref(null)
const trace = ref(null)

const summary = computed(() => {
  if (!verify.value) return ''
  return verify.value.ok
    ? `全部 ${verify.value.edge_count} 条回收边与占用、核对一致`
    : `${verify.value.failures.length} 项校验失败（缺边 ${verify.value.missing_edge_count}）`
})

async function load() {
  const [edges, proofRows] = await Promise.all([api.reclaimEdges(), api.proofs()])
  verify.value = edges
  proofs.value = proofRows
}

async function openTrace(occupancyId) {
  selected.value = occupancyId
  trace.value = await api.reclaimTrace(occupancyId)
}

onMounted(load)
</script>
<template>
  <div data-plant="23-cell-release-audit-fork" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">
      回收边与占用核对汇总：逐边给出占用 id / 回收原因 / 关闭前后状态 / 小时快照；无边即校验失败。
    </p>

    <div class="panel verdict" :class="verify?.ok ? 'ok' : 'bad'" v-if="verify">
      <b>{{ verify.ok ? '核对通过' : '核对失败' }}</b>
      <span>{{ summary }}</span>
      <button class="ghost" @click="load">重新校验</button>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto" v-if="verify">
      <h2 class="panel-h">回收边</h2>
      <p v-if="!verify.edges.length" class="sub" style="margin:0">暂无回收边。</p>
      <table v-else>
        <thead>
          <tr>
            <th>边</th><th>占用</th><th>原因</th><th>关闭前</th><th>关闭后</th>
            <th>关闭时刻</th><th>小时快照</th><th>核对</th><th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in verify.edges" :key="e.id">
            <td><code>{{ e.id }}</code></td>
            <td><a href="#" @click.prevent="openTrace(e.occupancy_id)"><code>{{ e.occupancy_id }}</code></a></td>
            <td>{{ e.reason }}</td>
            <td>{{ e.status_before }}</td>
            <td>{{ e.status_after }}</td>
            <td>{{ e.closed_at }}</td>
            <td><code>{{ e.hour_bucket }}</code></td>
            <td>#{{ e.proof_id }}</td>
            <td><span class="pill" :class="e.ok ? 'released' : 'occupied'">{{ e.ok ? '一致' : '不一致' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="verify?.failures?.length">
      <h2 class="panel-h">校验失败项</h2>
      <ul class="issues">
        <li v-for="(f, idx) in verify.failures" :key="idx">
          <code>{{ f.code }}</code>
          <code v-if="f.occupancy_id">{{ f.occupancy_id }}</code>
          {{ f.detail }}
        </li>
      </ul>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="trace">
      <h2 class="panel-h">追溯 {{ selected }}</h2>
      <p v-if="!trace.edge" class="sub">
        无回收边{{ trace.reclaim ? '（回收关账缺边，校验失败）' : '' }}。
      </p>
      <table v-else>
        <thead><tr><th>占用</th><th>原因</th><th>前/后</th><th>关闭时刻</th><th>小时快照</th><th>核对关闭</th></tr></thead>
        <tbody>
          <tr>
            <td><code>{{ trace.edge.occupancy_id }}</code></td>
            <td>{{ trace.edge.reason }}</td>
            <td>{{ trace.edge.status_before }} → {{ trace.edge.status_after }}</td>
            <td>{{ trace.edge.closed_at }}</td>
            <td><code>{{ trace.edge.hour_bucket }}</code></td>
            <td>{{ trace.proof?.closed_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <ul v-if="trace.issues.length" class="issues">
        <li v-for="i in trace.issues" :key="i.code"><code>{{ i.code }}</code> {{ i.detail }}</li>
      </ul>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 class="panel-h">占用核对</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>关闭</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.id">
            <td><a href="#" @click.prevent="openTrace(p.occupancy_id)"><code>{{ p.occupancy_id }}</code></a></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td>{{ p.closed_at || '—' }}</td>
            <td><code>{{ p.event_ids_json }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

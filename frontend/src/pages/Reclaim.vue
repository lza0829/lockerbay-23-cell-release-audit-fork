<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const q = ref([])
const candidates = ref([])
const verify = ref(null)
const scan = ref(null)
const asOf = ref('2026-11-20T06:00:00Z')
const message = ref('')
const trace = ref(null)
const busy = ref(false)

const verdict = computed(() => {
  if (!verify.value) return ''
  return verify.value.ok ? '核对一致' : `核对失败 · ${verify.value.failures.length} 项`
})

async function loadAll() {
  const [occ, quar, edges] = await Promise.all([api.occupancies(), api.quarantine(), api.reclaimEdges()])
  rows.value = occ
  q.value = quar
  verify.value = edges
  await loadCandidates()
}

async function loadCandidates() {
  try {
    candidates.value = (await api.reclaimCandidates(asOf.value || undefined)).candidates
  } catch (e) {
    candidates.value = []
    message.value = String(e.message || e)
  }
}

async function runScan() {
  busy.value = true
  message.value = ''
  try {
    scan.value = await api.reclaimScan({ as_of: asOf.value || undefined })
    message.value = `扫描 ${scan.value.scanned} 笔，回收 ${scan.value.reclaimed.length} 笔`
    await loadAll()
  } catch (e) {
    message.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function reclaimOne(id) {
  busy.value = true
  try {
    const r = await api.reclaimOne(id, { as_of: asOf.value || undefined })
    message.value = `已回收 ${r.occupancy_id}，回收边 ${r.edge_id}`
    await loadAll()
  } catch (e) {
    message.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function showTrace(id) {
  trace.value = await api.reclaimTrace(id)
}

async function loadFixture(name) {
  busy.value = true
  try {
    const r = await api.loadReclaimFixture(name)
    message.value = `已装载夹具 ${r.fixture}`
    await loadAll()
  } catch (e) {
    message.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(loadAll)
</script>
<template>
  <div data-plant="23-cell-release-audit-fork" class="plant-mark" style="display:none"></div>

  <div>
    <h1>回收</h1>
    <p class="sub">
      超时未取的占用按同一事务关账：先写不可变回收边，再关占用，核对盖关闭时刻；缺边即核对失败。
    </p>

    <div class="panel toolbar">
      <label>裁决时刻
        <input v-model="asOf" class="ts-input" placeholder="2026-11-20T06:00:00Z" />
      </label>
      <button :disabled="busy" @click="runScan">超时扫描并回收</button>
      <button class="ghost" :disabled="busy" @click="loadFixture('case')">装载回收夹具</button>
      <button class="ghost danger" :disabled="busy" @click="loadFixture('missing-edge')">装载缺边夹具</button>
      <span class="sub" style="margin:0">{{ message }}</span>
    </div>

    <div class="panel verdict" :class="verify?.ok ? 'ok' : 'bad'" v-if="verify">
      <b>{{ verdict }}</b>
      <span>回收边 {{ verify.edge_count }} · 回收关闭占用 {{ verify.reclaim_closed }} · 缺边 {{ verify.missing_edge_count }}</span>
      <span class="sub" style="margin:0">只读核对 · {{ verify.generated_at }}</span>
    </div>

    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">超时候选</h2>
      <p v-if="!candidates.length" class="sub" style="margin:0">当前裁决时刻下无超时未取占用。</p>
      <table v-else>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>截止</th><th></th></tr></thead>
        <tbody>
          <tr v-for="c in candidates" :key="c.occupancy_id">
            <td><code>{{ c.occupancy_id }}</code></td>
            <td>{{ c.cell_id }}</td>
            <td>{{ c.tracking_no }}</td>
            <td>{{ c.opened_at }}</td>
            <td>{{ c.deadline_at }}</td>
            <td><button :disabled="busy" @click="reclaimOne(c.occupancy_id)">回收</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用账</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>状态</th><th>关闭原因</th><th>关闭</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td><code>{{ r.id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td>
              <span class="pill" :class="r.status">{{ r.status }}</span>
            </td>
            <td>{{ r.close_reason || '—' }}</td>
            <td>{{ r.closed_at || '—' }}</td>
            <td><button class="ghost" :disabled="busy" @click="showTrace(r.id)">追边</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem" v-if="trace">
      <h2 style="margin:0 0 .5rem;font-size:1rem">
        追溯 {{ trace.occupancy.id }}
        <span class="pill" :class="trace.ok ? 'released' : 'occupied'">{{ trace.ok ? '一致' : '失败' }}</span>
      </h2>
      <p v-if="!trace.edge" class="sub" style="margin:0">
        {{ trace.reclaim ? '该占用按回收关账，但无回收边。' : '该占用非回收关闭，无回收边。' }}
      </p>
      <table v-if="trace.edge">
        <thead><tr><th>边</th><th>原因</th><th>前/后</th><th>关闭时刻</th><th>小时快照</th><th>核对</th></tr></thead>
        <tbody>
          <tr>
            <td><code>{{ trace.edge.id }}</code></td>
            <td>{{ trace.edge.reason }}</td>
            <td>{{ trace.edge.status_before }} → {{ trace.edge.status_after }}</td>
            <td>{{ trace.edge.closed_at }}</td>
            <td><code>{{ trace.edge.hour_bucket }}</code></td>
            <td>#{{ trace.edge.proof_id }}</td>
          </tr>
        </tbody>
      </table>
      <ul v-if="trace.issues.length" class="issues">
        <li v-for="i in trace.issues" :key="i.code"><code>{{ i.code }}</code> {{ i.detail }}</li>
      </ul>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">搁置</h2>
      <p v-if="!q.length" class="sub">暂无搁置。</p>
      <table v-else>
        <thead><tr><th>id</th><th>原因</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="item in q" :key="item.id">
            <td>{{ item.id }}</td>
            <td>{{ item.reason }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

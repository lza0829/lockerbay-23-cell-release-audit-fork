<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const overview = ref(null)
const bay = ref([])
onMounted(async () => {
  overview.value = await api.overview()
  bay.value = await api.bay()
})
</script>
<template>
  <div data-plant="23-cell-release-audit-fork" class="plant-mark" style="display:none"></div>

  <div>
    <h1>格阵</h1>
    <p class="sub">柜机格口占用态一眼可核；空闲 / 占用由装载事件物化，释放与回收同事务落审计。</p>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.cells }}</b><span>格口</span></div>
      <div class="stat"><b>{{ overview.occupied }}</b><span>占用</span></div>
      <div class="stat"><b>{{ overview.free }}</b><span>空闲</span></div>
      <div class="stat"><b>{{ overview.open_occupancies }}</b><span>开放占用账</span></div>
      <div class="stat" :class="{ bad: !overview.cell_reconciled }">
        <b>{{ overview.cell_reconciled ? '已对账' : overview.ghost_occupied_cells + overview.false_free_cells }}</b>
        <span>格口态对账</span>
      </div>
    </div>
    <div class="panel verdict" :class="overview?.cell_reconciled ? 'ok' : 'bad'" v-if="overview && !overview.cell_reconciled">
      <b>格口态漂移</b>
      <span>幽灵占用角标 {{ overview.ghost_occupied_cells }} · 假空闲 {{ overview.false_free_cells }}；释放核证页可查。</span>
    </div>
    <section v-for="cab in bay" :key="cab.id" class="cabinet panel">
      <h2>{{ cab.label }} <small>{{ cab.id }} · {{ cab.site }}</small></h2>
      <div class="grid">
        <div v-for="cell in cab.cells" :key="cell.id" class="cell" :class="[cell.status, { drift: !cell.reconciled }]">
          <div>
            <div class="code">{{ cell.code }}</div>
            <div class="meta">{{ cell.size_code }} · {{ cell.status === 'occupied' ? '占用' : '空闲' }}</div>
          </div>
          <div class="meta" v-if="cell.status === 'occupied' && cell.occupancy?.tracking_no">{{ cell.occupancy.tracking_no }}</div>
          <div class="meta drift-tag" v-if="!cell.reconciled">
            <code v-for="d in cell.drift" :key="d">{{ d === 'cell_ghost_occupied' ? '幽灵角标' : '假空闲' }}</code>
          </div>
          <span class="dot" />
        </div>
      </div>
    </section>
  </div>
</template>

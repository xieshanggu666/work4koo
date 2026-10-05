/* 视图：应急资源与避难点协同调度 —— 转移负责人分容量 / 物资管理员配车物 / 指挥员调拨到位 */
window.ResourceView = {
  name: "ResourceView",
  data() {
    return {
      overview: { shelters: [], resources: [] },
      orders: [],
      evacuations: [],
      warnings: [],
      zones: [],
      current: null,            // 当前选中处置单
      assignments: { items: [], summary: {} },
      role: "transfer_lead",    // 当前扮演角色
      operatorNames: { transfer_lead: "王转移", material_manager: "赵物资", commander: "陈指挥" },
      plan: { kind: "shelter", target_id: "", zone_id: "", quantity: "" },
      actionNote: "",
      loading: false,
    };
  },
  computed: {
    roles() {
      return [
        { id: "transfer_lead", name: "转移负责人" },
        { id: "material_manager", name: "物资管理员" },
        { id: "commander", name: "指挥员" },
      ];
    },
    shelterTotals() {
      const ss = this.overview.shelters;
      return {
        capacity: ss.reduce((a, s) => a + s.capacity, 0),
        used: ss.reduce((a, s) => a + s.used, 0),
        remaining: ss.reduce((a, s) => a + s.remaining, 0),
      };
    },
    vehicles() { return this.overview.resources.filter(r => r.kind === "vehicle"); },
    materials() { return this.overview.resources.filter(r => r.kind === "material"); },
    planKinds() {
      // 转移负责人分配避难容量；物资管理员分配车辆与物资
      return this.role === "transfer_lead"
        ? [{ id: "shelter", name: "避难容量" }]
        : [{ id: "vehicle", name: "车辆" }, { id: "material", name: "物资" }];
    },
    planTargets() {
      if (this.plan.kind === "shelter") {
        return this.overview.shelters.map(s => ({
          id: s.id, name: `${s.name}（剩余 ${s.remaining} 人）`, disabled: s.remaining <= 0,
        }));
      }
      return this.overview.resources
        .filter(r => r.kind === this.plan.kind)
        .map(r => ({ id: r.id, name: `${r.name}（可规划 ${r.plannable} ${r.unit}）`, disabled: r.plannable <= 0 }));
    },
    linkedEvacs() {
      if (!this.current) return [];
      return this.evacuations.filter(e => e.disposal_id === this.current.id);
    },
    linkedWarns() {
      if (!this.current) return [];
      return this.warnings.filter(w => w.disposal_id === this.current.id);
    },
    plannedCount() { return this.assignments.summary.planned || 0; },
    dispatchedCount() { return this.assignments.summary.dispatched || 0; },
    orderClosed() { return this.current && this.current.status === "completed"; },
  },
  methods: {
    kindText(k) { return { shelter: "避难容量", vehicle: "车辆", material: "物资" }[k] || k; },
    statusText(s) { return { planned: "已规划", dispatched: "已调拨", arrived: "已到位" }[s] || s; },
    statusBadge(s) { return { planned: "orange", dispatched: "blue", arrived: "green" }[s] || "gray"; },
    orderBadge(st) {
      return { initiated: "orange", approved: "blue", executed: "yellow", completed: "green" }[st] || "gray";
    },
    evacColor(st) { return { pending: "orange", moving: "blue", safe: "green" }[st] || "gray"; },
    evacName(st) { return { pending: "待转移", moving: "转移中", safe: "已安全" }[st] || st; },
    async load() {
      this.loading = true;
      try {
        const [ov, orders, evacs, warns, mp] = await Promise.all([
          API.resourceOverview(), API.disposals(), API.evacuations(), API.warnings(), API.map(),
        ]);
        this.overview = ov;
        this.orders = orders;
        this.evacuations = evacs;
        this.warnings = warns;
        this.zones = mp.flood_zones;
        if (this.current) {
          this.current = orders.find(o => o.id === this.current.id) || null;
          if (this.current) await this.loadAssignments();
        }
      } catch (e) {
        window.app.showToast("加载资源调度数据失败：" + e.message);
      } finally {
        this.loading = false;
      }
    },
    async selectOrder(o) {
      this.current = o;
      this.actionNote = "";
      await this.loadAssignments();
    },
    async loadAssignments() {
      if (!this.current) return;
      try {
        this.assignments = await API.disposalResources(this.current.id);
      } catch (e) { window.app.showToast("加载调拨台账失败：" + e.message); }
    },
    onRoleChange() {
      // 切换角色时校正规划类型，避免越权提交
      if (!this.planKinds.some(k => k.id === this.plan.kind)) this.plan.kind = this.planKinds[0].id;
      this.plan.target_id = "";
    },
    async submitPlan() {
      if (!this.current) return;
      const body = {
        operator: this.operatorNames[this.role], role: this.role,
        kind: this.plan.kind, target_id: Number(this.plan.target_id),
        zone_id: this.plan.kind === "shelter" ? Number(this.plan.zone_id) : 0,
        quantity: Number(this.plan.quantity),
      };
      if (!body.target_id || !body.quantity || (this.plan.kind === "shelter" && !body.zone_id)) {
        window.app.showToast("请完整选择调拨目标与数量");
        return;
      }
      try {
        await API.planResource(this.current.id, body);
        window.app.showToast(`处置单 #${this.current.id} 已规划${this.kindText(body.kind)}调拨`);
        this.plan = { kind: this.plan.kind, target_id: "", zone_id: "", quantity: "" };
        await this.load();
        await this.loadAssignments();
      } catch (e) { window.app.showToast("规划失败：" + e.message); }
    },
    async doAction(action) {
      if (!this.current) return;
      const api = action === "dispatch" ? API.dispatchResources : API.arriveResources;
      const verb = action === "dispatch" ? "下达调拨令" : "确认到位";
      try {
        await api(this.current.id, {
          operator: this.operatorNames[this.role], role: this.role,
          note: this.actionNote.trim(),
        });
        window.app.showToast(`处置单 #${this.current.id} ${verb}成功` +
          (action === "arrive" ? "，转移进度与风险预警已回写" : ""));
        this.actionNote = "";
        await this.load();
        await this.loadAssignments();
      } catch (e) { window.app.showToast(`${verb}失败：` + e.message); }
    },
    progressPct(e) {
      return e.people > 0 ? Math.min(100, Math.round((e.arrived_people || 0) / e.people * 100)) : 0;
    },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">应急资源与避难点协同调度
      <span class="sub">转移负责人分容量 · 物资管理员配车辆物资 · 指挥员调拨到位 · 回写转移进度与风险预警</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>

    <!-- 角色切换 -->
    <div class="panel">
      <div class="panel-body" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12.5px;color:#7d95b4">当前值守角色</span>
        <button v-for="r in roles" :key="r.id" class="btn sm"
                :class="{primary: role===r.id}" @click="role=r.id; onRoleChange()">{{ r.name }}</button>
        <div class="field" style="margin-left:auto">
          <label>操作人（电子签名）</label>
          <input v-model="operatorNames[role]" class="role-input" style="min-width:160px"/>
        </div>
      </div>
    </div>

    <!-- 资源总览 -->
    <div class="stats">
      <div class="stat blue"><div class="k">避难点总容量</div><div class="v">{{ shelterTotals.capacity }}<small>人</small></div></div>
      <div class="stat green"><div class="k">已安置</div><div class="v">{{ shelterTotals.used }}<small>人</small></div></div>
      <div class="stat amber"><div class="k">剩余可安置</div><div class="v">{{ shelterTotals.remaining }}<small>人</small></div></div>
      <div class="stat"><div class="k">可用车辆</div><div class="v">{{ vehicles.reduce((a,r)=>a+r.available,0) }}<small>辆/艘</small></div></div>
      <div class="stat"><div class="k">在途调拨</div><div class="v">{{ orders.reduce((a,o)=>a+(o.linked_resources?o.linked_resources.dispatched:0),0) }}<small>条</small></div></div>
    </div>

    <div class="row">
      <!-- 左：避难点与资源库存 -->
      <div class="col col-1">
        <div class="panel">
          <div class="panel-head">避难点容量 <span class="tag">{{ overview.shelters.length }} 处</span></div>
          <div class="panel-body nopad">
            <table class="grid">
              <thead><tr><th>避难点</th><th>总容量</th><th>已安置</th><th>在途占用</th><th>剩余</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="s in overview.shelters" :key="s.id">
                  <td>{{ s.name }}</td>
                  <td class="num">{{ s.capacity }}</td>
                  <td class="num" style="color:var(--ok)">{{ s.used }}</td>
                  <td class="num" style="color:var(--warn)">{{ s.reserved }}</td>
                  <td class="num mono">{{ s.remaining }}</td>
                  <td>
                    <span class="badge" :class="s.status==='open'?'green':(s.status==='full'?'red':'gray')">
                      {{ {open:'开放',full:'已满',closed:'关闭'}[s.status] || s.status }}
                    </span>
                  </td>
                </tr>
                <tr v-if="!overview.shelters.length">
                  <td colspan="6" style="text-align:center;color:#7d95b4;padding:22px">暂无避难点数据</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">应急资源库存 <span class="tag">车辆 · 物资</span></div>
          <div class="panel-body nopad">
            <table class="grid">
              <thead><tr><th>资源</th><th>类型</th><th>总量</th><th>可用</th><th>规划占用</th><th>可规划</th></tr></thead>
              <tbody>
                <tr v-for="r in overview.resources" :key="r.id">
                  <td>{{ r.name }}</td>
                  <td><span class="badge" :class="r.kind==='vehicle'?'blue':'yellow'">{{ r.kind_text }}</span></td>
                  <td class="num">{{ r.total }} {{ r.unit }}</td>
                  <td class="num" style="color:var(--ok)">{{ r.available }} {{ r.unit }}</td>
                  <td class="num" style="color:var(--warn)">{{ r.reserved }} {{ r.unit }}</td>
                  <td class="num mono">{{ r.plannable }} {{ r.unit }}</td>
                </tr>
                <tr v-if="!overview.resources.length">
                  <td colspan="6" style="text-align:center;color:#7d95b4;padding:22px">暂无应急资源数据</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 右：处置单调度台 -->
      <div class="col col-2">
        <div class="panel">
          <div class="panel-head">选择预警处置单 <span class="tag">围绕处置单协同调度</span></div>
          <div class="panel-body nopad" style="max-height:220px;overflow-y:auto">
            <table class="grid">
              <thead><tr><th>单号</th><th>标题</th><th>状态</th><th>调拨(规划/调拨/到位)</th><th></th></tr></thead>
              <tbody>
                <tr v-for="o in orders" :key="o.id" :style="current && current.id===o.id ? 'background:rgba(55,182,255,.07)' : ''">
                  <td class="mono">#{{ o.id }}</td>
                  <td>{{ o.title }}</td>
                  <td><span class="badge" :class="orderBadge(o.status)">{{ o.status_text }}</span></td>
                  <td class="num">{{ o.linked_resources ? o.linked_resources.planned + ' / ' + o.linked_resources.dispatched + ' / ' + o.linked_resources.arrived : '0 / 0 / 0' }}</td>
                  <td style="text-align:right"><button class="btn sm" @click="selectOrder(o)">调度</button></td>
                </tr>
                <tr v-if="!orders.length">
                  <td colspan="5" style="text-align:center;color:#7d95b4;padding:22px">
                    暂无处置单，请先在「处置协同」视图围绕预报运行发起
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <template v-if="current">
          <!-- 调拨台账 -->
          <div class="panel">
            <div class="panel-head">处置单 #{{ current.id }} · 资源调拨台账
              <span class="tag">{{ current.title }}</span>
              <span v-if="orderClosed" class="badge green" style="margin-left:auto">处置单已闭环 · 只读</span>
            </div>
            <div class="panel-body nopad">
              <table class="grid">
                <thead><tr><th>类型</th><th>调拨目标</th><th>安置风险区</th><th>数量</th><th>状态</th><th>经手人</th></tr></thead>
                <tbody>
                  <tr v-for="a in assignments.items" :key="a.id">
                    <td><span class="badge" :class="{shelter:'blue',vehicle:'yellow',material:'gray'}[a.kind]">{{ a.kind_text }}</span></td>
                    <td>{{ a.target_name }}</td>
                    <td>{{ a.zone_name || '—' }}</td>
                    <td class="num mono">{{ a.quantity }}</td>
                    <td><span class="badge" :class="statusBadge(a.status)">{{ a.status_text }}</span></td>
                    <td style="font-size:11.5px;color:#7d95b4">
                      {{ a.planned_by }}<template v-if="a.dispatched_by"> → {{ a.dispatched_by }}</template><template v-if="a.arrived_by"> → {{ a.arrived_by }}</template>
                    </td>
                  </tr>
                  <tr v-if="!assignments.items.length">
                    <td colspan="6" style="text-align:center;color:#7d95b4;padding:22px">尚未规划资源调拨</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- 规划表单 + 指挥员操作 -->
          <div class="panel" v-if="!orderClosed">
            <div class="panel-head">协同调度操作 <span class="tag">规划 → 调拨 → 到位</span></div>
            <div class="panel-body" style="display:flex;flex-direction:column;gap:14px">
              <!-- 规划（转移负责人 / 物资管理员） -->
              <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
                <div class="field">
                  <label>调拨类型</label>
                  <select v-model="plan.kind" class="role-input" @change="plan.target_id=''">
                    <option v-for="k in planKinds" :key="k.id" :value="k.id">{{ k.name }}</option>
                  </select>
                </div>
                <div class="field" style="min-width:220px">
                  <label>调拨目标</label>
                  <select v-model="plan.target_id" class="role-input">
                    <option value="" disabled>请选择</option>
                    <option v-for="t in planTargets" :key="t.id" :value="t.id" :disabled="t.disabled">{{ t.name }}</option>
                  </select>
                </div>
                <div class="field" v-if="plan.kind==='shelter'" style="min-width:180px">
                  <label>安置风险区</label>
                  <select v-model="plan.zone_id" class="role-input">
                    <option value="" disabled>请选择</option>
                    <option v-for="z in zones" :key="z.id" :value="z.id">{{ z.name }}（{{ z.population }} 人）</option>
                  </select>
                </div>
                <div class="field" style="width:130px">
                  <label>数量</label>
                  <input v-model="plan.quantity" type="number" min="1" placeholder="0"/>
                </div>
                <button class="btn primary" :disabled="role==='commander'" @click="submitPlan">
                  ＋ 规划调拨
                </button>
                <span style="font-size:11.5px;color:#7d95b4">
                  避难容量由转移负责人规划，车辆/物资由物资管理员规划；重复规划同一目标只更新数量
                </span>
              </div>
              <!-- 指挥员：调拨令 / 到位确认 -->
              <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;border-top:1px dashed var(--line-soft);padding-top:14px">
                <div class="field" style="flex:1;min-width:240px">
                  <label>指挥员备注（可选）</label>
                  <input v-model="actionNote" placeholder="如：优先保障白水渡城区安置"/>
                </div>
                <button class="btn primary" :disabled="role!=='commander' || !plannedCount" @click="doAction('dispatch')">
                  ▶ 下达调拨令（{{ plannedCount }} 条待调拨）
                </button>
                <button class="btn primary" :disabled="role!=='commander' || !dispatchedCount" @click="doAction('arrive')">
                  ✔ 确认到位（{{ dispatchedCount }} 条在途）
                </button>
              </div>
            </div>
          </div>

          <!-- 回写情况 -->
          <div class="panel">
            <div class="panel-head">到位回写 <span class="tag">转移进度 · 风险预警</span></div>
            <div class="panel-body" style="display:flex;flex-direction:column;gap:12px">
              <div v-for="e in linkedEvacs" :key="e.id" style="display:flex;align-items:center;gap:12px">
                <span style="min-width:110px;font-size:13px">{{ e.zone_name }}</span>
                <div style="flex:1;height:8px;border-radius:4px;background:rgba(125,149,180,.18);overflow:hidden">
                  <div :style="{width: progressPct(e)+'%', height:'100%', borderRadius:'4px',
                                background: e.status==='safe' ? 'var(--ok)' : 'var(--brand)'}"></div>
                </div>
                <span class="num mono" style="font-size:12px;min-width:96px;text-align:right">
                  {{ e.arrived_people || 0 }} / {{ e.people }} 人
                </span>
                <span class="badge" :class="evacColor(e.status)">{{ evacName(e.status) }}</span>
              </div>
              <div v-if="!linkedEvacs.length" style="font-size:12px;color:#7d95b4">
                本处置单尚未挂接转移台账（审核通过后挂接；历史遗留台账保持原样不参与回写）
              </div>
              <div style="font-size:12.5px;color:#7d95b4">
                关联预警 {{ linkedWarns.length }} 条：
                <span style="color:var(--warn)">生效 {{ linkedWarns.filter(w=>w.status==='active').length }}</span> ·
                <span style="color:var(--ok)">已解除 {{ linkedWarns.filter(w=>w.status==='cleared').length }}</span>
                （风险区群众全部安置到位后，其关联预警自动解除）
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>`,
};

/* 视图：联合防汛处置协同 —— 调度员发起 / 预警值守审核 / 转移负责人执行 / 完成闭环 */
window.DisposalView = {
  name: "DisposalView",
  data() {
    return {
      runs: [],
      orders: [],
      current: null,          // 当前选中处置单详情
      role: "dispatcher",     // 当前扮演角色
      operatorNames: { dispatcher: "张调度", duty: "李值守", transfer_lead: "王转移" },
      loading: false,
      actionNote: "",
    };
  },
  computed: {
    roles() {
      return [
        { id: "dispatcher", name: "调度员" },
        { id: "duty", name: "预警值守" },
        { id: "transfer_lead", name: "转移负责人" },
      ];
    },
    steps() {
      return [
        { key: "initiated", name: "发起", role: "调度员", actor: "initiated_by", at: "initiated_at" },
        { key: "approved", name: "审核", role: "预警值守", actor: "reviewed_by", at: "reviewed_at" },
        { key: "executed", name: "执行", role: "转移负责人", actor: "executed_by", at: "executed_at" },
        { key: "completed", name: "完成", role: "转移负责人", actor: "completed_by", at: "completed_at" },
      ];
    },
    availableRuns() {
      // 有处置单的运行展示「查看」，无单的展示「发起」
      const map = {};
      this.orders.forEach(o => { map[o.run_id] = o; });
      return this.runs.map(r => ({ ...r, order: map[r.id] || null }));
    },
  },
  methods: {
    modeText(m) { return { natural: "天然过流", rule: "规则调度", optimized: "联合优化" }[m] || m; },
    statusBadge(st) {
      return { initiated: "orange", approved: "blue", executed: "yellow", completed: "green" }[st] || "gray";
    },
    statusText(st) {
      return { initiated: "待审核", approved: "待执行", executed: "执行中", completed: "已完成" }[st] || st;
    },
    stepIndex(status) {
      return { initiated: 0, approved: 1, executed: 2, completed: 3 }[status] ?? -1;
    },
    async load() {
      this.loading = true;
      try {
        const [runs, orders] = await Promise.all([API.forecastRuns(), API.disposals()]);
        this.runs = runs;
        this.orders = orders;
        if (this.current) {
          this.current = orders.find(o => o.id === this.current.id) || null;
        }
      } catch (e) {
        window.app.showToast("加载协同处置数据失败：" + e.message);
      } finally {
        this.loading = false;
      }
    },
    selectOrder(o) {
      this.current = o;
      this.actionNote = "";
    },
    backToList() { this.current = null; },
    canAct(action) {
      if (!this.current) return false;
      const need = {
        review: { from: "initiated", role: "duty" },
        execute: { from: "approved", role: "transfer_lead" },
        complete: { from: "executed", role: "transfer_lead" },
      }[action];
      return this.current.status === need.from && this.role === need.role;
    },
    async initiate(run) {
      try {
        const o = await API.initiateDisposal(run.id, {
          operator: this.operatorNames.dispatcher, role: "dispatcher",
        });
        window.app.showToast(`处置单 #${o.id} 已发起，待预警值守审核`);
        await this.load();
        this.selectOrder(o);
      } catch (e) { window.app.showToast("发起失败：" + e.message); }
    },
    async act(action) {
      const id = this.current.id;
      const body = { operator: this.operatorNames[this.role], role: this.role };
      const note = this.actionNote.trim();
      if (action === "review") body.opinion = note;
      if (action === "execute") body.note = note;
      if (action === "complete") body.summary = note;
      const api = { review: API.reviewDisposal, execute: API.executeDisposal,
                    complete: API.completeDisposal }[action];
      const verb = { review: "审核通过", execute: "启动执行", complete: "确认完成" }[action];
      try {
        const o = await api(id, body);
        window.app.showToast(`处置单 #${id} ${verb}：${this.statusText(o.status)}`);
        this.actionNote = "";
        await this.load();
        this.selectOrder(this.orders.find(x => x.id === id) || o);
      } catch (e) { window.app.showToast(`${verb}失败：` + e.message); }
    },
    gateSummary(plan) {
      if (!plan || !plan.peak_flow) return "—";
      return `下游峰值 ${fmt.num(plan.peak_flow, 0)} m³/s · 削峰 ${fmt.num(plan.peak_ratio, 1)}% · 拦蓄 ${fmt.num(plan.storage_gain, 0)} 万m³`;
    },
    remarks() {
      return (this.current && this.current.remark ? this.current.remark.split("\n") : []);
    },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">联合防汛处置协同
      <span class="sub">调度员发起 · 预警值守审核 · 转移负责人执行 · 闭环销警</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>

    <!-- 角色切换 -->
    <div class="panel">
      <div class="panel-body" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12.5px;color:#7d95b4">当前值守角色</span>
        <button v-for="r in roles" :key="r.id" class="btn sm"
                :class="{primary: role===r.id}" @click="role=r.id">{{ r.name }}</button>
        <div class="field" style="margin-left:auto">
          <label>操作人（电子签名）</label>
          <input v-model="operatorNames[role]" class="role-input" style="min-width:160px"/>
        </div>
      </div>
    </div>

    <!-- 列表视图 -->
    <template v-if="!current">
      <div class="row">
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">可发起处置的预报运行 <span class="tag">一次运行一单 · 重复发起幂等</span></div>
            <div class="panel-body nopad">
              <table class="grid">
                <thead><tr><th>运行</th><th>降雨情景</th><th>工况</th><th>处置单</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="r in availableRuns" :key="r.id">
                    <td class="mono">#{{ r.id }}</td>
                    <td>{{ r.event_name }}</td>
                    <td><span class="badge gray">{{ modeText(r.mode) }}</span></td>
                    <td>
                      <span v-if="r.order" class="badge" :class="statusBadge(r.order.status)">
                        #{{ r.order.id }} {{ statusText(r.order.status) }}
                      </span>
                      <span v-else class="badge gray">未发起</span>
                    </td>
                    <td style="text-align:right">
                      <button v-if="r.order" class="btn sm" @click="selectOrder(r.order)">查看</button>
                      <button v-else class="btn sm primary" :disabled="role!=='dispatcher'"
                              @click="initiate(r)">发起处置</button>
                    </td>
                  </tr>
                  <tr v-if="!runs.length">
                    <td colspan="5" style="text-align:center;color:#7d95b4;padding:26px">
                      暂无预报运行，请先在「洪水预报」视图执行推演
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div class="panel" v-if="orders.length">
        <div class="panel-head">处置单台账 <span class="tag">{{ orders.length }} 单</span></div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead><tr><th>单号</th><th>标题</th><th>工况</th><th>状态</th><th>预警/转移</th><th>发起时间</th><th></th></tr></thead>
            <tbody>
              <tr v-for="o in orders" :key="o.id" style="cursor:pointer" @click="selectOrder(o)">
                <td class="mono">#{{ o.id }}</td>
                <td>{{ o.title }}</td>
                <td>{{ o.mode_text }}</td>
                <td><span class="badge" :class="statusBadge(o.status)">{{ o.status_text }}</span></td>
                <td class="num">{{ o.linked_warnings }} / {{ o.linked_evacuations }}</td>
                <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(o.initiated_at) }}</td>
                <td style="text-align:right"><button class="btn sm" @click.stop="selectOrder(o)">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- 详情视图 -->
    <template v-else>
      <div class="panel">
        <div class="panel-body">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <button class="btn sm" @click="backToList">← 返回</button>
            <div style="font-size:16px;font-weight:700">处置单 #{{ current.id }} · {{ current.title }}</div>
            <span class="badge" :class="statusBadge(current.status)">{{ current.status_text }}</span>
            <span style="margin-left:auto;font-size:12px;color:#7d95b4">
              预报运行 #{{ current.run_id }} · {{ current.event_name }} · {{ current.mode_text }}
            </span>
          </div>
        </div>
      </div>

      <!-- 四态流转 -->
      <div class="panel">
        <div class="panel-head">协同流转 <span class="tag">发起 → 审核 → 执行 → 完成</span></div>
        <div class="panel-body">
          <div class="flow-steps">
            <template v-for="(s, i) in steps" :key="s.key">
              <div class="flow-step" :class="{done: i <= stepIndex(current.status), active: i === stepIndex(current.status) + 0 && current.status===s.key}">
                <div class="dot">{{ i + 1 }}</div>
                <div class="meta">
                  <div class="name">{{ s.name }}</div>
                  <div class="role">{{ s.role }}</div>
                  <div class="who" v-if="current[s.actor]">{{ current[s.actor] }} · {{ fmt.time(current[s.at]) }}</div>
                </div>
              </div>
              <div class="flow-arrow" v-if="i < steps.length - 1">→</div>
            </template>
          </div>

          <!-- 当前角色待办 -->
          <div style="margin-top:16px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="flex:1;min-width:260px" v-if="canAct('review')||canAct('execute')||canAct('complete')">
              <label>{{ canAct('review') ? '审核意见' : canAct('execute') ? '执行说明' : '完成小结' }}（可选）</label>
              <input v-model="actionNote" :placeholder="canAct('review') ? '如：同意按方案调度，密切监视白水渡水位' : '记录处置情况…'"/>
            </div>
            <button v-if="canAct('review')" class="btn primary" @click="act('review')">✓ 审核通过并回写台账</button>
            <button v-if="canAct('execute')" class="btn primary" @click="act('execute')">▶ 启动转移执行</button>
            <button v-if="canAct('complete')" class="btn primary" @click="act('complete')">✔ 确认完成闭环</button>
            <span v-if="current.status!=='completed' && !(canAct('review')||canAct('execute')||canAct('complete'))"
                  style="font-size:12.5px;color:#7d95b4">
              当前角色（{{ {dispatcher:'调度员',duty:'预警值守',transfer_lead:'转移负责人'}[role] }}）本环节无待办，可切换角色继续流转
            </span>
            <span v-if="current.status==='completed'" class="badge green">处置已闭环</span>
          </div>
        </div>
      </div>

      <!-- 审核方案快照 + 回写情况 -->
      <div class="row" v-if="current.plan">
        <div class="col col-2">
          <div class="panel">
            <div class="panel-head">审核归档调度方案 <span class="tag">{{ current.plan.plan_name }}</span></div>
            <div class="panel-body">
              <div style="font-size:13px;color:#b9cbe2;margin-bottom:10px">{{ current.plan.objective }}</div>
              <div class="stats" style="grid-template-columns:repeat(3,1fr)">
                <div class="stat blue"><div class="k">下游峰值</div><div class="v">{{ fmt.num(current.plan.peak_flow,0) }}<small>m³/s</small></div></div>
                <div class="stat green"><div class="k">削峰率</div><div class="v">{{ fmt.num(current.plan.peak_ratio,1) }}<small>%</small></div></div>
                <div class="stat amber"><div class="k">拦蓄水量</div><div class="v">{{ fmt.num(current.plan.storage_gain,0) }}<small>万m³</small></div></div>
              </div>
              <table class="grid" style="margin-top:12px">
                <thead><tr><th>水库</th><th>洪峰水位(m)</th><th>末水位(m)</th><th>末库容(万m³)</th><th>峰值出流(m³/s)</th></tr></thead>
                <tbody>
                  <tr v-for="r in current.plan.reservoirs" :key="r.id">
                    <td>{{ r.name }}</td>
                    <td class="num mono">{{ fmt.num(r.peak_level,2) }}</td>
                    <td class="num mono">{{ fmt.num(r.final_level,2) }}</td>
                    <td class="num mono">{{ fmt.num(r.final_storage,1) }}</td>
                    <td class="num mono">{{ fmt.num(r.peak_outflow,1) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">台账回写情况 <span class="tag">审核通过时联动</span></div>
            <div class="panel-body" style="display:flex;flex-direction:column;gap:12px;font-size:13px">
              <div class="writeback-item">
                <span class="badge green" v-if="stepIndex(current.status) >= 1">已回写</span>
                <span class="badge gray" v-else>待审核</span>
                <span>水库工况按方案末水位/末库容更新（{{ current.plan.reservoirs.length }} 座）</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="stepIndex(current.status) >= 1">已挂接</span>
                <span class="badge gray" v-else>待审核</span>
                <span>预警台账 {{ current.linked_warnings }} 条关联处置单</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="stepIndex(current.status) >= 1">已挂接</span>
                <span class="badge gray" v-else>待审核</span>
                <span>转移台账 {{ current.linked_evacuations }} 处关联处置单</span>
              </div>
              <div class="writeback-item">
                <span class="badge green" v-if="current.status==='completed'">已闭环</span>
                <span class="badge gray" v-else>待完成</span>
                <span>转移全部到位 · 关联预警统一销警</span>
              </div>
              <div v-if="!current.linked_warnings && !current.linked_evacuations && stepIndex(current.status) >= 1"
                   style="font-size:11.5px;color:#7d95b4;line-height:1.7">
                本次运行无预警/转移台账（历史运行补算不回造台账，历史遗留记录保持原样）。
              </div>
            </div>
          </div>
          <div class="panel" v-if="remarks().length">
            <div class="panel-head">处置记录</div>
            <div class="panel-body" style="font-size:12.5px;color:#b9cbe2;line-height:1.9">
              <div v-for="(line, i) in remarks()" :key="i">{{ line }}</div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>`,
};

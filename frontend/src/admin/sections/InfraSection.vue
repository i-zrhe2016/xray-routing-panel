<script>
// Infrastructure: DNS failover status + manual check/switch, data-plane restart,
// AI routing summary, and the tenant-access overview.

export default {
  name: "InfraSection",
  inject: ["panel"],
  computed: {
    dns() {
      return this.panel.dnsFailoverStatus || {};
    },
    aiNode() {
      return this.panel.aiNodeStatus || {};
    },
    aiNodes() {
      if (Array.isArray(this.panel.aiNodes) && this.panel.aiNodes.length) {
        return this.panel.aiNodes;
      }
      return [this.aiNode];
    },
    trafficRouting() {
      return this.panel.trafficRouting || {};
    },
    trafficNodes() {
      const route = this.trafficRouting;
      const transit = Array.isArray(route.transit_nodes) ? route.transit_nodes : [];
      const names = [route.entry_node || "入口未确认", ...transit, route.exit_node || "出口未确认"];
      return names.map((name, index) => ({
        role: index === 0 ? "入口" : (index === names.length - 1 ? "出口" : "中转"),
        name,
        icon: index === 0 ? "入口" : (index === names.length - 1 ? "出口" : "转发"),
        active: route.path !== "unknown" && (route.waiting_for_switch ? index === 0 : true),
      }));
    },
    peak() {
      return this.dns.peak_window || {};
    },
    peakStatusLabel() {
      if (!this.peak.enabled) return "未启用";
      if (this.peak.config_error) return "配置错误";
      return this.peak.active ? "高峰中 · 优先备用" : "非高峰 · 优先主";
    },
    peakTone() {
      if (!this.peak.enabled) return "warn";
      if (this.peak.config_error) return "bad";
      return this.peak.active ? "ok" : "warn";
    },
    peakDetail() {
      if (!this.peak.enabled) return "未配置高峰窗口切换";
      if (this.peak.config_error) return this.peak.config_error;
      const prefer = this.peak.preferred_target_label || "-";
      const window = `${this.peak.start || "--:--"} - ${this.peak.end || "--:--"}`;
      const tz = this.peak.timezone_label || "服务器本地时区";
      const now = this.peak.current_time ? ` · 现在 ${this.peak.current_time.slice(11, 16)}` : "";
      return `当前优先 ${prefer} · 窗口 ${window} ${tz}${now}`;
    },
    peakNextTime() {
      if (!this.peak.enabled || this.peak.config_error || !this.peak.next_transition_at) return "";
      return this.peak.next_transition_at.slice(11, 16);
    },
    peakCountdown() {
      const total = Number(this.peak.seconds_to_next_transition || 0);
      if (!total) return "";
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      if (hours > 0) return `约 ${hours} 小时 ${minutes} 分后`;
      if (minutes > 0) return `约 ${minutes} 分后`;
      return "即将切换";
    },
    peakNextDetail() {
      if (!this.peakCountdown) return "";
      return `${this.peakCountdown} · 切到 ${this.peak.next_preferred_target_label || "-"}`;
    },
    diag() {
      return this.panel.dataPlaneDiagnosis;
    },
  },
  methods: {
    toneClass(tone) {
      return tone === "ok" ? "ok" : tone === "bad" ? "bad" : "warn";
    },
    summaryTone(ok, total) {
      if (!total) return "warn";
      return ok === total ? "ok" : "bad";
    },
    realityTone(port) {
      if (!port.tcp_reachable) return "warn";
      if (!port.reality) return "warn";
      return port.reality.ok ? "ok" : "bad";
    },
    realityLabel(port) {
      if (!port.tcp_reachable) return "—";
      const reality = port.reality;
      if (!reality) return "未检测";
      if (reality.ok) return "正常";
      if (reality.cert_matches_sni === false) return "证书与 SNI 不符";
      return "失败";
    },
    aiNodeTone(node) {
      return node.reachable ? "ok" : (node.configured ? "bad" : "warn");
    },
    aiNodeActionKey(node) {
      return node.node_id ? `restart-ai-node:${node.node_id}` : "restart-ai-node";
    },
  },
};
</script>

<template>
  <div class="a-section">
    <!-- DNS failover -->
    <div class="a-card">
      <div class="a-card-head">
        <p class="eyebrow">DNS FAILOVER</p>
        <h3>DNS 故障切换</h3>
        <p>仅数据面公网 TCP 探测参与自动切换，AI 节点状态不参与任何 DNS 切换决策。</p>
      </div>
      <div class="a-tiles">
        <div class="a-tile">
          <span>功能状态</span>
          <strong :class="toneClass(panel.dnsFailoverTone(dns))">{{ panel.dnsFailoverSummary(dns) }}</strong>
          <small>{{ dns.enabled ? (dns.config_error || dns.fast_propagation_note) : "未启用自动切换" }}</small>
        </div>
        <div class="a-tile">
          <span>当前 DNS 指向</span>
          <strong>{{ dns.current_target_label || "未知" }}</strong>
          <small>{{ dns.record_name ? ((dns.record_type || "") + " " + dns.record_name) : "未配置记录" }}</small>
        </div>
        <div class="a-tile">
          <span>记录值</span>
          <strong>{{ dns.record_content || "暂无" }}</strong>
          <small>TTL {{ dns.record_ttl || "-" }} 秒 · {{ dns.record_proxied ? "Cloudflare 代理" : "仅 DNS" }}</small>
        </div>
        <div class="a-tile">
          <span>最近探测</span>
          <strong :class="toneClass(panel.dnsFailoverTone(dns))">{{ dns.last_probe_status_label || "未检测" }}</strong>
          <small>{{ dns.last_probe_checked_at_display || "暂无" }}</small>
        </div>
        <div class="a-tile" :class="{ 'peak-active': peak.active }">
          <span>高峰专用节点</span>
          <strong :class="toneClass(peakTone)">{{ peakStatusLabel }}</strong>
          <small>{{ peakDetail }}</small>
        </div>
        <div v-if="peak.enabled && !peak.config_error" class="a-tile">
          <span>下次自动切换</span>
          <strong>{{ peakNextTime || "—" }}</strong>
          <small>{{ peakNextDetail || "暂无" }}</small>
        </div>
        <div class="a-tile">
          <span>探测目标</span>
          <strong>{{ (dns.probe_host || "-") + ":" + (dns.probe_port || "-") }}</strong>
        </div>
        <div class="a-tile">
          <span>连续失败 / 成功</span>
          <strong>{{ (dns.consecutive_failures || 0) + " / " + (dns.consecutive_successes || 0) }}</strong>
        </div>
      </div>
      <div class="a-actions">
        <button class="a-btn secondary" type="button" :disabled="panel.isBusy('dns-failover-check') || !dns.enabled || !dns.configured" @click="panel.runDnsFailoverCheck">
          {{ panel.isBusy("dns-failover-check") ? "检测中..." : "立即检测" }}
        </button>
        <button class="a-btn secondary" type="button" :disabled="panel.isBusy('dns-failover-switch:primary') || !dns.enabled || !dns.configured" @click="panel.switchDnsTarget('primary')">
          {{ panel.isBusy("dns-failover-switch:primary") ? "切换中..." : "切到主" }}
        </button>
        <button class="a-btn secondary" type="button" :disabled="panel.isBusy('dns-failover-switch:backup') || !dns.enabled || !dns.configured" @click="panel.switchDnsTarget('backup')">
          {{ panel.isBusy("dns-failover-switch:backup") ? "切换中..." : "切到备" }}
        </button>
      </div>
    </div>

    <!-- Traffic routing flow -->
    <div class="a-card traffic-routing-card">
      <div class="a-card-head traffic-routing-head">
        <div>
          <p class="eyebrow">TRAFFIC ROUTING</p>
          <h3>流量导向</h3>
          <p>按当前 DNS 入口、节点可达性和 AI 路由状态展示实际路径；虚线节点未参与本次路径。</p>
        </div>
        <span class="traffic-status" :class="trafficRouting.is_degraded ? (trafficRouting.waiting_for_switch ? 'warn' : 'bad') : 'ok'">
          {{ trafficRouting.route_status || "未知" }}
        </span>
      </div>
      <div class="traffic-path" :class="{ 'traffic-path-degraded': trafficRouting.is_degraded }" aria-label="当前流量路径">
        <template v-for="(node, index) in trafficNodes" :key="`${node.role}-${index}`">
          <div class="traffic-node" :class="{ active: node.active, muted: !node.active }">
            <span class="traffic-node-icon" aria-hidden="true">{{ node.icon }}</span>
            <strong>{{ node.role }}</strong>
            <small>{{ node.name }}</small>
            <em>{{ node.active ? "当前经过" : "未经过" }}</em>
          </div>
          <span v-if="index < trafficNodes.length - 1" class="traffic-connector" :class="{ active: node.active && trafficNodes[index + 1].active }" aria-hidden="true">→</span>
        </template>
      </div>
      <div class="traffic-scenario">
        <strong>{{ trafficRouting.label || "状态未知" }}</strong>
        <small>{{ trafficRouting.scenario || "节点状态待确认" }}</small>
      </div>
    </div>

    <!-- AI node -->
    <div class="a-card">
      <div class="a-card-head">
        <p class="eyebrow">AI NODE</p>
        <h3>AI 节点（{{ aiNodes.length }}）</h3>
        <p>远端独立 Xray，分别通过 SSH 纳管；每台节点可单独检查状态和重启。</p>
      </div>
      <div class="ai-node-list">
        <article
          v-for="node in aiNodes"
          :key="node.node_id || 'legacy-ai-node'"
          class="ai-node-card"
          :data-testid="`ai-node-card-${node.node_id || 'legacy'}`"
        >
          <div class="a-tiles">
            <div class="a-tile">
              <span>{{ node.label || "AI 节点" }}</span>
              <strong :class="aiNodeTone(node)">{{ panel.aiNodeStatusLabel(node) }}</strong>
              <small>{{ node.management_target || "未纳管（AI_NODE_SSH_TARGETS 未设置）" }}</small>
            </div>
            <div class="a-tile">
              <span>配置路径</span>
              <strong>{{ node.config_path || "—" }}</strong>
              <small>{{ node.supports_sync ? "支持 SSH 推送" : "不支持配置推送" }}</small>
            </div>
            <div class="a-tile">
              <span>重启能力</span>
              <strong :class="node.supports_restart ? 'ok' : 'warn'">
                {{ node.supports_restart ? "支持" : "不支持" }}
              </strong>
              <small>{{ node.last_error || "SSH 管理通道已纳入状态检查" }}</small>
            </div>
          </div>
          <div class="a-actions">
            <button
              v-if="node.configured && node.supports_restart"
              class="a-btn secondary"
              type="button"
              :data-testid="`restart-ai-node-${node.node_id || 'legacy'}`"
              :aria-label="`重启${node.label || 'AI 节点'}`"
              :disabled="panel.isBusy(aiNodeActionKey(node))"
              @click="panel.restartAiNode(node.node_id || '')"
            >
              {{ panel.isBusy(aiNodeActionKey(node)) ? "重启中..." : `重启${node.label || "AI 节点"}` }}
            </button>
          </div>
        </article>
      </div>
    </div>

    <!-- Data plane + AI -->
    <div class="a-card">
      <div class="a-card-head">
        <p class="eyebrow">DATA PLANE</p>
        <h3>数据面与 AI 路由</h3>
        <p>控制面只管理一个数据面，AI 路由作为该数据面的附属能力统一呈现。</p>
      </div>
      <div class="a-tiles">
        <div class="a-tile">
          <span>{{ panel.dataPlaneStatus.label || "数据面" }}</span>
          <strong :class="panel.dataPlaneStatus.xray_running ? 'ok' : 'bad'">{{ panel.dataPlaneRunningLabel(panel.dataPlaneStatus) }}</strong>
          <small>{{ panel.dataPlaneStatus.management_target || "数据面未配置" }}</small>
        </div>
        <div class="a-tile">
          <span>AI 路由</span>
          <strong :class="toneClass(panel.aiRoutingStatus.status_tone)">{{ panel.aiRoutingLabel(panel.aiRoutingStatus) }}</strong>
          <small>{{ panel.aiRoutingStatus.sync_error || ("最近报告：" + (panel.aiRoutingStatus.report_generated_at_display || "暂无")) }}</small>
        </div>
      </div>
      <div class="a-actions">
        <button
          class="a-btn secondary"
          type="button"
          :disabled="panel.isBusy('diagnose-data-plane')"
          @click="panel.diagnoseDataPlane"
        >
          {{ panel.isBusy("diagnose-data-plane") ? "体检中..." : "数据面体检" }}
        </button>
        <button
          v-if="panel.dataPlaneStatus.configured && panel.dataPlaneStatus.supports_restart"
          class="a-btn secondary"
          type="button"
          :disabled="panel.isBusy('restart-data-plane')"
          @click="panel.restartDataPlane"
        >
          {{ panel.isBusy("restart-data-plane") ? "重启中..." : "重启数据面" }}
        </button>
        <a v-if="panel.meta.ai_domain_dashboard_url" class="a-btn ghost" :href="panel.meta.ai_domain_dashboard_url">打开 AI 域名页</a>
        <a v-if="panel.meta.probe_enabled && panel.meta.probe_dashboard_url" class="a-btn ghost" :href="panel.meta.probe_dashboard_url">打开探针页</a>
      </div>

      <div v-if="diag" class="diag">
        <div class="diag-summary">
          <span :class="toneClass(diag.subscription_profile_available ? 'ok' : 'bad')">
            订阅配置：{{ diag.subscription_profile_available ? "可用" : ("不可用 · " + (diag.subscription_error || "")) }}
          </span>
          <span>节点 {{ diag.node_host || "—" }} · SNI {{ diag.server_name || "—" }} · {{ diag.data_plane_mode }} 模式</span>
          <span :class="toneClass(summaryTone(diag.summary.ports_tcp_ok, diag.summary.ports_total))">
            TCP {{ diag.summary.ports_tcp_ok }}/{{ diag.summary.ports_total }}
          </span>
          <span :class="toneClass(summaryTone(diag.summary.ports_reality_ok, diag.summary.ports_total))">
            Reality {{ diag.summary.ports_reality_ok }}/{{ diag.summary.ports_total }}
          </span>
        </div>

        <div class="diag-block">
          <h4>订阅 ↔ 数据面配置一致性</h4>
          <p v-if="!diag.consistency.available" class="bad">无法比对：{{ diag.consistency.error }}</p>
          <template v-else>
            <p class="diag-src">来源：{{ diag.consistency.source }}</p>
            <div class="diag-table-wrap">
              <table class="diag-table">
                <thead><tr><th>字段</th><th>订阅下发</th><th>数据面实际</th><th>结果</th></tr></thead>
                <tbody>
                  <tr v-for="field in diag.consistency.fields" :key="field.field">
                    <td>{{ field.field }}</td>
                    <td class="mono">{{ field.subscription }}</td>
                    <td class="mono">{{ field.data_plane.join(", ") || "（空）" }}</td>
                    <td :class="field.match ? 'ok' : 'bad'">{{ field.match ? "一致" : "不一致" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>

        <div class="diag-block">
          <h4>端口连通性 / Reality 握手</h4>
          <div class="diag-table-wrap">
            <table class="diag-table">
              <thead><tr><th>端口</th><th>备注</th><th>TCP</th><th>Reality</th><th>回落证书</th></tr></thead>
              <tbody>
                <tr v-for="port in diag.ports" :key="port.listen_port">
                  <td class="mono">{{ port.listen_port }}</td>
                  <td>{{ port.note || "—" }}</td>
                  <td :class="port.tcp_reachable ? 'ok' : 'bad'">{{ port.tcp_reachable ? "通" : ("不通 · " + (port.tcp_error || "")) }}</td>
                  <td :class="toneClass(realityTone(port))">{{ realityLabel(port) }}</td>
                  <td class="mono">{{ port.reality && port.reality.cert_subject_cn ? port.reality.cert_subject_cn : "—" }}</td>
                </tr>
                <tr v-if="!diag.ports.length"><td colspan="5">无启用端口可检测。</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Tenant access overview -->
    <div class="a-card">
      <div class="a-card-head">
        <p class="eyebrow">TENANT ACCESS</p>
        <h3>租户面板与订阅</h3>
        <p>每个有效端口独立生成登录地址、随机凭据和订阅地址；过期端口会自动清理。</p>
      </div>
      <div v-if="panel.subscription.available" class="a-tiles">
        <div class="a-tile"><span>租户数量</span><strong>{{ panel.subscription.tenant_count }}</strong></div>
        <div class="a-tile"><span>客户端目标</span><strong>{{ panel.subscription.server }}</strong></div>
        <div class="a-tile"><span>登录路径示例</span><strong>{{ panel.subscription.tenant_panel_path_example }}</strong></div>
        <div class="a-tile"><span>订阅路径示例</span><strong>{{ panel.subscription.tenant_subscription_path_example }}</strong></div>
      </div>
      <div v-else class="a-empty">
        订阅功能未就绪：{{ panel.subscription.error }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.a-tile.peak-active {
  box-shadow: inset 0 0 0 1px rgba(34, 197, 94, 0.5);
}

.traffic-routing-card {
  overflow: hidden;
}

.traffic-routing-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.traffic-status {
  flex: 0 0 auto;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  color: var(--c-text, #1f2937);
  background: rgba(100, 116, 139, 0.12);
}

.traffic-status.ok { color: var(--c-success, #188038); background: rgba(24, 128, 56, 0.12); }
.traffic-status.warn { color: var(--c-warning, #b06000); background: rgba(176, 96, 0, 0.12); }
.traffic-status.bad { color: var(--c-danger, #d93025); background: rgba(217, 48, 37, 0.12); }

.traffic-path {
  display: flex;
  align-items: stretch;
  gap: 10px;
  overflow-x: auto;
  padding: 16px 2px 12px;
}

.traffic-node {
  min-width: 142px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  border: 1px solid var(--c-primary, #1a73e8);
  border-radius: var(--r-md, 8px);
  background: rgba(26, 115, 232, 0.08);
  color: var(--c-text, #1f2937);
}

.traffic-node.muted {
  border-style: dashed;
  border-color: rgba(100, 116, 139, 0.35);
  background: rgba(100, 116, 139, 0.05);
  opacity: 0.62;
}

.traffic-node-icon {
  color: var(--c-primary, #1a73e8);
  font-size: 11px;
  font-weight: 700;
}

.traffic-node strong { font-size: 14px; }
.traffic-node small, .traffic-node em { color: var(--c-text-muted, #64748b); font-size: 12px; font-style: normal; }
.traffic-node em { color: var(--c-success, #188038); }
.traffic-node.muted em { color: var(--c-text-muted, #64748b); }

.traffic-connector {
  align-self: center;
  color: var(--c-text-muted, #94a3b8);
  font-size: 22px;
}

.traffic-connector.active { color: var(--c-primary, #1a73e8); }

.traffic-flow {
  display: none;
}

.flow-step {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 14px;
  border: 1px solid rgba(148, 163, 184, 0.3);
  border-radius: var(--r-md, 8px);
  background: rgba(148, 163, 184, 0.06);
  opacity: 0.45;
  transition: opacity 0.2s;
}

.flow-step.active {
  opacity: 1;
  border-color: var(--c-primary, #1a73e8);
  background: rgba(26, 115, 232, 0.06);
}

.flow-step .flow-label {
  font-size: 15px;
  font-weight: 600;
}

.flow-step small {
  font-size: 12px;
  color: var(--c-text-muted, #64748b);
}

.flow-arrow {
  font-size: 20px;
  color: var(--c-text-muted, #94a3b8);
}

.flow-arrow.dim {
  opacity: 0.3;
}

.traffic-scenario {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 0 0;
  border-top: 1px solid rgba(148, 163, 184, 0.18);
  margin-top: 8px;
}

.traffic-scenario strong {
  font-size: 15px;
}

.traffic-scenario small {
  font-size: 13px;
  color: var(--c-text-muted, #64748b);
}

.diag {
  margin-top: 16px;
  border-top: 1px solid rgba(148, 163, 184, 0.25);
  padding-top: 16px;
  display: grid;
  gap: 16px;
}

.diag-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  font-size: 13px;
}

.diag-block h4 {
  margin: 0 0 8px;
  font-size: 14px;
}

.diag-src {
  margin: 0 0 8px;
  font-size: 12px;
  opacity: 0.7;
}

.diag-table-wrap {
  /* On phones the multi-column diag tables would otherwise crush each column;
   * let the table keep a readable width and scroll horizontally instead. */
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
.diag-table {
  width: 100%;
  min-width: 520px;
  border-collapse: collapse;
  font-size: 13px;
}

.diag-table th,
.diag-table td {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.18);
  vertical-align: top;
}

.diag-table th {
  font-weight: 600;
  opacity: 0.75;
}

.diag .mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}

.diag .ok {
  color: #16a34a;
}

.diag .bad {
  color: #dc2626;
}

.diag .warn {
  color: #d97706;
}

</style>

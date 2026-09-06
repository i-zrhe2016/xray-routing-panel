import { fallbackCopyText } from "../utils.js";
import { reportClientError, shouldReportHttpError } from "../shared/apiClient.js";

export function sameOriginLoginUrl(value, location = window.location) {
  try {
    const destination = new URL(value || "/login", location.origin);
    return destination.origin === location.origin ? destination.href : "/login";
  } catch (_error) {
    return "/login";
  }
}

// Cross-cutting state and helpers: dashboard application, HTTP/CSRF, busy/flash
// tracking, and clipboard. Domain-specific data/computed/methods live in the
// per-domain mixins; Vue merges them all so `this.x` resolves across mixins.
export const CoreMixin = {
  data() {
    return {
      meta: {},
      summary: {},
      subscription: {},
      dataPlaneStatus: {},
      aiNodeStatus: {},
      aiNodes: [],
      aiRoutingStatus: {},
      dnsFailoverStatus: {},
      nodes: [],
      trafficRouting: {},
      commerceSummary: {},
      commerceSettings: {},
      ports: [],
      plans: [],
      orders: [],
      flash: { message: "", level: "info" },
      busyActions: {},
      copiedKey: "",
      dashboardReady: false,
      routingSignature: "",
      topologyTransitionKey: 0,
      topologyTransitioning: false,
      topologyTransitionTimer: null,
    };
  },

  computed: {
    attentionPortCount() {
      return (
        Number(this.summary.expired_ports || 0) +
        Number(this.summary.quota_ports || 0) +
        Number(this.summary.disabled_ports || 0)
      );
    },

    totalTrafficBytes() {
      return Number(this.summary.total_bytes_received || 0) + Number(this.summary.total_bytes_sent || 0);
    },
  },

  methods: {
    applyDashboard(dashboard) {
      const nextRouting = dashboard.meta?.traffic_routing || {};
      const nextDns = dashboard.meta?.dns_failover_status || {};
      const nextAi = dashboard.meta?.ai_routing_status || {};
      const nextSignature = JSON.stringify({
        path: nextRouting.path || "unknown",
        dnsTarget: nextDns.current_target || "",
        aiMode: nextAi.manual_mode || "auto",
        backupMode: dashboard.meta?.backup_xray_mode || "",
      });
      if (this.dashboardReady && this.routingSignature && this.routingSignature !== nextSignature) {
        this.topologyTransitionKey += 1;
        this.topologyTransitioning = true;
        if (this.topologyTransitionTimer) {
          window.clearTimeout(this.topologyTransitionTimer);
        }
        this.topologyTransitionTimer = window.setTimeout(() => {
          this.topologyTransitioning = false;
          this.topologyTransitionTimer = null;
        }, 1500);
      }
      this.routingSignature = nextSignature;
      this.dashboardReady = true;
      this.meta = dashboard.meta || {};
      this.summary = dashboard.summary || {};
      this.subscription = dashboard.subscription || {};
      this.dataPlaneStatus = dashboard.meta?.data_plane_status || {};
      this.aiNodeStatus = dashboard.meta?.ai_node_status || {};
      this.aiNodes = Array.isArray(dashboard.meta?.ai_nodes)
        ? dashboard.meta.ai_nodes
        : (Array.isArray(this.aiNodeStatus.nodes) ? this.aiNodeStatus.nodes : []);
      this.aiRoutingStatus = dashboard.meta?.ai_routing_status || {};
      this.dnsFailoverStatus = dashboard.meta?.dns_failover_status || {};
      this.nodes = dashboard.meta?.nodes || [];
      this.trafficRouting = dashboard.meta?.traffic_routing || {};
      this.flash = dashboard.flash || { message: "", level: "info" };
      this.ports = (dashboard.ports || []).map((port) => this.preparePort(port));
      this.commerceSummary = dashboard.commerce?.summary || {};
      this.commerceSettings = { ...(dashboard.commerce?.settings || {}) };
      this.plans = (dashboard.commerce?.plans || []).map((plan) => this.preparePlan(plan));
      this.orders = (dashboard.commerce?.orders || []).map((order) => this.prepareOrder(order));
    },

    clearFlash() {
      this.flash = { message: "", level: "info" };
    },

    setFlash(message, level = "info") {
      this.flash = { message, level };
    },

    humanBytes(value) {
      let size = Number(value || 0);
      const units = ["B", "KB", "MB", "GB", "TB", "PB"];
      for (let index = 0; index < units.length; index += 1) {
        const unit = units[index];
        if (size < 1024 || unit === units[units.length - 1]) {
          if (unit === "B") {
            return `${Math.trunc(size)} ${unit}`;
          }
          return `${size.toFixed(2)} ${unit}`;
        }
        size /= 1024;
      }
      return "0 B";
    },

    isBusy(key) {
      return Boolean(this.busyActions[key]);
    },

    async runAction(key, callback) {
      if (this.isBusy(key)) {
        return;
      }
      this.busyActions[key] = true;
      try {
        await callback();
      } catch (error) {
        this.setFlash(error.message || "操作失败。", "error");
      } finally {
        delete this.busyActions[key];
      }
    },

    async requestJson(url, options = {}) {
      const headers = {
        Accept: "application/json",
        ...(options.headers || {}),
      };
      const method = String(options.method || "GET").toUpperCase();
      if (method !== "GET" && this.meta?.csrf_token) {
        headers["X-CSRF-Token"] = this.meta.csrf_token;
      }
      if (options.body !== undefined) {
        headers["Content-Type"] = "application/json";
      }
      let response;
      try {
        response = await fetch(url, {
          method,
          headers,
          body: options.body,
          credentials: "same-origin",
        });
      } catch (error) {
        reportClientError({ url, method, error, csrfToken: this.meta?.csrf_token, source: "fetch" });
        throw error;
      }
      let rawText;
      try {
        rawText = await response.text();
      } catch (error) {
        reportClientError({ url, method, error, csrfToken: this.meta?.csrf_token, status: response.status, source: "response.read" });
        throw error;
      }
      let data = {};
      if (rawText) {
        try {
          data = JSON.parse(rawText);
        } catch (_error) {
          const error = new Error(`服务返回了无法解析的响应（${response.status}）。`);
          reportClientError({ url, method, error, csrfToken: this.meta?.csrf_token, status: response.status, source: "response.parse" });
          if (response.status === 401) {
            window.location.assign("/login");
            throw new Error("登录已失效，请重新登录。");
          }
          throw error;
        }
      }
      if (response.status === 401) {
        window.location.assign(sameOriginLoginUrl(data.login_url));
        const error = new Error(data.message || "登录已失效，请重新登录。");
        reportClientError({ url, method, error, csrfToken: this.meta?.csrf_token, status: 401, source: "http" });
        throw error;
      }
      if (!response.ok || data.ok === false) {
        const error = new Error(data.message || `请求失败（${response.status}）。`);
        error.status = response.status;
        error.payload = data;
        if (shouldReportHttpError(response.status)) {
          reportClientError({ url, method, error, csrfToken: this.meta?.csrf_token, status: response.status, source: "http" });
        }
        throw error;
      }
      return data;
    },

    applyResponse(data) {
      if (data.dashboard) {
        this.applyDashboard(data.dashboard);
      }
      if (data.message && !data.dashboard) {
        this.setFlash(data.message, data.level || "success");
      }
    },

    copyLabel(key) {
      return this.copiedKey === key ? "已复制" : "复制";
    },

    async copy(value, key) {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(value);
        } else if (!fallbackCopyText(value)) {
          throw new Error("复制失败。");
        }
        this.copiedKey = key;
        window.setTimeout(() => {
          if (this.copiedKey === key) {
            this.copiedKey = "";
          }
        }, 1200);
      } catch (_error) {
        this.setFlash("浏览器未允许复制，请手动复制。", "error");
      }
    },

    aiNodeStatusLabel(status = this.aiNodeStatus) {
      const s = status || {};
      if (!s.configured) return "未纳管";
      if (s.any_reachable && s.all_reachable === false) return "部分可用";
      if (s.reachable) return "运行中";
      return "不可达";
    },

    async restartAiNode(nodeId = "") {
      const actionKey = nodeId ? `restart-ai-node:${nodeId}` : "restart-ai-node";
      const endpoint = nodeId
        ? `/api/ai-nodes/${encodeURIComponent(nodeId)}/restart`
        : "/api/ai-node/restart";
      await this.runAction(actionKey, async () => {
        const data = await this.requestJson(endpoint, { method: "POST" });
        this.applyResponse(data);
      });
    },
  },
};

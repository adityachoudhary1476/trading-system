import type {
  AccountResponse,
  AllocationResponse,
  ApiError,
  ApiResult,
  AutonomousBotResponse,
  AutonomousDecideResponse,
  AutonomousDeploymentsResponse,
  AutonomousEventsResponse,
  AutonomousLifecycleResponse,
  AutonomousScanResponse,
  CircuitBreakerResponse,
  DashboardSnapshotResponse,
  DeploymentCreateResponse,
  DeploymentListResponse,
  DeploymentResponse,
  EventsResponse,
  EvidenceResponse,
  ExportResponse,
  HealthEndpointResponse,
  HealthResponse,
  OrderIntentResponse,
  PaperOrderIntent,
  PerformanceResponse,
  Phase22StrategySpec,
  PositionsResponse,
  RegimeResponse,
  RiskResponse,
  SessionResponse,
} from "@/types/paper-api";

const DEFAULT_BASE = "";
const PAPER_API_PREFIX = "/api/paper";

function isLoopbackUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "[::1]" || host === "::1";
  } catch {
    // Not an absolute URL — e.g. a relative path. Those are same-origin and safe.
    return !url.startsWith("http://") && !url.startsWith("https://");
  }
}

function baseUrl(): string {
  const envUrl = typeof import.meta !== "undefined" && (import.meta as any).env?.VITE_PAPER_API_URL;
  // VITE_PAPER_API_URL is embedded at build time and is therefore PUBLIC.
  // Only honour it for loopback (local dev) targets so the browser can never
  // bypass the Vercel proxy → PYTHON_BACKEND_URL chain in production.
  // In production leave it unset: the browser uses same-origin /api/paper/*
  // which Vercel routes to api/paper/[...path].ts.
  if (envUrl && isLoopbackUrl(envUrl)) return envUrl;
  return DEFAULT_BASE;
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<ApiResult<T>> {
  const url = `${baseUrl()}${PAPER_API_PREFIX}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(init?.headers ?? {}),
      },
      // Prevent accidental credential caching.
      cache: "no-store",
    });
  } catch (err) {
    return {
      ok: false,
      error: { code: "network_error", message: "Network request failed" },
      status: 0,
    };
  }

  const contentType = res.headers.get("content-type") ?? "";
  let body: unknown;
  try {
    if (contentType.includes("application/json")) {
      body = await res.json();
    } else {
      body = null;
    }
  } catch {
    body = null;
  }

  if (!res.ok) {
    const err = parseError(body);
    return { ok: false, error: err, status: res.status };
  }

  return { ok: true, data: body as T };
}

function parseError(body: unknown): ApiError {
  if (
    body &&
    typeof body === "object" &&
    "error" in body &&
    body.error &&
    typeof body.error === "object"
  ) {
    const e = body.error as Partial<ApiError> & { code?: unknown };
    return {
      code: typeof e.code === "string" ? e.code : "unknown_error",
      message: typeof e.message === "string" ? e.message : "Unknown error",
      details:
        typeof e.details === "object" && e.details !== null
          ? (e.details as Record<string, unknown>)
          : undefined,
    };
  }
  return { code: "unknown_error", message: "Unexpected error response" };
}

export function get<T>(path: string): Promise<ApiResult<T>> {
  return request<T>(path);
}

export function post<T>(path: string, body?: unknown): Promise<ApiResult<T>> {
  return request<T>(path, {
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export const paperApi = {
  health: () => get<HealthEndpointResponse>("/health"),

  listDeployments: (params?: {
    deployment_id?: string;
    strategy_id?: string;
    symbol?: string;
    timeframe?: string;
    status?: string;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.deployment_id) qs.set("deployment_id", params.deployment_id);
    if (params?.strategy_id) qs.set("strategy_id", params.strategy_id);
    if (params?.symbol) qs.set("symbol", params.symbol);
    if (params?.timeframe) qs.set("timeframe", params.timeframe);
    if (params?.status) qs.set("status", params.status);
    if (params?.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return get<DeploymentListResponse>(`/deployments${q ? `?${q}` : ""}`);
  },

  createDeployment: (payload: {
    spec?: Record<string, unknown>;
    strategy_id?: string;
    symbol?: string;
    timeframe?: string;
    dataset_id?: string;
    config?: Record<string, unknown>;
  }) => post<DeploymentCreateResponse>("/deployments", payload),

  getDeployment: (deploymentId: string) =>
    get<DeploymentResponse>(`/deployments/${encodeURIComponent(deploymentId)}`),

  getSession: (deploymentId: string) =>
    get<SessionResponse>(`/deployments/${encodeURIComponent(deploymentId)}/session`),

  checkpoint: (deploymentId: string, label?: string) =>
    post<SessionResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/checkpoint`,
      label !== undefined ? { label } : undefined,
    ),

  restore: (deploymentId: string, attachRunner = false) =>
    post<SessionResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/restore`,
      { attach_runner: attachRunner },
    ),

  getAccount: (deploymentId: string) =>
    get<AccountResponse>(`/deployments/${encodeURIComponent(deploymentId)}/account`),

  getPositions: (deploymentId: string) =>
    get<PositionsResponse>(`/deployments/${encodeURIComponent(deploymentId)}/positions`),

  getPerformance: (deploymentId: string) =>
    get<PerformanceResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/performance`,
    ),

  getHealth: (deploymentId: string) =>
    get<HealthResponse>(`/deployments/${encodeURIComponent(deploymentId)}/health`),

  getRisk: (deploymentId: string) =>
    get<RiskResponse>(`/deployments/${encodeURIComponent(deploymentId)}/risk`),

  getCircuitBreaker: (deploymentId: string) =>
    get<CircuitBreakerResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/circuit-breaker`,
    ),

  resetCircuitBreaker: (deploymentId: string) =>
    post<CircuitBreakerResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/reset-circuit-breaker`,
    ),

  getEvents: (deploymentId: string, params?: {
    event_type?: string;
    since_sequence?: number;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.event_type) qs.set("event_type", params.event_type);
    if (params?.since_sequence !== undefined)
      qs.set("since_sequence", String(params.since_sequence));
    if (params?.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return get<EventsResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/events${q ? `?${q}` : ""}`,
    );
  },

  getEvidence: (deploymentId: string) =>
    get<EvidenceResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/evidence`,
    ),

  getDashboard: (deploymentId: string) =>
    get<DashboardSnapshotResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/dashboard`,
    ),

  exportJson: (deploymentId: string) =>
    get<ExportResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/export`,
    ),

  activate: (deploymentId: string) =>
    post<DeploymentResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/activate`,
    ),

  pause: (deploymentId: string) =>
    post<DeploymentResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/pause`,
    ),

  resume: (deploymentId: string) =>
    post<DeploymentResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/resume`,
    ),

  stop: (deploymentId: string) =>
    post<DeploymentResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/stop`,
    ),

  submitOrder: (deploymentId: string, order: PaperOrderIntent) =>
    post<OrderIntentResponse>(
      `/deployments/${encodeURIComponent(deploymentId)}/orders`,
      order,
    ),

  // Phase 22 — Adaptive Multi-Strategy Market Intelligence
  getStrategies: () => get<Phase22StrategySpec[]>("/strategies"),

  getRegime: (params?: { symbol?: string; timeframe?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.symbol) qs.set("symbol", params.symbol);
    if (params?.timeframe) qs.set("timeframe", params.timeframe);
    if (params?.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return get<RegimeResponse>(`/regime${q ? `?${q}` : ""}`);
  },

  getAllocation: (params?: { symbol?: string; timeframe?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.symbol) qs.set("symbol", params.symbol);
    if (params?.timeframe) qs.set("timeframe", params.timeframe);
    if (params?.limit) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return get<AllocationResponse>(`/allocation${q ? `?${q}` : ""}`);
  },

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    const result = await request<T>(path, init);
    if (!result.ok) throw new Error(result.error.message);
    return result.data;
  },

  // Phase 6 — Autonomous Trading Operations Center
  async getAutonomousBot(): Promise<AutonomousBotResponse> {
    return this.request<AutonomousBotResponse>("/autonomous/bot")
  },

  async setAutonomousBotLifecycle(
    action: "start" | "pause" | "resume" | "stop"
  ): Promise<AutonomousLifecycleResponse> {
    return this.request<AutonomousLifecycleResponse>(
      `/autonomous/bot/${action}`,
      { method: "POST" }
    )
  },

  async getAutonomousScan(): Promise<AutonomousScanResponse> {
    return this.request<AutonomousScanResponse>("/autonomous/scan")
  },

  async getAutonomousDecisions(): Promise<AutonomousDecideResponse> {
    return this.request<AutonomousDecideResponse>(
      "/autonomous/decide",
      { method: "POST" }
    )
  },

  async getAutonomousDeployments(): Promise<AutonomousDeploymentsResponse> {
    return this.request<AutonomousDeploymentsResponse>("/autonomous/deployments")
  },

  async stopAutonomousDeployment(deploymentId: string): Promise<{
    status: string
    message: string
    schema_version?: number
  }> {
    return this.request<{ status: string; message: string; schema_version?: number }>(
      `/autonomous/deployments/${deploymentId}/stop`,
      { method: "POST" }
    )
  },

  async getAutonomousEvents(params?: {
    eventType?: string
    limit?: number
  }): Promise<AutonomousEventsResponse> {
    const query = new URLSearchParams()
    if (params?.eventType) query.set("event_type", params.eventType)
    if (params?.limit) query.set("limit", String(params.limit))
    const qs = query.toString()
    return this.request<AutonomousEventsResponse>(
      `/autonomous/events${qs ? `?${qs}` : ""}`
    )
  },
};

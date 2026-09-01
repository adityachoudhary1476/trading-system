import type {
  AccountResponse,
  ApiError,
  ApiResult,
  CircuitBreakerResponse,
  DashboardSnapshotResponse,
  DeploymentListResponse,
  DeploymentResponse,
  EventsResponse,
  EvidenceResponse,
  ExportResponse,
  HealthEndpointResponse,
  HealthResponse,
  PerformanceResponse,
  PositionsResponse,
  RiskResponse,
  SessionResponse,
} from "@/types/paper-api";

const DEFAULT_BASE = "http://127.0.0.1:8765";

function baseUrl(): string {
  const envUrl = typeof import.meta !== "undefined" && (import.meta as any).env?.VITE_PAPER_API_URL;
  if (envUrl) return envUrl;
  return DEFAULT_BASE;
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<ApiResult<T>> {
  const url = `${baseUrl()}${path}`;
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
};

export type DeploymentStatus =
  | "created"
  | "active"
  | "paused"
  | "stopped"
  | "failed"
  | "checkpointed"
  | "restored";

export type SessionStatus =
  | "created"
  | "active"
  | "paused"
  | "stopped"
  | "failed"
  | "checkpointed"
  | "restored";

export type HealthStatus = "healthy" | "warning" | "halted";

export type CircuitState = "closed" | "open";

export type RiskDecision = "allow" | "warning" | "halt";

export type EventType =
  | "bar_processed"
  | "order_submitted"
  | "fill_received"
  | "order_rejected"
  | "signal_generated"
  | "health_warning"
  | "circuit_breaker_tripped"
  | "circuit_breaker_reset"
  | "checkpoint_saved"
  | "session_restored"
  | "deployment_activated"
  | "deployment_paused"
  | "deployment_resumed"
  | "deployment_stopped";

export type EvidenceType =
  | "research"
  | "walk_forward"
  | "paper_trading";

export interface DashboardDeploymentSummary {
  deployment_id: string;
  strategy_id: string;
  strategy_spec_hash: string;
  symbol: string;
  timeframe: string;
  execution_mode: string;
  dataset_id: string;
  status: DeploymentStatus;
  created_at: string;
  activated_at: string | null;
  updated_at: string;
  notes: string;
  schema_version: number;
}

export interface DashboardStrategySummary {
  strategy_id: string;
  spec_hash: string;
  symbol: string;
  timeframe: string;
  lifecycle_status: string;
  generated_by: string;
  name: string;
  description: string;
  parent_strategy_id: string;
  latest_evidence_at: string | null;
  latest_research_evidence_id: string | null;
  latest_walk_forward_evidence_id: string | null;
  latest_paper_evidence_id: string | null;
}

export interface DashboardAccountBlock {
  initial_cash: number;
  cash: number;
  equity: number;
  margin_used: number;
  available_cash: number;
  realized_pnl: number;
  unrealized_pnl: number;
  starting_equity: number | null;
  total_return: number | null;
}

export interface DashboardPositionsBlock {
  open_position: {
    symbol: string;
    quantity: number;
    side: string;
    entry_price: number;
    current_price: number;
    unrealized_pnl: number;
    position_value: number;
  } | null;
  is_flat: boolean;
}

export interface DashboardPerformanceBlock {
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  total_pnl: number | null;
  return: number | null;
  drawdown: number | null;
  trade_count: number;
  win_rate: number | null;
  profit_factor: number | null;
  exposure: number | null;
  health_status: string;
  orders_submitted: number;
  fills_received: number;
  rejected_orders: number;
  generated_signals: number;
  bar_count: number;
}

export interface DashboardHealthBlock {
  status: HealthStatus;
  halt_reason: string | null;
  warnings: string[];
}

export interface DashboardRiskBlock {
  decision: RiskDecision;
  reason: string | null;
}

export interface DashboardCircuitBreakerBlock {
  state: CircuitState;
  reason: string | null;
  trip_count: number;
}

export interface DashboardEventSummary {
  total_events: number;
  last_event_sequence: number;
  last_event_type: string | null;
  last_event_timestamp: string | null;
  recent: Array<{
    sequence: number;
    timestamp: string;
    event_type: string;
    deployment_id: string;
    strategy_id: string;
    message: string;
    [key: string]: unknown;
  }>;
}

export interface DashboardEvidenceSummary {
  research_count: number;
  walk_forward_count: number;
  paper_trading_count: number;
  latest_research_evidence_id: string | null;
  latest_walk_forward_evidence_id: string | null;
  latest_paper_trading_evidence_id: string | null;
  latest_research_at: string | null;
  latest_walk_forward_at: string | null;
  latest_paper_trading_at: string | null;
}

export interface PaperSession {
  session_id: string;
  deployment_id: string;
  strategy_id: string;
  strategy_spec_hash: string;
  symbol: string;
  timeframe: string;
  execution_mode: string;
  dataset_id: string;
  session_status: SessionStatus;
  deployment_status: DeploymentStatus;
  created_at: string;
  updated_at: string;
  last_processed_bar_timestamp: string | null;
  bar_count: number;
  generated_signals: number;
  orders_submitted: number;
  fills_received: number;
  rejected_orders: number;
  starting_equity: number | null;
  current_equity: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  max_drawdown: number | null;
  health_status: HealthStatus;
  halt_reason: string | null;
  consecutive_errors: number;
  circuit_state: CircuitState;
  circuit_reason: string | null;
  circuit_trip_count: number;
  event_count: number;
  event_sequence: number;
  broker_state: Record<string, unknown>;
  operations_state_json: Record<string, unknown>;
  schema_version: number;
}

export interface PaperSessionCheckpoint {
  checkpoint_id: string;
  session_id: string;
  deployment_id: string;
  strategy_id: string;
  strategy_spec_hash: string;
  symbol: string;
  timeframe: string;
  execution_mode: string;
  dataset_id: string;
  schema_version: number;
  deployment_status: DeploymentStatus;
  session_status: SessionStatus;
  last_processed_bar_timestamp: string | null;
  bar_count: number;
  orders_submitted: number;
  fills_received: number;
  rejected_orders: number;
  generated_signals: number;
  consecutive_errors: number;
  starting_equity: number | null;
  current_equity: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  max_drawdown: number | null;
  health_status: HealthStatus;
  halt_reason: string | null;
  circuit_state: CircuitState;
  circuit_reason: string | null;
  circuit_trip_count: number;
  event_count: number;
  event_sequence: number;
  broker_state: Record<string, unknown>;
  operations_state_json: Record<string, unknown>;
  events_fingerprint: string;
  ops_fingerprint: string;
  created_at: string;
}

export interface DeploymentListResponse {
  deployments: DashboardDeploymentSummary[];
  count: number;
  schema_version: number;
}

export interface DeploymentResponse {
  deployment: DashboardDeploymentSummary;
  schema_version: number;
}

export interface DeploymentCreateResponse {
  deployment: DashboardDeploymentSummary;
  session_id: string;
  schema_version: number;
}

export interface SessionResponse {
  session: PaperSession;
  checkpoint: PaperSessionCheckpoint | null;
  schema_version: number;
}

export interface AccountResponse {
  account: DashboardAccountBlock;
  schema_version: number;
}

export interface PositionsResponse {
  positions: DashboardPositionsBlock;
  schema_version: number;
}

export interface PerformanceResponse {
  performance: DashboardPerformanceBlock;
  schema_version: number;
}

export interface HealthResponse {
  health: DashboardHealthBlock;
  schema_version: number;
}

export interface RiskResponse {
  risk: DashboardRiskBlock;
  schema_version: number;
}

export interface CircuitBreakerResponse {
  circuit_breaker: DashboardCircuitBreakerBlock;
  schema_version: number;
}

export interface EventsResponse {
  events: DashboardEventSummary;
  schema_version: number;
}

export interface EvidenceResponse {
  evidence: DashboardEvidenceSummary;
  schema_version: number;
}

export interface HealthEndpointResponse {
  status: string;
  phase: string;
  paper_only: boolean;
  schema_version: number;
}

export interface DashboardSnapshotResponse {
  generated_at: string;
  deployment: DashboardDeploymentSummary;
  strategy: DashboardStrategySummary | null;
  session: PaperSession;
  account: DashboardAccountBlock;
  positions: DashboardPositionsBlock;
  performance: DashboardPerformanceBlock;
  health: DashboardHealthBlock;
  risk: DashboardRiskBlock;
  circuit_breaker: DashboardCircuitBreakerBlock;
  recent_events: DashboardEventSummary;
  evidence_summary: DashboardEvidenceSummary;
  schema_version: number;
  session_schema_version: number;
}

export interface ExportResponse {
  dashboard_snapshot: DashboardSnapshotResponse;
  report?: Record<string, unknown>;
}

export interface PaperOrderIntent {
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  order_type?: "MARKET" | "LIMIT";
  limit_price?: number;
  client_order_id?: string;
  current_price?: number;
}

export interface OrderIntentFill {
  fill_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  fee: number;
}

export interface OrderIntentResponse {
  order_id: string;
  client_order_id: string | null;
  symbol: string;
  side: string;
  quantity: number;
  order_type: string;
  limit_price: number | null;
  status: string;
  filled_quantity: number;
  avg_fill_price: number;
  fills: OrderIntentFill[];
  cash_after: number | null;
  equity_after: number | null;
  realized_pnl_after: number | null;
  unrealized_pnl_after: number | null;
  position_qty_after: number | null;
  reject_reason: string;
  idempotent: boolean;
  schema_version: number;
}

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface ErrorResponse {
  error: ApiError;
  request_id: string | null;
  timestamp: string;
  schema_version: number;
}

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiError; status: number };

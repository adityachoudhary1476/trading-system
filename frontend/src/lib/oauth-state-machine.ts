export interface StateRow {
  id: string;
  user_id: string;
  expires_at: string;
  consumed_at: string | null;
}

export interface StateValidationResult {
  ok: boolean;
  reason?: string;
  userId?: string;
  stateId?: string;
}

export function validateOAuthState(params: {
  state: string | undefined;
  code: string | undefined;
  upstoxError: string | undefined;
  getSession: () => Promise<{ row: StateRow | null; error: string | null }>;
}): Promise<StateValidationResult> {
  if (params.upstoxError) {
    return Promise.resolve({ ok: false, reason: params.upstoxError });
  }
  if (!params.code || !params.state) {
    return Promise.resolve({ ok: false, reason: "missing_params" });
  }
  return params.getSession().then(({ row, error }) => {
    if (error || !row) return { ok: false, reason: "invalid_state" };
    if (row.consumed_at) return { ok: false, reason: "state_reused" };
    if (new Date(row.expires_at).getTime() < Date.now()) return { ok: false, reason: "state_expired" };
    return { ok: true, userId: row.user_id, stateId: row.id };
  });
}

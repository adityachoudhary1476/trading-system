import { useEffect } from "react";
import type { ReactNode } from "react";

export function Panel({
  title,
  actions,
  toolbar,
  children,
  className,
  subtle,
}: {
  title?: string;
  actions?: ReactNode;
  toolbar?: ReactNode;
  children: ReactNode;
  className?: string;
  subtle?: boolean;
}) {
  return (
    <section className={`panel${subtle ? " panel-subtle" : ""}${className ? ` ${className}` : ""}`}>
      {title && (
        <div className="panel-head">
          <span className="panel-title">{title}</span>
          {actions && <div className="head-actions">{actions}</div>}
        </div>
      )}
      {toolbar && <div className="panel-toolbar">{toolbar}</div>}
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: "pos" | "neg" | "muted" }) {
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className={`value${tone ? ` ${tone}` : ""}`}>{value}</span>
    </div>
  );
}

export function Badge({ kind, children, title }: { kind: string; children?: ReactNode; title?: string }) {
  return (
    <span className={`badge ${kind}`} title={title}>
      {children ?? kind.replace(/_/g, " ")}
    </span>
  );
}

export function Pill({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: "pos" | "neg" | "warn";
}) {
  return <span className={`pill${tone ? ` ${tone}` : ""}`}>{children}</span>;
}

export function EmptyState({
  title,
  hint,
  icon = "○",
}: {
  title: string;
  hint?: string;
  icon?: string;
}) {
  return (
    <div className="empty">
      <div className="empty-icon" aria-hidden="true">{icon}</div>
      <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>{title}</div>
      {hint && <div style={{ fontSize: 12 }}>{hint}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="empty">
      <div className="spinner" />
      <div>{label}</div>
    </div>
  );
}

export function Skeleton({ lines = 3, blocks = 0 }: { lines?: number; blocks?: number }) {
  return (
    <div role="status" aria-label="Loading">
      {Array.from({ length: blocks }).map((_, i) => (
        <div key={`b${i}`} className="skel skel-block" />
      ))}
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={`l${i}`}
          className="skel skel-line"
          style={{ width: `${70 + ((i * 13) % 25)}%` }}
        />
      ))}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function Kpi({
  label,
  value,
  sub,
  tone,
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "pos" | "neg" | "warn";
  icon?: ReactNode;
}) {
  return (
    <div className={`kpi${tone ? ` ${tone}` : ""}`}>
      <div>
        <div className="kpi-label">{label}</div>
        <div className="kpi-value">{value}</div>
        {sub && <div className="kpi-sub">{sub}</div>}
      </div>
      {icon && <div className="kpi-icon" aria-hidden="true">{icon}</div>}
    </div>
  );
}

/** Toast: simple, auto-dismissing notification. Render <ToastHost /> once at app root. */
export type ToastKind = "info" | "pos" | "neg" | "warn";
export interface ToastItem {
  id: number;
  title: string;
  message?: string;
  kind?: ToastKind;
  ttl?: number;
}

export function Toast({ toast, onClose }: { toast: ToastItem; onClose: (id: number) => void }) {
  useEffect(() => {
    const t = setTimeout(() => onClose(toast.id), toast.ttl ?? 4000);
    return () => clearTimeout(t);
  }, [toast.id, toast.ttl, onClose]);
  const kind = toast.kind ?? "info";
  return (
    <div className={`toast ${kind === "info" ? "" : kind}`} role="status">
      <div style={{ flex: 1 }}>
        <div className="t-title">{toast.title}</div>
        {toast.message && <div className="t-msg">{toast.message}</div>}
      </div>
      <button className="t-x" onClick={() => onClose(toast.id)} aria-label="Dismiss">×</button>
    </div>
  );
}

export function ToastHost({ toasts, onClose }: { toasts: ToastItem[]; onClose: (id: number) => void }) {
  if (!toasts.length) return null;
  return (
    <div className="toast-host" aria-live="polite">
      {toasts.map((t) => (
        <Toast key={t.id} toast={t} onClose={onClose} />
      ))}
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  healthy: "var(--positive)",
  connected: "var(--positive)",
  ready: "var(--accent)",
  stale: "var(--warning)",
  disconnected: "var(--negative)",
  auth_error: "var(--negative)",
  invalid_data: "var(--negative)",
};

/** Status dot + (optional) text. Used in System pipeline + Data Health. */
export function HealthDot({ status, pulse }: { status: string; pulse?: boolean }) {
  const color = STATUS_COLOR[status] ?? "var(--neutral)";
  return (
    <span
      className={`health-dot${pulse && status === "healthy" ? " pulse" : ""}`}
      style={{ background: color, boxShadow: `0 0 0 3px ${color}22` }}
      aria-hidden="true"
    />
  );
}

/* ====================================================================== */
/* Paper Trading components                                                */
/* ====================================================================== */

/** Consistent button with variant + size. */
export function Button({
  children,
  variant = "secondary",
  size,
  onClick,
  disabled,
  title,
  className,
  type = "button",
  autoFocus,
}: {
  children: ReactNode;
  variant?: "primary" | "secondary" | "danger" | "danger-solid" | "warning" | "ghost";
  size?: "sm" | "xs";
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  className?: string;
  type?: "button" | "submit";
  autoFocus?: boolean;
}) {
  const cls = `btn btn-${variant}${size ? ` btn-${size}` : ""}${className ? ` ${className}` : ""}`;
  return (
    <button type={type} className={cls} onClick={onClick} disabled={disabled} title={title} autoFocus={autoFocus}>
      {children}
    </button>
  );
}

/** Accessible confirmation modal. */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="modal-overlay" onClick={onCancel} role="presentation">
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head" id="modal-title">{title}</div>
        {message && <div className="modal-body">{message}</div>}
        <div className="modal-foot">
          <Button variant="secondary" onClick={onCancel} autoFocus={false}>{cancelLabel}</Button>
          <Button
            variant={danger ? "danger-solid" : "primary"}
            onClick={onConfirm}
            autoFocus
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Standardized status indicator with dot + label. */
export function StatusIndicator({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  const statusClass = status.replace(/_/g, "-");
  return (
    <span className={`status-ind status-${statusClass}${className ? ` ${className}` : ""}`}>
      <span className="status-dot" aria-hidden="true" />
      {status.toUpperCase()}
    </span>
  );
}

/** Compact metric item for metric-grid. */
export function MetricItem({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: "pos" | "neg" | "muted";
}) {
  return (
    <div className="mg-item">
      <span className="mg-label">{label}</span>
      <span className={`mg-value${tone ? ` ${tone}` : ""}`}>{value}</span>
    </div>
  );
}

/** Inline feedback message. */
export function Feedback({
  kind = "info",
  children,
}: {
  kind?: "info" | "success" | "error" | "processing";
  children: ReactNode;
}) {
  const cls = `feedback fb-${kind}`;
  return <div className={cls} role="status">{children}</div>;
}

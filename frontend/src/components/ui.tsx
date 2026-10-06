import { useEffect, useId, useState, type ReactNode } from "react";
import { Film, Image as ImageIcon } from "lucide-react";
import type { Tone } from "../lib/labels";
import type { MediaType } from "../api/types";
import { formatPercent } from "../lib/format";

export function Spinner({ label }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label ?? "Carregando"} />;
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Carregando">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${90 - (i % 3) * 15}%` }} />
      ))}
    </div>
  );
}

export function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`pill tone-${tone}`}>{children}</span>;
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className="switch">
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="switch-track" aria-hidden="true" />
      <span>{label}</span>
    </label>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? <span className="field-error">{error}</span> : hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function ErrorLine({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="alert err" role="alert">
      <span className="grow">{message}</span>
      {onRetry ? (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          Tentar de novo
        </button>
      ) : null}
    </div>
  );
}

export function ProgressBar({ value, inline }: { value: number; inline?: boolean }) {
  const pct = formatPercent(value);
  return (
    <span
      className={`progress${inline ? " inline" : ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(Math.min(1, Math.max(0, value)) * 100)}
      style={inline ? undefined : { display: "block" }}
    >
      <span style={{ width: pct }} />
    </span>
  );
}

/** Barra com a parte concluída com sucesso e a parte com erro (frações de 0 a 1). */
export function SplitBar({ ok, err }: { ok: number; err: number }) {
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  const okPct = clamp(ok);
  const errPct = Math.min(clamp(err), 1 - okPct);
  return (
    <span
      className="progress"
      style={{ display: "flex" }}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round((okPct + errPct) * 100)}
    >
      <span style={{ width: `${okPct * 100}%` }} />
      {errPct > 0 ? <span className="err" style={{ width: `${errPct * 100}%` }} /> : null}
    </span>
  );
}

export function Thumb({ src, mediaType, size = 40 }: { src: string | null; mediaType: MediaType | null; size?: number }) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const showImg = src !== null && failedSrc !== src;
  const Icon = mediaType === "image" ? ImageIcon : Film;
  return (
    <span className="thumb" style={{ width: size, height: size }}>
      {showImg ? (
        <img src={src} alt="" loading="lazy" onError={() => setFailedSrc(src)} />
      ) : (
        <Icon size={16} strokeWidth={1.5} aria-hidden="true" />
      )}
    </span>
  );
}

export function Modal({
  title,
  onClose,
  children,
  actions,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
}) {
  const titleId = useId();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId}>{title}</h2>
        {children}
        {actions ? <div className="modal-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal
      title={title}
      onClose={busy ? () => undefined : onCancel}
      actions={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancelar
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn-danger-solid" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={busy}
            autoFocus
          >
            {busy ? <Spinner /> : null}
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className="muted">{children}</div>
    </Modal>
  );
}

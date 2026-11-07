import { useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Film, Image as ImageIcon, Inbox, type LucideIcon } from "lucide-react";
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

/** Estado vazio: ícone, título e (opcional) orientação ou ação do próximo passo. */
export function Empty({ title, children, icon: Icon = Inbox }: { title: string; children?: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="empty">
      <span className="empty-icon" aria-hidden="true">
        <Icon size={20} strokeWidth={1.6} />
      </span>
      <strong>{title}</strong>
      {children}
    </div>
  );
}

/** Cabeçalho da página: título grande, subtítulo curto e a ação principal à direita. */
export function PageHeader({ title, subtitle, children }: { title: ReactNode; subtitle?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div className="page-head-text">
        <h1>{title}</h1>
        {subtitle ? <p className="page-sub">{subtitle}</p> : null}
      </div>
      {children ? <div className="row page-actions">{children}</div> : null}
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

/**
 * No navegador o diálogo vai para o fim do <body> (fora de superfícies com vidro fosco, que prenderiam o
 * `position: fixed`). Sem DOM (renderização no servidor dos testes) ele fica no lugar, para a marcação ser conferida.
 */
function ModalPortal({ children }: { children: ReactNode }) {
  return typeof document === "undefined" ? <>{children}</> : <Dialog.Portal>{children}</Dialog.Portal>;
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
  // Radix Dialog: prende o foco no diálogo, devolve o foco ao fechar, fecha só o diálogo de cima com Esc e
  // esconde o resto da página dos leitores de tela (a região dos avisos continua anunciada).
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <ModalPortal>
        <Dialog.Overlay className="overlay">
          <Dialog.Content
            className={`modal${wide ? " wide" : ""}`}
            aria-modal="true"
            aria-describedby={undefined}
            onPointerDownOutside={(e) => {
              // Clicar num aviso (toast) não fecha o diálogo.
              if ((e.target as Element | null)?.closest?.(".toasts")) e.preventDefault();
            }}
          >
            <Dialog.Title>{title}</Dialog.Title>
            {children}
            {actions ? <div className="modal-actions">{actions}</div> : null}
          </Dialog.Content>
        </Dialog.Overlay>
      </ModalPortal>
    </Dialog.Root>
  );
}

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  cancelLabel = "Cancelar",
  focusCancel,
  danger,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** Rótulo do botão que desiste (ex.: "Voltar" quando a ação confirmada é um cancelamento). */
  cancelLabel?: string;
  /** Foco inicial no botão que desiste (seguro), em vez do botão de confirmar. */
  focusCancel?: boolean;
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
          <button type="button" className="btn" onClick={onCancel} disabled={busy} autoFocus={focusCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn-danger-solid" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={busy}
            autoFocus={!focusCancel}
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

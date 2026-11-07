import { useState } from "react";
import { api, capcutJobFileUrl, messageOf } from "../api/client";
import type {
  CapCutBitrate,
  CapCutDraft,
  CapCutFps,
  CapCutJob,
  CapCutJobStatus,
  CapCutResolution,
  CapCutSettings,
  CapCutStatus,
  CapCutStyle,
  CapCutWhisperModel,
} from "../api/types";
import { ConfirmDialog, Empty, ErrorLine, Field, Modal, Pill, ProgressBar, SkeletonRows, Spinner, Switch, Thumb } from "../components/ui";
import {
  CAPCUT_STATUS_LABEL,
  capcutPollMs,
  capcutSettingsPatch,
  capcutTone,
  isCapcutActive,
  jobNote,
  jobProgress,
  stageLabel,
  styleSummaryText,
  whisperModelText,
} from "../lib/capcut";
import { formatCount, formatDateTime } from "../lib/format";
import { nextPollMs } from "../lib/polling";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

const PAGE_SIZE = 50;

const RESOLUTIONS: { value: CapCutResolution; label: string }[] = [
  { value: "original", label: "Igual ao original" },
  { value: "480P", label: "480p" },
  { value: "720P", label: "720p" },
  { value: "1080P", label: "1080p" },
  { value: "2K", label: "2K" },
  { value: "4K", label: "4K" },
];
const BITRATES: { value: CapCutBitrate; label: string }[] = [
  { value: "low", label: "Menor" },
  { value: "recommended", label: "Recomendada" },
  { value: "high", label: "Maior" },
];
const FPS_OPTIONS: { value: CapCutFps; label: string }[] = [
  { value: "60", label: "60 fps (padrão)" },
  { value: "50", label: "50 fps" },
  { value: "30", label: "30 fps" },
  { value: "25", label: "25 fps" },
  { value: "24", label: "24 fps" },
  { value: "original", label: "Igual ao original" },
];
const WHISPER_MODELS: { value: CapCutWhisperModel; label: string }[] = [
  { value: "small", label: "Rápido (small)" },
  { value: "medium", label: "Equilibrado (medium)" },
  { value: "large-v3-turbo", label: "Mais preciso (large-v3-turbo)" },
];
const STATUS_FILTERS: { value: CapCutJobStatus | ""; label: string }[] = [
  { value: "", label: "Todos" },
  { value: "pending", label: CAPCUT_STATUS_LABEL.pending },
  { value: "running", label: CAPCUT_STATUS_LABEL.running },
  { value: "waiting", label: CAPCUT_STATUS_LABEL.waiting },
  { value: "completed", label: CAPCUT_STATUS_LABEL.completed },
  { value: "failed", label: CAPCUT_STATUS_LABEL.failed },
  { value: "cancelled", label: CAPCUT_STATUS_LABEL.cancelled },
];

export function CapCutPage() {
  const [statusPoll, setStatusPoll] = useState<number>(() => nextPollMs(null, false));
  const status = useQuery(
    "capcut-status",
    () =>
      api.capcutStatus().then((s) => {
        setStatusPoll((p) => nextPollMs(p, s.queue.running + s.queue.pending + s.queue.waiting > 0));
        return s;
      }),
    statusPoll,
  );
  const settings = useQuery("capcut-settings", () => api.capcutSettings());

  return (
    <div className="page">
      <div className="page-head">
        <h1>Legendas CapCut</h1>
      </div>

      <StatusSection status={status.data} error={status.error} onReload={status.reload} />

      <StylesSection />

      <section className="section">
        <div className="section-head">
          <h2>Configurações</h2>
        </div>
        {settings.data ? (
          <SettingsForm
            key={JSON.stringify(settings.data)}
            initial={settings.data}
            models={status.data?.models}
            onSaved={() => {
              settings.reload();
              status.reload();
            }}
          />
        ) : settings.error ? (
          <ErrorLine message={settings.error} onRetry={settings.reload} />
        ) : (
          <SkeletonRows rows={4} />
        )}
      </section>

      <JobsSection onChanged={status.reload} />
    </div>
  );
}

/* ---------------- Status ---------------- */

function StatusSection({
  status,
  error,
  onReload,
}: {
  status: CapCutStatus | undefined;
  error: string | null;
  onReload: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function toggleQueue(paused: boolean) {
    setBusy(true);
    try {
      if (paused) await api.resumeCapcutQueue();
      else await api.pauseCapcutQueue();
      toast.push("ok", paused ? "Fila de legendas retomada." : "Fila de legendas pausada.");
      onReload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2>Status</h2>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        Durante a exportação o Media Tool traz o CapCut para a frente e usa o mouse por alguns segundos. Se outra janela
        estiver na frente, ele tenta de novo.
      </p>
      {status === undefined ? (
        error ? (
          <ErrorLine message={error} onRetry={onReload} />
        ) : (
          <SkeletonRows rows={3} />
        )
      ) : (
        <>
          {!status.available ? (
            <div className="alert err" role="alert" style={{ marginBottom: 10 }}>
              <span className="grow">{status.reason ?? "As Legendas CapCut não estão disponíveis."}</span>
            </div>
          ) : null}
          <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
            <StatusItem
              label="CapCut"
              ok={status.installed}
              text={status.installed ? `Detectado${status.version ? ` (versão ${status.version})` : ""}` : "Não encontrado"}
            />
            <StatusItem
              label="Dependências"
              ok={Object.values(status.deps).length > 0 && Object.values(status.deps).every(Boolean)}
              text={
                Object.entries(status.deps)
                  .map(([name, ok]) => `${name} ${ok ? "ok" : "faltando"}`)
                  .join(", ") || "—"
              }
            />
            <StatusItem
              label="Skill (capcut-agent-skill)"
              ok={Boolean(status.skill.installed)}
              text={status.skill.installed ? `Instalada${status.skill.commit ? ` (${status.skill.commit.slice(0, 8)})` : ""}` : "Não instalada"}
            />
            <StatusItem
              label="Modelo de transcrição"
              ok={Boolean(status.models[status.settings.whisper_model])}
              text={whisperModelText(status.models, status.settings.whisper_model)}
            />
          </div>
          <div className="row between" style={{ marginTop: 12 }}>
            <span className="muted small" aria-live="polite">
              Fila {status.queue.paused ? "pausada" : "ativa"}: {formatCount(status.queue.running)} em andamento,{" "}
              {formatCount(status.queue.pending)} na fila, {formatCount(status.queue.waiting)} aguardando o CapCut,{" "}
              {formatCount(status.queue.failed)} com erro.
            </span>
            <button
              type="button"
              className="btn btn-sm"
              disabled={busy}
              onClick={() => void toggleQueue(status.queue.paused)}
            >
              {status.queue.paused ? "Retomar" : "Pausar"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function StatusItem({ label, ok, text }: { label: string; ok: boolean; text: string }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="row" style={{ gap: 8 }}>
        <Pill tone={ok ? "ok" : "warn"}>{ok ? "OK" : "Atenção"}</Pill>
        <span className="small">{text}</span>
      </span>
    </div>
  );
}

/* ---------------- Modelos de legenda ---------------- */

function StylesSection() {
  const toast = useToast();
  const styles = useQuery("capcut-styles", () => api.capcutStyles());
  const [list, setList] = useState<CapCutStyle[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<CapCutStyle | null>(null);
  const [busy, setBusy] = useState(false);
  const items = list ?? styles.data ?? null;

  async function makeDefault(style: CapCutStyle) {
    setBusy(true);
    try {
      setList(await api.setDefaultCapcutStyle(style.id));
      toast.push("ok", `"${style.name}" é o modelo padrão.`);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(style: CapCutStyle) {
    setBusy(true);
    try {
      await api.deleteCapcutStyle(style.id);
      setList(await api.capcutStyles());
      toast.push("ok", "Modelo excluído.");
      setDeleting(null);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2>Modelo de legenda</h2>
        <button
          type="button"
          className="btn btn-sm"
          style={{ marginLeft: "auto" }}
          onClick={() => setAdding(true)}
        >
          Salvar modelo do CapCut
        </button>
      </div>
      {items === null ? (
        styles.error ? (
          <ErrorLine message={styles.error} onRetry={styles.reload} />
        ) : (
          <SkeletonRows rows={2} />
        )
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Modelo</th>
                <th>Resumo</th>
                <th>Origem</th>
                <th aria-label="Ações" />
              </tr>
            </thead>
            <tbody>
              {items.map((style) => (
                <tr key={style.id}>
                  <td>
                    {style.name} {style.is_default ? <Pill tone="accent">Padrão</Pill> : null}
                  </td>
                  <td className="muted small">{styleSummaryText(style.summary)}</td>
                  <td className="muted small">{style.builtin ? "Embutido" : (style.source_draft ?? "—")}</td>
                  <td className="nowrap" style={{ textAlign: "right" }}>
                    {!style.is_default ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={busy}
                        onClick={() => void makeDefault(style)}
                      >
                        Usar como padrão
                      </button>
                    ) : null}
                    {!style.builtin ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={busy}
                        onClick={() => setDeleting(style)}
                      >
                        Excluir
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {adding ? (
        <SaveStyleDialog
          onClose={() => setAdding(false)}
          onSaved={async () => {
            setAdding(false);
            setList(await api.capcutStyles().catch(() => null));
            styles.reload();
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmDialog
          title="Excluir modelo"
          confirmLabel="Excluir"
          danger
          busy={busy}
          onConfirm={() => void remove(deleting)}
          onCancel={() => setDeleting(null)}
        >
          <p>
            Excluir o modelo "{deleting.name}"? Os vídeos já legendados não mudam.
            {deleting.is_default ? " O padrão volta a ser o estilo embutido." : ""}
          </p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

function SaveStyleDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const drafts = useQuery("capcut-drafts", () => api.capcutDrafts());
  const [draftName, setDraftName] = useState("");
  const [segment, setSegment] = useState(0);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const chosen: CapCutDraft | undefined = drafts.data?.find((d) => d.name === draftName);

  async function save() {
    setBusy(true);
    try {
      await api.createCapcutStyle({ name: name.trim(), draft_name: draftName, segment_index: segment });
      toast.push("ok", "Modelo salvo.");
      onSaved();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Salvar modelo do CapCut"
      onClose={busy ? () => undefined : onClose}
      wide
      actions={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !draftName || !name.trim()}
            onClick={() => void save()}
          >
            {busy ? <Spinner /> : null}
            Salvar
          </button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        No CapCut, crie um projeto com uma legenda no estilo desejado (fonte, cores, contorno, animação, modelo de
        legenda). Para definir a cor do destaque, pinte uma palavra com essa cor. Feche o CapCut e escolha o projeto
        aqui.
      </p>
      {drafts.error ? <ErrorLine message={drafts.error} onRetry={drafts.reload} /> : null}
      <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
        <Field label="Projeto do CapCut" htmlFor="capcut-draft">
          <select
            id="capcut-draft"
            className="select"
            value={draftName}
            onChange={(e) => {
              setDraftName(e.target.value);
              setSegment(0);
            }}
          >
            <option value="">{drafts.data === undefined ? "Carregando…" : "Escolha um projeto"}</option>
            {(drafts.data ?? []).map((d) => (
              <option key={d.name} value={d.name}>
                {d.name}
                {d.modified_at ? ` (${formatDateTime(d.modified_at)})` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Texto usado como modelo"
          htmlFor="capcut-segment"
          hint={chosen?.preview ? `Primeiro texto: "${chosen.preview}"` : undefined}
        >
          <select
            id="capcut-segment"
            className="select"
            value={segment}
            disabled={!chosen}
            onChange={(e) => setSegment(Number(e.target.value))}
          >
            {Array.from({ length: Math.max(1, chosen?.text_segments ?? 1) }, (_, i) => (
              <option key={i} value={i}>
                {i + 1}º texto
              </option>
            ))}
          </select>
        </Field>
        <Field label="Nome do modelo" htmlFor="capcut-style-name">
          <input
            id="capcut-style-name"
            className="input"
            value={name}
            maxLength={60}
            onChange={(e) => setName(e.target.value)}
            placeholder="Ex.: Amarelo com contorno"
          />
        </Field>
      </div>
      {drafts.data && drafts.data.length === 0 ? (
        <p className="muted small">Nenhum projeto do CapCut com texto foi encontrado.</p>
      ) : null}
    </Modal>
  );
}

/* ---------------- Configurações ---------------- */

function SettingsForm({
  initial,
  models,
  onSaved,
}: {
  initial: CapCutSettings;
  models: Record<string, boolean> | undefined;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<CapCutSettings>(initial);
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof CapCutSettings>(key: K, value: CapCutSettings[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const patch = capcutSettingsPatch(initial, draft);
  const dirty = Object.keys(patch).length > 0;

  async function save() {
    setSaving(true);
    try {
      await api.updateCapcutSettings(patch);
      toast.push("ok", "Configurações das legendas salvas.");
      onSaved();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
        <Field
          label="Pasta de saída"
          htmlFor="cc-output"
          hint={initial.output_dir_is_default ? "Padrão do Media Tool. Vazio volta ao padrão." : "Vazio volta ao padrão."}
        >
          <input
            id="cc-output"
            className="input"
            value={draft.output_dir}
            onChange={(e) => set("output_dir", e.target.value)}
            spellCheck={false}
          />
        </Field>
        <Field label="Resolução" htmlFor="cc-resolution">
          <select
            id="cc-resolution"
            className="select"
            value={draft.resolution}
            onChange={(e) => set("resolution", e.target.value as CapCutResolution)}
          >
            {RESOLUTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Qualidade (taxa de bits)" htmlFor="cc-bitrate">
          <select
            id="cc-bitrate"
            className="select"
            value={draft.bitrate}
            onChange={(e) => set("bitrate", e.target.value as CapCutBitrate)}
          >
            {BITRATES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Quadros por segundo" htmlFor="cc-fps">
          <select
            id="cc-fps"
            className="select"
            value={draft.fps}
            onChange={(e) => set("fps", e.target.value as CapCutFps)}
          >
            {FPS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Palavras por legenda" htmlFor="cc-words">
          <input
            id="cc-words"
            className="input"
            type="number"
            min={1}
            max={10}
            value={draft.max_words}
            onChange={(e) => set("max_words", Number(e.target.value))}
          />
        </Field>
        <Field label="Caracteres por legenda" htmlFor="cc-chars">
          <input
            id="cc-chars"
            className="input"
            type="number"
            min={8}
            max={80}
            value={draft.max_chars}
            onChange={(e) => set("max_chars", Number(e.target.value))}
          />
        </Field>
        <Field label="Modelo de transcrição" htmlFor="cc-model" hint={whisperModelText(models, draft.whisper_model)}>
          <select
            id="cc-model"
            className="select"
            value={draft.whisper_model}
            onChange={(e) => set("whisper_model", e.target.value as CapCutWhisperModel)}
          >
            {WHISPER_MODELS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Tentativas por vídeo" htmlFor="cc-attempts">
          <select
            id="cc-attempts"
            className="select"
            value={draft.max_attempts}
            onChange={(e) => set("max_attempts", Number(e.target.value))}
          >
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Cor do destaque"
          htmlFor="cc-color"
          hint="Formato #RRGGBB. Vazio usa a cor do modelo (ou amarelo)."
        >
          <input
            id="cc-color"
            className="input"
            value={draft.highlight_color ?? ""}
            disabled={!draft.highlight}
            onChange={(e) => set("highlight_color", e.target.value)}
            placeholder="#FFD400"
            spellCheck={false}
          />
        </Field>
      </div>
      <div className="row" style={{ gap: 18, flexWrap: "wrap", marginTop: 12 }}>
        <Switch checked={draft.highlight} onChange={(v) => set("highlight", v)} label="Destacar a palavra falada" />
        <Switch
          checked={draft.keep_original_audio}
          onChange={(v) => set("keep_original_audio", v)}
          label="Manter o áudio original"
        />
        <Switch
          checked={draft.keep_draft}
          onChange={(v) => set("keep_draft", v)}
          label="Manter o projeto no CapCut"
        />
        <Switch
          checked={draft.auto_after_download}
          onChange={(v) => set("auto_after_download", v)}
          label="Legendar automaticamente após o download"
        />
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <button type="submit" className="btn btn-primary" disabled={saving || !dirty}>
          {saving ? <Spinner /> : null}
          Salvar
        </button>
        {dirty ? (
          <button type="button" className="btn btn-ghost" disabled={saving} onClick={() => setDraft(initial)}>
            Descartar
          </button>
        ) : null}
      </div>
    </form>
  );
}

/* ---------------- Fila e histórico ---------------- */

function JobsSection({ onChanged }: { onChanged: () => void }) {
  const toast = useToast();
  const [filter, setFilter] = useState<CapCutJobStatus | "">("");
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number>(() => nextPollMs(null, false));
  const [busyId, setBusyId] = useState<number | null>(null);
  const jobs = useQuery(
    `capcut-jobs:${filter}:${offset}`,
    () =>
      api.listCapcutJobs({ status: filter || null, limit: PAGE_SIZE, offset }).then((page) => {
        setPollMs((p) => capcutPollMs(page.items, p));
        return page;
      }),
    pollMs,
  );

  async function act(job: CapCutJob, action: "cancel" | "retry" | "delete" | "reveal") {
    setBusyId(job.id);
    try {
      if (action === "cancel") await api.cancelCapcutJob(job.id);
      else if (action === "retry") await api.retryCapcutJob(job.id);
      else if (action === "delete") await api.deleteCapcutJob(job.id);
      else await api.revealCapcutJob(job.id);
      if (action === "reveal") toast.push("ok", "Pasta aberta no Explorer.");
      else {
        setPollMs(nextPollMs(null, true));
        jobs.reload();
        onChanged();
      }
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusyId(null);
    }
  }

  const page = jobs.data;
  return (
    <section className="section">
      <div className="section-head">
        <h2>Fila e histórico</h2>
        {jobs.loading && page ? <Spinner /> : null}
        <select
          className="select"
          style={{ marginLeft: "auto", width: "auto" }}
          aria-label="Filtrar por status"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value as CapCutJobStatus | "");
            setOffset(0);
          }}
        >
          {STATUS_FILTERS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {jobs.error ? <ErrorLine message={jobs.error} onRetry={jobs.reload} /> : null}
      {page === undefined ? (
        jobs.error ? null : (
          <SkeletonRows rows={3} />
        )
      ) : page.total === 0 ? (
        <Empty title="Nenhum vídeo na fila de legendas">
          <span className="muted small">Na Biblioteca, selecione vídeos e use "Legendar no CapCut".</span>
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Vídeo</th>
                  <th>Status</th>
                  <th>Etapa</th>
                  <th>Progresso</th>
                  <th>Tentativas</th>
                  <th>Observação</th>
                  <th>Saída</th>
                  <th aria-label="Ações" />
                </tr>
              </thead>
              <tbody>
                {page.items.map((job) => (
                  <JobRow key={job.id} job={job} busy={busyId === job.id} onAction={(a) => void act(job, a)} />
                ))}
              </tbody>
            </table>
          </div>
          {page.total > PAGE_SIZE ? (
            <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
              <span className="muted small">
                {formatCount(offset + 1)}–{formatCount(Math.min(offset + PAGE_SIZE, page.total))} de{" "}
                {formatCount(page.total)}
              </span>
              <button
                type="button"
                className="btn btn-sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Anterior
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={offset + PAGE_SIZE >= page.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Próxima
              </button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

function JobRow({
  job,
  busy,
  onAction,
}: {
  job: CapCutJob;
  busy: boolean;
  onAction: (action: "cancel" | "retry" | "delete" | "reveal") => void;
}) {
  const active = isCapcutActive(job);
  const note = jobNote(job);
  return (
    <tr>
      <td>
        <span className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
          <Thumb src={job.thumbnail_url} mediaType={job.media_type ?? "video"} size={36} />
          <span style={{ minWidth: 0 }}>
            <span className="small">{job.media_filename ?? "—"}</span>
            <br />
            <span className="muted small">
              {job.style_name ?? "—"} · {formatDateTime(job.created_at)}
              {job.origin === "auto" ? " · automático" : ""}
            </span>
          </span>
        </span>
      </td>
      <td>
        <Pill tone={capcutTone(job.status)}>{CAPCUT_STATUS_LABEL[job.status]}</Pill>
      </td>
      <td className="muted small">{job.status === "running" ? stageLabel(job.stage) : "—"}</td>
      <td style={{ minWidth: 120 }}>
        <ProgressBar value={jobProgress(job)} />
      </td>
      <td className="muted nowrap">
        {formatCount(job.attempts)} de {formatCount(job.max_attempts)}
      </td>
      <td className="muted small">{note || "—"}</td>
      <td className="nowrap">
        {job.status === "completed" && job.output_exists ? (
          <>
            <a className="btn btn-sm btn-ghost" href={capcutJobFileUrl(job.id)} download={job.output_name ?? undefined}>
              Baixar
            </a>
            <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("reveal")}>
              Mostrar na pasta
            </button>
          </>
        ) : job.status === "completed" ? (
          <span className="muted small">Arquivo removido</span>
        ) : (
          <span className="muted small">—</span>
        )}
      </td>
      <td className="nowrap" style={{ textAlign: "right" }}>
        {active ? (
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            disabled={busy || job.cancel_requested}
            onClick={() => onAction("cancel")}
          >
            Cancelar
          </button>
        ) : null}
        {job.status === "failed" || job.status === "cancelled" ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("retry")}>
            Tentar de novo
          </button>
        ) : null}
        {!active ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("delete")}>
            Remover do histórico
          </button>
        ) : null}
      </td>
    </tr>
  );
}

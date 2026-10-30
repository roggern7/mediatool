import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import { type ExtensionDevice, type ExtensionPairingCode, extensionApi, formatPairingCode } from "../api/extension";
import type { CheckStatus, InstagramProvider, Diagnostics, InstagramStatus, Preset, SessionInput, Settings, ToolDiag } from "../api/types";
import { ConfirmDialog, ErrorLine, Field, Pill, SkeletonRows, Spinner } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { diskShortage, diskText } from "../lib/disk";
import { describeSessionTest, formatTestDuration } from "../lib/sessionTest";
import { SessionCookiesForm } from "../components/SessionCookiesForm";
import { PauseBanner } from "../components/PauseBanner";
import { isPaused, useNow } from "../lib/pause";
import { STEP_STATE_LABEL, sessionSteps } from "../lib/sessionSteps";
import type { Tone } from "../lib/labels";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

const CHECK_TONE: Record<CheckStatus, Tone> = { ok: "ok", warn: "warn", error: "err" };
const CHECK_LABEL: Record<CheckStatus, string> = { ok: "OK", warn: "Atenção", error: "Erro" };
const STORAGE_LABEL: Record<string, string> = {
  root: "Pasta raiz do storage",
  originals: "Originais",
  processed: "Processados",
  temp: "Temporários",
  thumbs: "Miniaturas",
};

export function SettingsPage() {
  const settings = useQuery("settings", () => api.getSettings());
  const presets = useQuery("presets", () => api.listPresets());
  const [igOverride, setIgOverride] = useState<InstagramStatus | null>(null);
  const ig = igOverride ?? settings.data?.instagram ?? null;
  const [diagTick, setDiagTick] = useState(0);
  const refreshDiag = () => setDiagTick((t) => t + 1);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Configurações</h1>
      </div>

      <section className="section">
        <div className="section-head">
          <h2>Instagram</h2>
        </div>
        {ig ? (
          <InstagramSection
            status={ig}
            onStatus={(st) => {
              setIgOverride(st);
              refreshDiag();
            }}
          />
        ) : settings.error ? (
          <ErrorLine message={settings.error} onRetry={settings.reload} />
        ) : (
          <SkeletonRows rows={3} />
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2>Processamento</h2>
        </div>
        {settings.data ? (
          <PreferencesForm initial={settings.data} presets={presets.data ?? []} onSaved={refreshDiag} />
        ) : settings.error ? (
          <ErrorLine message={settings.error} onRetry={settings.reload} />
        ) : (
          <SkeletonRows rows={3} />
        )}
      </section>

      <ExtensionSection />

      <DiagnosticsSection tick={diagTick} onRefresh={refreshDiag} />
    </div>
  );
}

/* ---------------- Extensão do Chrome ---------------- */

function ExtensionSection() {
  const toast = useToast();
  const devices = useQuery("extension-devices", () => extensionApi.listDevices());
  const [list, setList] = useState<ExtensionDevice[] | null>(null);
  const [pairing, setPairing] = useState<ExtensionPairingCode | null>(null);
  const [busy, setBusy] = useState(false);
  const [revoking, setRevoking] = useState<ExtensionDevice | null>(null);
  // O código mostrado foi usado pela extensão (a tela troca o código por "Extensão conectada").
  const [connected, setConnected] = useState(false);
  const now = useNow(1_000);
  const items = list ?? devices.data ?? null;
  const left = pairing ? Math.max(0, Math.ceil((Date.parse(pairing.expires_at) - now) / 1000)) : 0;
  const pairingId = pairing && left > 0 ? pairing.id : null;

  // Enquanto o código está visível: consulta leve a cada 2 s se ele já foi usado (ou invalidado).
  useEffect(() => {
    if (!pairingId) return undefined;
    let stopped = false;
    const timer = window.setInterval(() => {
      extensionApi
        .pairingCodeStatus(pairingId)
        .then(async (result) => {
          if (stopped || result.status === "pending") return;
          stopped = true;
          window.clearInterval(timer);
          if (result.status === "used") {
            setConnected(true);
            setPairing(null);
            setList(await extensionApi.listDevices());
          } else {
            // Substituído, invalidado pelo bloqueio ou expirado no servidor: mostra "expirou".
            const expired = new Date(0).toISOString();
            setPairing((current) =>
              current && current.id === pairingId ? { ...current, expires_at: expired } : current,
            );
          }
        })
        .catch(() => undefined); // falha momentânea: tenta de novo no próximo ciclo
    }, 2_000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [pairingId]);

  const generate = async () => {
    setBusy(true);
    try {
      setConnected(false);
      setPairing(await extensionApi.createPairingCode());
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  };

  const refresh = async () => {
    try {
      setList(await extensionApi.listDevices());
    } catch (err) {
      toast.push("err", messageOf(err));
    }
  };

  const revoke = async (device: ExtensionDevice) => {
    setBusy(true);
    try {
      setList(await extensionApi.revokeDevice(device.id));
      toast.push("ok", `Acesso de "${device.name}" revogado.`);
      setRevoking(null);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="section">
      <div className="section-head">
        <h2>Extensão do Chrome</h2>
        <button type="button" className="btn btn-sm" disabled={devices.loading} onClick={() => void refresh()}>
          <RefreshCw size={13} strokeWidth={1.75} aria-hidden="true" />
          Atualizar
        </button>
      </div>
      <p className="muted" style={{ marginTop: 0 }}>
        A extensão inicia importações a partir da aba do Instagram aberta no Chrome. Para conectar, gere um código e
        digite-o nas opções da extensão. O código vale por 5 minutos e só pode ser usado uma vez.
      </p>
      <div className="row" style={{ gap: 12, marginBottom: 14 }}>
        <button type="button" className="btn" disabled={busy} onClick={() => void generate()}>
          {busy && !revoking ? <Spinner /> : null}
          Gerar código
        </button>
        {pairing && left > 0 ? (
          <span className="row" style={{ gap: 10 }}>
            <span className="mono" style={{ fontSize: 20, letterSpacing: 2 }} aria-label="Código de pareamento">
              {formatPairingCode(pairing.code)}
            </span>
            <span className="muted">
              expira em {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}
            </span>
          </span>
        ) : pairing ? (
          <span className="muted">O código expirou. Gere outro.</span>
        ) : connected ? (
          <span role="status">Extensão conectada.</span>
        ) : null}
      </div>

      {devices.error && !items ? <ErrorLine message={devices.error} onRetry={devices.reload} /> : null}
      {!items ? (
        devices.error ? null : <SkeletonRows rows={2} />
      ) : items.length === 0 ? (
        <p className="muted">Nenhuma extensão conectada.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Extensão conectada</th>
                <th>Conectada em</th>
                <th>Último uso</th>
                <th aria-label="Ações" />
              </tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr key={d.id}>
                  <td>{d.name}</td>
                  <td className="nowrap muted">{formatDateTime(d.created_at)}</td>
                  <td className="nowrap muted">{d.last_used_at ? formatDateTime(d.last_used_at) : "Nunca"}</td>
                  <td className="nowrap" style={{ textAlign: "right" }}>
                    <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setRevoking(d)}>
                      Revogar
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {revoking ? (
        <ConfirmDialog
          title="Revogar extensão"
          confirmLabel="Revogar"
          danger
          busy={busy}
          onConfirm={() => void revoke(revoking)}
          onCancel={() => setRevoking(null)}
        >
          A extensão &quot;{revoking.name}&quot; perde o acesso na hora. Para usá-la de novo será preciso parear com um
          código novo.
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

/* ---------------- Instagram ---------------- */

function InstagramSection({
  status,
  onStatus,
}: {
  status: InstagramStatus;
  onStatus: (s: InstagramStatus) => void;
}) {
  const toast = useToast();
  const [formOpen, setFormOpen] = useState(false);
  const [cookiesOpen, setCookiesOpen] = useState(false);
  const [suggestTest, setSuggestTest] = useState(false);
  const cookiesEditable = status.session_configured && status.source === "file";
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState<"test" | "remove" | null>(null);

  const testing = useRef(false);

  async function test() {
    if (testing.current) return; // impede clique duplo
    testing.current = true;
    setSuggestTest(false);
    setBusy("test");
    try {
      const s = await api.testSession();
      onStatus(s);
      if (s.last_test) {
        const view = describeSessionTest(s.last_test);
        toast.push(view.tone === "ok" ? "ok" : view.tone === "warn" ? "info" : "err", `Teste da sessão: ${view.label}.`);
      }
    } catch (err) {
      const inProgress = err instanceof ApiError && err.code === "session_test_in_progress";
      toast.push("err", inProgress ? "Já existe um teste de sessão em andamento." : messageOf(err));
      // O backend pode ter gravado o resultado mesmo assim: busca o último resultado salvo.
      try {
        const fresh = await api.getSettings();
        onStatus(fresh.instagram);
      } catch {
        // mantém o estado atual; o erro já foi mostrado
      }
    } finally {
      testing.current = false;
      setBusy(null);
    }
  }

  async function remove() {
    setBusy("remove");
    try {
      const s = await api.clearSession();
      onStatus(s);
      setConfirmRemove(false);
      toast.push("ok", s.session_configured ? "Arquivo de sessão removido." : "Sessão removida.");
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(null);
    }
  }

  const last = status.last_test;
  const now = useNow();
  const paused = isPaused(status.paused_until, now);

  async function refreshStatus() {
    try {
      const fresh = await api.getSettings();
      onStatus(fresh.instagram);
    } catch (err) {
      toast.push("err", messageOf(err));
    }
  }

  return (
    <>
      {paused ? <PauseBanner status={status} now={now} onReleased={() => void refreshStatus()} /> : null}
      <dl className="kv">
        <dt>Sessão</dt>
        <dd>
          {status.session_configured ? (
            <span className="row" style={{ gap: 10 }}>
              <Pill tone="ok">Configurada</Pill>
              <span className="dim" aria-label="Valor oculto">
                ••••••••••••
              </span>
            </span>
          ) : (
            <Pill tone="warn">Não configurada</Pill>
          )}
        </dd>
        {status.source ? (
          <>
            <dt>Fonte</dt>
            <dd>{status.source === "file" ? "Arquivo protegido" : "Variável de ambiente"}</dd>
          </>
        ) : null}
        {status.updated_at ? (
          <>
            <dt>Atualizado em</dt>
            <dd>{formatDateTime(status.updated_at)}</dd>
          </>
        ) : null}
        <dt>Último teste</dt>
        <dd>
          {last ? <LastTestView test={last} /> : (
            <span className="muted">Nunca testada</span>
          )}
        </dd>
      </dl>

      {!formOpen && !cookiesOpen ? (
        <div className="row" style={{ marginTop: 12 }}>
          <button type="button" className="btn btn-primary" onClick={() => setFormOpen(true)}>
            {status.session_configured ? "Atualizar sessão" : "Configurar sessão"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={!status.session_configured || busy !== null}
            aria-busy={busy === "test"}
            onClick={() => void test()}
          >
            {busy === "test" ? (
              <>
                <Spinner />
                <TestingLabel />
              </>
            ) : (
              "Testar sessão"
            )}
          </button>
          {status.session_configured ? (
            <button
              type="button"
              className="btn btn-ghost btn-danger"
              disabled={busy !== null}
              onClick={() => setConfirmRemove(true)}
            >
              Remover sessão
            </button>
          ) : null}
          {cookiesEditable ? (
            <button type="button" className="btn btn-ghost" disabled={busy !== null} onClick={() => setCookiesOpen(true)}>
              Cookies adicionais
            </button>
          ) : null}
        </div>
      ) : formOpen ? (
        <SessionForm
          onCancel={() => setFormOpen(false)}
          onSaved={(s) => {
            onStatus(s);
            setFormOpen(false);
            toast.push("ok", "Sessão configurada.");
          }}
        />
      ) : (
        <SessionCookiesForm
          savedKeys={status.extra_keys ?? []}
          onCancel={() => setCookiesOpen(false)}
          onSaved={(s) => {
            onStatus(s);
            setCookiesOpen(false);
            setSuggestTest(true);
            toast.push("ok", "Cookies salvos.");
          }}
        />
      )}

      {suggestTest && !cookiesOpen && !formOpen ? (
        <div className="alert" style={{ marginTop: 12, maxWidth: 620 }}>
          <span className="grow">Cookies salvos. Teste a sessão para confirmar que continuam válidos.</span>
          <button type="button" className="btn btn-sm" disabled={busy !== null} onClick={() => void test()}>
            Testar sessão
          </button>
        </div>
      ) : null}

      {confirmRemove ? (
        <ConfirmDialog
          title="Remover sessão"
          confirmLabel="Remover"
          danger
          busy={busy === "remove"}
          onConfirm={() => void remove()}
          onCancel={() => setConfirmRemove(false)}
        >
          A sessão salva neste computador será apagada. Importações do Instagram ficam indisponíveis até configurar
          uma nova sessão.
          {status.source === "env"
            ? " A variável de ambiente INSTAGRAM_SESSIONID, se definida, continua valendo até ser removida do sistema."
            : ""}
        </ConfirmDialog>
      ) : null}
    </>
  );
}

function LastTestView({ test }: { test: NonNullable<InstagramStatus["last_test"]> }) {
  const view = describeSessionTest(test);
  const steps = sessionSteps(test);
  const details = [
    test.http_status ? `HTTP ${test.http_status}` : null,
    formatTestDuration(test.duration_ms),
    formatDateTime(test.tested_at),
  ].filter(Boolean);
  return (
    <span>
      <Pill tone={view.tone}>{view.label}</Pill>
      <span className="muted"> · {details.join(" · ")}</span>
      {steps.length === 0 ? (
        <div className="small muted">
          {view.session} · {view.listing}
        </div>
      ) : null}
      {view.hint ? <div className="small muted">{view.hint}</div> : null}
      {view.tone !== "ok" && test.message ? <div className={`small tone-${view.tone}`}>{test.message}</div> : null}
      {steps.length > 0 ? (
        <ul className="small" style={{ listStyle: "none", padding: 0, margin: "6px 0 0" }}>
          {steps.map((s) => (
            <li key={s.label}>
              <span
                className={
                  s.state === "ok"
                    ? "tone-ok"
                    : s.state === "failed"
                      ? "tone-err"
                      : s.state === "other_account" || s.state === "unconfirmed"
                        ? "tone-warn"
                        : "dim"
                }
              >
                {STEP_STATE_LABEL[s.state]}
              </span>{" "}
              · {s.label}
            </li>
          ))}
        </ul>
      ) : null}
    </span>
  );
}

/** Rótulo do botão durante o teste, com o tempo decorrido real. */
function TestingLabel() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 500);
    return () => window.clearInterval(timer);
  }, []);
  return <span>Testando… (até 40 s) · {seconds} s</span>;
}

const EXTRA_KEYS = ["csrftoken", "ds_user_id", "mid", "ig_did"] as const;
type ExtraKey = (typeof EXTRA_KEYS)[number];
const EXTRA_RE = /^[A-Za-z0-9%:_\-.]+$/;
const EMPTY_EXTRAS: Record<ExtraKey, string> = { csrftoken: "", ds_user_id: "", mid: "", ig_did: "" };

function SessionForm({ onCancel, onSaved }: { onCancel: () => void; onSaved: (s: InstagramStatus) => void }) {
  const toast = useToast();
  const [sessionid, setSessionid] = useState("");
  const [extras, setExtras] = useState<Record<ExtraKey, string>>(EMPTY_EXTRAS);
  const [saving, setSaving] = useState(false);
  const [touched, setTouched] = useState(false);

  const sidTrim = sessionid.trim();
  const sidError = !sidTrim
    ? "Informe o sessionid."
    : sidTrim.length > 512 || !EXTRA_RE.test(sidTrim)
      ? "Valor com formato inválido."
      : null;
  const extraErrors = EXTRA_KEYS.filter((k) => {
    const v = extras[k].trim();
    return v !== "" && (v.length > 512 || !EXTRA_RE.test(v));
  });

  function clear() {
    setSessionid("");
    setExtras(EMPTY_EXTRAS);
  }

  async function save() {
    setTouched(true);
    if (sidError || extraErrors.length > 0) return;
    const body: SessionInput = { sessionid: sidTrim };
    for (const k of EXTRA_KEYS) {
      const v = extras[k].trim();
      if (v) body[k] = v;
    }
    setSaving(true);
    try {
      const s = await api.saveSession(body);
      clear();
      onSaved(s);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      className="panel"
      style={{ marginTop: 12, maxWidth: 560 }}
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <Field label="sessionid" htmlFor="ig-sessionid" error={touched ? sidError : null}>
        <input
          id="ig-sessionid"
          name="ig-sessionid"
          type="password"
          className={`input${touched && sidError ? " invalid" : ""}`}
          value={sessionid}
          onChange={(e) => setSessionid(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          autoFocus
        />
      </Field>

      <details className="more" style={{ marginTop: 12 }}>
        <summary>Cookies adicionais (opcional)</summary>
        <div className="form-grid" style={{ marginTop: 10 }}>
          {EXTRA_KEYS.map((k) => (
            <Field
              key={k}
              label={k}
              htmlFor={`ig-${k}`}
              error={touched && extraErrors.includes(k) ? "Caracteres inválidos." : null}
            >
              <input
                id={`ig-${k}`}
                name={`ig-${k}`}
                type="password"
                className="input"
                value={extras[k]}
                onChange={(e) => setExtras({ ...extras, [k]: e.target.value })}
                autoComplete="off"
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
              />
            </Field>
          ))}
        </div>
      </details>

      <div className="row" style={{ marginTop: 14 }}>
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? <Spinner /> : null}
          Salvar sessão
        </button>
        <button
          type="button"
          className="btn"
          disabled={saving}
          onClick={() => {
            clear();
            onCancel();
          }}
        >
          Cancelar
        </button>
      </div>
    </form>
  );
}

/* ---------------- Preferências ---------------- */

type PathField = "ffmpeg_path" | "ffprobe_path";

/** Separa "ffmpeg_path: arquivo não encontrado." em campo e mensagem legível. */
function pathError(err: unknown): { field: PathField; message: string } | null {
  if (!(err instanceof ApiError) || err.code !== "invalid_path" || !err.detail) return null;
  const match = /^(ffmpeg_path|ffprobe_path):\s*(.+)$/.exec(err.detail);
  if (!match || !match[1] || !match[2]) return null;
  const text = match[2].trim();
  return { field: match[1] as PathField, message: text.charAt(0).toUpperCase() + text.slice(1) };
}

function PreferencesForm({
  initial,
  presets,
  onSaved,
}: {
  initial: Settings;
  presets: Preset[];
  onSaved: () => void;
}) {
  const toast = useToast();
  const [ffmpeg, setFfmpeg] = useState(initial.ffmpeg_path ?? "");
  const [ffprobe, setFfprobe] = useState(initial.ffprobe_path ?? "");
  const [concurrency, setConcurrency] = useState(initial.concurrency);
  // Dois pools (downloads e processamentos) quando o backend manda download_concurrency.
  const twoPools = initial.download_concurrency !== undefined;
  const [downloads, setDownloads] = useState(initial.download_concurrency ?? 2);
  const [processings, setProcessings] = useState(
    Math.min(2, Math.max(1, initial.processing_concurrency ?? initial.concurrency)),
  );
  const [defaultPreset, setDefaultPreset] = useState<number | null>(initial.default_preset_id);
  const providerSupported = initial.instagram_provider !== undefined;
  const [provider, setProvider] = useState<InstagramProvider>(initial.instagram_provider ?? "auto");
  const [effective, setEffective] = useState(initial.effective);
  const [saving, setSaving] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<PathField, string>>>({});

  async function save() {
    setSaving(true);
    setFieldErrors({});
    try {
      const s = await api.updateSettings({
        ffmpeg_path: ffmpeg.trim() || null,
        ffprobe_path: ffprobe.trim() || null,
        ...(twoPools
          ? {
              download_concurrency: downloads,
              ...(initial.processing_concurrency !== undefined
                ? { processing_concurrency: processings }
                : { concurrency: processings }),
            }
          : { concurrency }),
        default_preset_id: defaultPreset,
        ...(providerSupported ? { instagram_provider: provider } : {}),
      });
      setEffective(s.effective);
      toast.push("ok", "Configurações salvas.");
      onSaved();
    } catch (err) {
      const pe = pathError(err);
      if (pe) setFieldErrors({ [pe.field]: pe.message });
      else toast.push("err", messageOf(err));
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
      <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
        <Field
          label="Caminho do FFmpeg"
          htmlFor="set-ffmpeg"
          error={fieldErrors.ffmpeg_path}
          hint={effective.ffmpeg_path ? `Em uso: ${effective.ffmpeg_path}` : "Vazio usa o PATH do sistema"}
        >
          <input
            id="set-ffmpeg"
            className={`input${fieldErrors.ffmpeg_path ? " invalid" : ""}`}
            value={ffmpeg}
            onChange={(e) => {
              setFfmpeg(e.target.value);
              setFieldErrors((f) => ({ ...f, ffmpeg_path: undefined }));
            }}
            placeholder="ffmpeg.exe"
            spellCheck={false}
          />
        </Field>
        <Field
          label="Caminho do ffprobe"
          htmlFor="set-ffprobe"
          error={fieldErrors.ffprobe_path}
          hint={effective.ffprobe_path ? `Em uso: ${effective.ffprobe_path}` : "Vazio usa o PATH do sistema"}
        >
          <input
            id="set-ffprobe"
            className={`input${fieldErrors.ffprobe_path ? " invalid" : ""}`}
            value={ffprobe}
            onChange={(e) => {
              setFfprobe(e.target.value);
              setFieldErrors((f) => ({ ...f, ffprobe_path: undefined }));
            }}
            placeholder="ffprobe.exe"
            spellCheck={false}
          />
        </Field>
        {twoPools ? (
          <>
            <Field label="Downloads simultâneos" htmlFor="set-downloads" hint="Arquivos baixados ao mesmo tempo">
              <select
                id="set-downloads"
                className="select"
                value={downloads}
                onChange={(e) => setDownloads(Number(e.target.value))}
              >
                {[1, 2, 3, 4].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Processamentos simultâneos"
              htmlFor="set-processings"
              hint="Vídeos ajustados ao mesmo tempo (usa mais o computador)"
            >
              <select
                id="set-processings"
                className="select"
                value={processings}
                onChange={(e) => setProcessings(Number(e.target.value))}
              >
                {[1, 2].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </Field>
          </>
        ) : (
          <Field label="Concorrência" htmlFor="set-conc" hint="Jobs processados ao mesmo tempo">
            <select
              id="set-conc"
              className="select"
              value={concurrency}
              onChange={(e) => setConcurrency(Number(e.target.value))}
            >
              {[1, 2, 3, 4].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
        )}
        {providerSupported ? (
          <Field label="Método de importação" htmlFor="set-provider">
            <select
              id="set-provider"
              className="select"
              value={provider}
              onChange={(e) => setProvider(e.target.value as InstagramProvider)}
            >
              <option value="web">Instagram Web (recomendado)</option>
              <option value="auto">Automático (Instagram Web com alternativa)</option>
              <option value="gallery_dl">gallery-dl (legado)</option>
            </select>
          </Field>
        ) : null}
        <Field
          label="Preset padrão"
          htmlFor="set-preset"
          hint="Usado só quando você escolhe processar; o download não aplica preset"
        >
          <select
            id="set-preset"
            className="select"
            value={defaultPreset ?? ""}
            onChange={(e) => setDefaultPreset(e.target.value === "" ? null : Number(e.target.value))}
          >
            <option value="">Nenhum (usar Anti-duplicidade)</option>
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {initial.disk ? (
        <div
          className={diskShortage(initial.disk) ? "alert warn" : "muted small"}
          role={diskShortage(initial.disk) ? "alert" : undefined}
          style={{ marginTop: 14 }}
        >
          {diskText(initial.disk)}.
          {diskShortage(initial.disk)
            ? " Espaço insuficiente: os downloads ficam parados até você liberar espaço no disco."
            : null}
        </div>
      ) : null}
      <div className="row" style={{ marginTop: 14 }}>
        <button type="submit" className="btn btn-primary" disabled={saving}>
          {saving ? <Spinner /> : null}
          Salvar
        </button>
      </div>
    </form>
  );
}

/* ---------------- Diagnóstico ---------------- */

function ToolRow({ label, tool }: { label: string; tool: ToolDiag }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>
        <Pill tone={tool.found ? "ok" : "err"}>{tool.found ? "Encontrado" : "Não encontrado"}</Pill>
        {tool.version ? <span className="muted"> · {tool.version}</span> : null}
        {tool.path ? <div className="mono dim">{tool.path}</div> : null}
      </dd>
    </>
  );
}

function DiagnosticsSection({ tick, onRefresh }: { tick: number; onRefresh: () => void }) {
  const diag = useQuery(`diagnostics:${tick}`, () => api.diagnostics());
  const d: Diagnostics | undefined = diag.data;

  return (
    <section className="section">
      <div className="section-head">
        <div className="row">
          <h2>Sistema e diagnóstico</h2>
          {d ? <Pill tone={CHECK_TONE[d.overall]}>{CHECK_LABEL[d.overall]}</Pill> : null}
        </div>
        <button type="button" className="btn btn-sm" disabled={diag.loading} onClick={onRefresh}>
          {diag.loading ? <Spinner /> : <RefreshCw size={13} strokeWidth={1.75} aria-hidden="true" />}
          Atualizar diagnóstico
        </button>
      </div>

      {diag.error ? <ErrorLine message={diag.error} /> : null}
      {!d ? (
        diag.error ? null : <SkeletonRows rows={5} />
      ) : (
        <>
          {d.checks.length > 0 ? (
            <div className="table-wrap" style={{ marginBottom: 16 }}>
              <table className="table">
                <tbody>
                  {d.checks.map((c) => (
                    <tr key={c.id}>
                      <td className="nowrap" style={{ width: 110 }}>
                        <Pill tone={CHECK_TONE[c.status]}>{CHECK_LABEL[c.status]}</Pill>
                      </td>
                      <td className="nowrap">{c.label}</td>
                      <td className="muted">{c.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          <dl className="kv">
            <ToolRow label="FFmpeg" tool={d.ffmpeg} />
            <ToolRow label="ffprobe" tool={d.ffprobe} />
            <ToolRow label="gallery-dl" tool={d.gallery_dl} />
            {Object.entries(d.storage).map(([name, s]) => (
              <StorageRow key={name} name={name} path={s.path} exists={s.exists} writable={s.writable} />
            ))}
            <dt>Banco</dt>
            <dd>
              <span>Schema {d.sqlite.schema_version ?? "-"}</span>
              <span className="muted"> · SQLite {d.sqlite.version}</span>
              <div className="mono dim">{d.sqlite.path}</div>
            </dd>
            <dt>Worker</dt>
            <dd>
              <Pill tone={d.worker.running ? "ok" : "err"}>{d.worker.running ? "Rodando" : "Parado"}</Pill>
              <span className="muted">
                {" "}
                · concorrência {d.worker.concurrency} · {d.worker.active_jobs} ativos
              </span>
            </dd>
            <dt>Backend</dt>
            <dd>
              <span className="mono">
                {d.backend.host}:{d.backend.port}
              </span>
              <span className="muted"> · versão {d.backend.version}</span>
            </dd>
            <dt>Python</dt>
            <dd>
              {d.python.version}
              <div className="mono dim">{d.python.executable}</div>
            </dd>
            <dt>Gerado em</dt>
            <dd className="muted">{formatDateTime(d.generated_at)}</dd>
          </dl>
        </>
      )}
    </section>
  );
}

function StorageRow({ name, path, exists, writable }: { name: string; path: string; exists: boolean; writable: boolean }) {
  const tone: Tone = !exists ? "err" : writable ? "ok" : "warn";
  const label = !exists ? "Não existe" : writable ? "Gravável" : "Somente leitura";
  return (
    <>
      <dt>{STORAGE_LABEL[name] ?? name}</dt>
      <dd>
        <Pill tone={tone}>{label}</Pill>
        <div className="mono dim">{path}</div>
      </dd>
    </>
  );
}

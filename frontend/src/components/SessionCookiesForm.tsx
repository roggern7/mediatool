import { useState } from "react";
import { api, messageOf } from "../api/client";
import type { InstagramStatus } from "../api/types";
import { COOKIE_KEYS, buildCookiePatch, invalidCookieKeys, savedKeysLabel, type CookieKey } from "../lib/sessionCookies";
import { useToast } from "../lib/toast";
import { Field, Spinner } from "./ui";

const EMPTY: Record<CookieKey, string> = { csrftoken: "", ds_user_id: "", mid: "", ig_did: "" };

export function SessionCookiesForm({
  savedKeys,
  onCancel,
  onSaved,
}: {
  savedKeys: readonly string[];
  onCancel: () => void;
  onSaved: (s: InstagramStatus) => void;
}) {
  const toast = useToast();
  const [values, setValues] = useState<Record<CookieKey, string>>(EMPTY);
  const [remove, setRemove] = useState<Set<CookieKey>>(() => new Set());
  const [saving, setSaving] = useState(false);
  const [touched, setTouched] = useState(false);

  const invalid = invalidCookieKeys(values);
  const body = buildCookiePatch(values, remove);
  const nothing = Object.keys(body).length === 0;

  function clear() {
    setValues(EMPTY);
    setRemove(new Set());
  }

  function toggleRemove(k: CookieKey) {
    setRemove((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }

  async function save() {
    setTouched(true);
    if (invalid.length > 0 || nothing) return;
    setSaving(true);
    try {
      const s = await api.patchSessionCookies(body);
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
      style={{ marginTop: 12, maxWidth: 620 }}
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="row between">
        <strong style={{ fontWeight: 500 }}>Cookies adicionais</strong>
        <span className="muted small">{savedKeysLabel(savedKeys)}</span>
      </div>
      <p className="field-hint" style={{ margin: "6px 0 12px" }}>
        Copie do mesmo navegador em que o sessionid foi obtido: DevTools &gt; Application &gt; Cookies &gt;
        https://www.instagram.com.
      </p>
      <div className="form-grid">
        {COOKIE_KEYS.map((k) => {
          const saved = savedKeys.includes(k);
          const removing = remove.has(k);
          return (
            <Field
              key={k}
              label={saved ? `${k} (salvo)` : k}
              htmlFor={`ck-${k}`}
              error={touched && invalid.includes(k) ? "Caracteres inválidos." : null}
              hint={removing && !values[k].trim() ? "Será removido ao salvar." : undefined}
            >
              <div className="row" style={{ flexWrap: "nowrap" }}>
                <input
                  id={`ck-${k}`}
                  name={`ck-${k}`}
                  type="password"
                  className="input grow"
                  value={values[k]}
                  placeholder={saved ? "Novo valor" : undefined}
                  onChange={(e) => setValues({ ...values, [k]: e.target.value })}
                  autoComplete="new-password"
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                />
                {saved ? (
                  <button
                    type="button"
                    className={`btn btn-sm btn-danger${removing ? "" : " btn-ghost"}`}
                    aria-pressed={removing}
                    onClick={() => toggleRemove(k)}
                  >
                    {removing ? "Desfazer" : "Remover"}
                  </button>
                ) : null}
              </div>
            </Field>
          );
        })}
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <button type="submit" className="btn btn-primary" disabled={saving || nothing}>
          {saving ? <Spinner /> : null}
          Salvar cookies
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

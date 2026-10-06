import { describe, expect, it } from "vitest";
import {
  actionRequiredText,
  formatPauseTime,
  isPaused,
  nextCheckText,
  pauseReasonLabel,
  pauseScopeText,
  pauseText,
} from "./pause";

const now = new Date(2026, 9, 3, 10, 0, 0).getTime();
const later = new Date(2026, 9, 3, 14, 30, 0).toISOString();
const tomorrow = new Date(2026, 9, 4, 8, 5, 0).toISOString();

describe("pausa do Instagram", () => {
  it("isPaused só no futuro", () => {
    expect(isPaused(later, now)).toBe(true);
    expect(isPaused(new Date(now - 1000).toISOString(), now)).toBe(false);
    expect(isPaused(null, now)).toBe(false);
    expect(isPaused("lixo", now)).toBe(false);
  });
  it("horário local", () => {
    expect(formatPauseTime(later, now)).toBe("14:30");
    expect(formatPauseTime(tomorrow, now)).toBe("04/10 08:05");
  });
  it("motivo legível", () => {
    expect(pauseReasonLabel("rate_limited")).toBe("limite de requisições");
    expect(pauseReasonLabel("action_blocked")).toBe("proteção do Media Tool após aviso de restrição");
    expect(pauseReasonLabel("checkpoint_required")).toBe(
      "o Instagram pediu uma verificação; conclua no navegador e depois libere a pausa",
    );
    expect(pauseReasonLabel("session_invalid")).toBe("Sessão precisa ser verificada");
    expect(pauseReasonLabel("auth_check")).toBe("Verificando acesso ao Instagram");
    expect(pauseReasonLabel(null)).toBeNull();
  });
  it("texto da faixa", () => {
    expect(pauseText(later, "rate_limited", now)).toBe("Instagram em pausa até 14:30 (limite de requisições)");
    expect(pauseText(later, null, now)).toBe("Instagram em pausa até 14:30");
    expect(pauseText(tomorrow, "session_invalid", now)).toBe(
      "Instagram em pausa até 04/10 08:05 (Sessão precisa ser verificada)",
    );
  });
  it("escopo, próxima verificação e ação necessária", () => {
    expect(pauseScopeText("session")).toBe(
      "Downloads e processamento continuam; novas consultas ao Instagram aguardam",
    );
    expect(pauseScopeText("instagram")).toBe("Processamento continua; downloads e consultas ao Instagram aguardam");
    expect(pauseScopeText(null)).toBeNull();
    expect(nextCheckText(later, now)).toBe("Nova verificação automática às 14:30");
    expect(nextCheckText(new Date(now - 1000).toISOString(), now)).toBe("Verificação automática em andamento");
    expect(nextCheckText(null, now)).toBeNull();
    expect(actionRequiredText(true, "checkpoint_required")).toBe(
      "Ação necessária: conclua a verificação no Instagram pelo navegador e depois libere a pausa",
    );
    expect(actionRequiredText(true, "session_invalid")).toBe(
      "Ação necessária: teste a sessão ou salve a sessão de novo em Configurações",
    );
    expect(actionRequiredText(false, "session_invalid")).toBeNull();
  });
});

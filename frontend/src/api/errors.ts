// Mensagens em português para os códigos de erro conhecidos do contrato.

const MESSAGES: Record<string, string> = {
  session_missing: "Sessão do Instagram não configurada.",
  session_not_editable:
    "A sessão atual vem da variável de ambiente. Configure a sessão pelo painel para salvar cookies adicionais.",
  session_invalid: "A sessão precisa ser verificada. Teste a sessão ou salve a sessão de novo em Configurações.",
  request_auth_failed:
    "O Instagram recusou um acesso desta vez. O Media Tool verifica de novo sozinho em alguns minutos; a sessão salva continua guardada.",
  session_expired: "A sessão do Instagram expirou. Atualize a sessão em Configurações.",
  checkpoint_required:
    "O Instagram pediu uma verificação de segurança. Abra o Instagram no navegador, conclua a verificação e atualize a sessão.",
  rate_limited: "O Instagram limitou as requisições. Aguarde alguns minutos e tente novamente.",
  action_blocked: "O Instagram respondeu com um aviso de restrição a esta chamada.",
  access_refused: "O Instagram recusou a chamada usada pelo Media Tool.",
  instagram_paused: "Instagram em pausa.",
  profile_already_importing:
    "Este perfil já está sendo importado. Aguarde essa importação terminar ou cancele-a antes de começar outra.",
  disk_full: "Pouco espaço livre em disco. Libere espaço para os downloads continuarem.",
  limit_all_disabled: "A opção Todos está indisponível por enquanto. Escolha 10, 20, 50 ou 100.",
  profile_not_found: "Perfil não encontrado.",
  profile_private: "Este perfil é privado e a sessão atual não tem acesso a ele.",
  content_unavailable: "Conteúdo indisponível.",
  contract_changed: "O Instagram mudou a forma de carregar publicações; é preciso atualizar o Media Tool.",
  unsupported_media: "Tipo de mídia não suportado.",
  network_error: "Falha de rede ao falar com o Instagram.",
  timeout: "O servidor demorou demais para responder.",
  download_failed: "Falha ao baixar o arquivo.",
  invalid_input: "Dados inválidos.",
  internal_error: "Erro interno no backend.",
  interrupted: "Interrompido porque o backend foi reiniciado.",
  ffmpeg_not_found: "FFmpeg não encontrado. Configure o caminho em Configurações.",
  backend_offline: "Não foi possível falar com o backend local.",
};

/** Códigos do Instagram: a mensagem do backend é mais precisa e tem prioridade; o texto fixo é fallback. */
const BACKEND_FIRST = new Set([
  "session_invalid",
  "request_auth_failed",
  "session_expired",
  "checkpoint_required",
  "rate_limited",
  "action_blocked",
  "access_refused",
  "instagram_paused",
  "profile_private",
  "profile_not_found",
  "content_unavailable",
  "contract_changed",
  "unsupported_media",
  "network_error",
  "timeout",
]);

const BY_STATUS: Record<number, string> = {
  400: "Requisição inválida.",
  403: "Requisição recusada pelo backend.",
  404: "Item não encontrado.",
  409: "Conflito com o estado atual.",
  413: "Arquivo grande demais.",
  422: "Dados inválidos.",
  429: "Muitas requisições. Aguarde um pouco.",
  500: "Erro interno no backend.",
  502: "Falha ao falar com um serviço externo.",
  503: "Serviço indisponível no momento.",
};

function hasText(v: string | null | undefined): v is string {
  return typeof v === "string" && v.trim() !== "";
}

/** Mensagem legível para um erro HTTP do backend. */
export function errorMessageFor(code: string | null, status: number, detail: string | null): string {
  if (code && BACKEND_FIRST.has(code) && hasText(detail)) return detail;
  if (code && MESSAGES[code]) {
    if (code === "invalid_input" && hasText(detail)) return `Dados inválidos: ${detail}`;
    return MESSAGES[code];
  }
  if (hasText(detail)) return detail;
  return BY_STATUS[status] ?? `Erro ${status}.`;
}

/** Texto curto para exibir um código de erro gravado em job/batch. */
export function describeErrorCode(code: string | null, message: string | null): string {
  if (code && BACKEND_FIRST.has(code) && hasText(message)) return message;
  if (code && MESSAGES[code]) return MESSAGES[code];
  if (hasText(message)) return message;
  if (code) return `Erro (${code}).`;
  return "Erro desconhecido.";
}

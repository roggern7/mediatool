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
  invalid_profiles: "Há entradas inválidas na lista de perfis. Corrija ou remova as entradas indicadas e tente de novo.",
  no_profiles: "Informe ao menos um perfil.",
  too_many_profiles: "Perfis demais de uma vez. O máximo é 10.000 por importação.",
  invalid_links: "Há links inválidos na lista. Corrija ou remova os links indicados e tente de novo.",
  no_links: "Informe ao menos um link.",
  too_many_links: "Links demais de uma vez. O máximo é 500 por importação.",
  links_failing:
    "Vários links seguidos não puderam ser importados. Confira os links que falharam e retome para continuar pelos próximos.",
  listing_not_resumable: "Esta importação não pode recomeçar do início.",
  group_finished: "Esta importação já terminou; não há nada para cancelar.",
  group_not_found: "Importação não encontrada.",
  payload_too_large:
    "A lista é grande demais para enviar de uma vez. Divida em partes menores ou cole só os nomes de usuário, sem a URL completa.",
  not_supported_by_provider:
    "“Todos” e “Publicações do feed” exigem o método de importação web. Troque o método em Configurações > Instagram.",
  profile_not_found: "Perfil não encontrado.",
  profile_private: "Este perfil é privado e a sessão atual não tem acesso a ele.",
  content_unavailable: "Conteúdo indisponível.",
  contract_changed: "O Instagram mudou a forma de carregar publicações; é preciso atualizar o Media Tool.",
  unsupported_media: "Tipo de mídia não suportado.",
  invalid_media: "O arquivo enviado não é um vídeo válido.",
  integrity_timeout:
    "Não foi possível verificar o arquivo a tempo. Tente um arquivo menor ou com mais quadros-chave.",
  invalid_upload: "Envio inválido. Escolha o arquivo de novo.",
  too_many_uploads: "Já há 2 envios em andamento. Aguarde um terminar.",
  ffmpeg_missing: "FFmpeg não encontrado. Configure o caminho em Configurações.",
  media_not_found: "Uma das mídias escolhidas não existe mais na biblioteca.",
  edit_requires_video: "Só vídeos podem ser editados. Remova as imagens da seleção.",
  edit_no_changes: "Nenhuma edição selecionada.",
  network_error: "Falha de rede ao falar com o Instagram.",
  timeout: "O servidor demorou demais para responder.",
  download_failed: "Falha ao baixar o arquivo.",
  invalid_input: "Dados inválidos.",
  internal_error: "Erro interno no backend.",
  interrupted: "Interrompido porque o backend foi reiniciado.",
  ffmpeg_not_found: "FFmpeg não encontrado. Configure o caminho em Configurações.",
  backend_offline: "Não foi possível falar com o backend local.",
  picker_busy: "Já existe uma janela de escolha de pasta aberta. Conclua ou feche essa janela do Windows.",
  picker_unavailable:
    "Não foi possível abrir a janela de escolha de pasta neste computador. Use a pasta padrão em Documentos.",
  export_token_invalid: "A pasta escolhida expirou ou já foi usada. Escolha a pasta de novo.",
  export_dest_invalid: "Pasta de destino inválida. Escolha outra pasta.",
  export_dest_missing: "A pasta de destino não existe mais. Escolha outra pasta.",
  export_dest_not_writable: "Sem permissão para gravar nesta pasta. Escolha outra pasta.",
  export_dest_forbidden: "Esta pasta é usada pelo Media Tool ou pelo Windows. Escolha uma pasta sua, fora delas.",
  export_dest_drive_root:
    "Não é possível exportar direto para a raiz de uma unidade. Escolha ou crie uma subpasta.",
  export_dest_too_long:
    "O caminho desta pasta é longo demais para o Windows neste computador. Escolha uma pasta com caminho mais curto.",
  export_empty: "Não há mídias para exportar nesta seleção.",
  export_already_running:
    "Já existe uma exportação em andamento para esta origem. Aguarde ela terminar ou cancele-a.",
  export_no_space: "Sem espaço em disco para preparar o ZIP. Libere espaço ou use “Exportar para pasta”.",
  export_read_failed:
    "Um arquivo original não pôde ser lido durante a preparação do ZIP. Tente de novo.",
  export_zip_not_ready: "O ZIP ainda não está pronto.",
  export_zip_expired: "Este ZIP já foi removido. Prepare o ZIP de novo.",
  export_not_folder: "Esta exportação é um ZIP: não há pasta para abrir.",
  export_queue_full: "Há exportações demais na fila. Aguarde alguma terminar antes de pedir outra.",
  export_not_found: "Exportação não encontrada.",
  export_finished: "Esta exportação já terminou; não há nada para cancelar.",
  export_folder_missing:
    "A pasta desta exportação não existe mais (foi movida, renomeada ou apagada). Exporte de novo.",
  export_disk_full: "O disco de destino ficou sem espaço. Libere espaço e exporte de novo para a mesma pasta.",
  export_write_failed: "Muitas falhas seguidas ao gravar na pasta de destino. Verifique a pasta e exporte de novo.",
  post_times_not_found: "Ainda não há horários de publicações para gerar o relatório.",
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
  "links_failing",
  "export_dest_forbidden",
  "export_no_space",
  "export_folder_missing",
  "not_supported_by_provider",
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

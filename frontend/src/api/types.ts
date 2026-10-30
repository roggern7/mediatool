// Tipos espelhando docs/CONTRACTS.md, seção 8.

/** Interface: "all" | "reels" | "photos_carousels". "posts", "videos" e "images" só aparecem em lotes antigos. */
export type ContentType = "reels" | "posts" | "videos" | "images" | "all" | "photos_carousels";
export type MediaType = "video" | "image";
/** Papel da mídia na Biblioteca: "cover" = capa de um vídeo/Reel baixada sozinha. */
export type MediaRole = "original" | "cover";
/** Tipo de mídia da importação unificada. */
export type MediaChoice = "videos" | "all" | "covers";

export type JobStatus =
  | "pending"
  | "downloading"
  | "downloaded"
  | "processing"
  | "completed"
  | "failed"
  | "cancelled";

export type JobStage =
  | "queued"
  | "waiting_retry"
  | "downloading"
  | "muxing"
  | "hashing"
  | "probing"
  | "thumbnail"
  | "processing"
  | "finalizing"
  | "done"
  | "skipped";

export type BatchStatus = "listing" | "running" | "completed" | "failed" | "cancelled";

/** Estado da coleta da lista (docs/BATCH_ROBUSTNESS.md, seções 0 e 9). */
export type ListingState =
  | "pending"
  | "running"
  | "waiting_backpressure"
  | "waiting_rate_limit"
  | "paused"
  | "needs_attention"
  | "done"
  | "cancelled";

export interface Health {
  status: "ok";
  version: string;
  time: string;
}

export interface PresetParams {
  metadata_cleanup: boolean;
  trim_start: number;
  mirror: boolean;
  speed: number;
  auto_adjust: boolean;
  reencode: boolean;
  // Ajustes opcionais: ausente = desligado (a API só devolve os que estão ligados; presets antigos não têm nenhum).
  zoom?: number;
  brightness?: number;
  contrast?: number;
  saturation?: number;
  trim_end?: number;
  pitch_shift?: number;
  grain?: number;
  // Chaves da Edição (aceitas pela API em qualquer preset): limite de duração em s (0 = sem limite), nitidez e
  // sincronia do áudio que começa depois do vídeo. Ausente = desligado.
  max_duration?: number;
  sharpen?: boolean;
  audio_sync_start?: boolean;
}

export interface Preset {
  id: number;
  name: string;
  is_builtin: boolean;
  params: PresetParams;
  created_at: string;
  updated_at: string;
}

export interface ProfileInfo {
  username: string;
  user_id: string | null;
  full_name: string | null;
  avatar_url: string | null;
  post_count: number | null;
  is_private: boolean | null;
  is_verified: boolean | null;
}

export interface Rendition {
  id: number;
  media_id: number;
  preset_id: number | null;
  preset_name: string | null;
  job_id: number | null;
  processed_sha256: string;
  file_size: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean | null;
  created_at: string;
  file_url: string;
}

export interface Job {
  id: number;
  batch_id: number | null;
  media_id: number | null;
  rendition_id: number | null;
  kind: "ingest" | "process";
  preset_id: number | null;
  preset_name: string | null;
  status: JobStatus;
  stage: JobStage | null;
  progress: number | null;
  error_code: string | null;
  error_message: string | null;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string | null;
  is_duplicate: boolean;
  duplicate_reason: "source_id" | "sha256" | "duplicate_in_flight" | null;
  source_platform: string | null;
  source_profile: string | null;
  source_url: string | null;
  source_post_id: string | null;
  source_media_index: number | null;
  media_type: MediaType | null;
  thumbnail_url: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
  elapsed_seconds: number | null;
}

export interface Media {
  id: number;
  source_platform: string;
  source_profile: string | null;
  source_url: string | null;
  source_post_id: string | null;
  source_media_index: number;
  media_type: MediaType;
  /** Ausente em backends antigos (= "original"). */
  role?: MediaRole;
  original_filename: string | null;
  original_sha256: string;
  file_size: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean | null;
  /** Codec de vídeo do original ("vp9", "h264"...); null quando não foi analisado. */
  video_codec: string | null;
  mime_type: string | null;
  taken_at: string | null;
  created_at: string;
  thumbnail_url: string | null;
  original_url: string;
  renditions: Rendition[];
  last_job: Job | null;
}

export interface BatchProgress {
  found: number;
  downloaded: number;
  processed: number;
  skipped: number;
  failed: number;
  pending: number;
  active: number;
  /** Mídias que já existiam na Biblioteca (ausente em backends antigos). */
  duplicates?: number;
  /** Ignoradas por tipo de mídia, período ou visualizações (ausente em backends antigos). */
  filtered?: number;
}

export interface Batch {
  id: number;
  source: "instagram_import" | "library";
  source_profile: string | null;
  content_type: ContentType | null;
  requested_limit: number | null;
  preset_id: number | null;
  preset_name: string | null;
  process: boolean;
  status: BatchStatus;
  found: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  progress: BatchProgress;
  counts: Partial<Record<JobStatus, number>>;
  /** Só com include_jobs=true (no máximo 500, mais recentes); backends antigos sempre mandavam. */
  jobs?: Job[];
  jobs_truncated?: boolean;
  /** Ausentes em backends antigos. needs_attention: o motivo vem em error_code/error_message. */
  listing_state?: ListingState | null;
  /** Só em waiting_rate_limit (teto de páginas por hora): retomada prevista (ISO UTC) e páginas usadas/teto. */
  rate_limit_resume_at?: string | null;
  rate_limit_pages_used?: number | null;
  rate_limit_pages_max?: number | null;
  /** Pausa do lote pelo disjuntor (falhas seguidas com o mesmo código) e retomada automática prevista. */
  paused_reason?: string | null;
  paused_until?: string | null;
  /**
   * Mídias/posts que a listagem não pôde adicionar (inesperado; filtros de propósito não entram). Não muda o
   * status do lote. Ausente em backends antigos; zero quando não houve descarte.
   */
  listing_dropped?: ListingDropped | null;
  /** Por que a listagem terminou: fim real do perfil ou quantidade atingida; null enquanto lista/erro/antigo. */
  /** "period_end": a listagem chegou às publicações mais antigas que o período de postagem pedido. */
  /** "empty_first_page": a 1a página do perfil veio vazia e sem próxima (sem posts, privado ou limite temporário). */
  listing_end?: "profile_end" | "limit" | "period_end" | "empty_first_page" | null;
  /** Filtro de visualizações mínimas do lote; null/ausente = sem filtro (e em backends antigos). */
  views_filter?: ViewsFilter | null;
  /** Grupo de importação (vários perfis); null/ausente em lotes avulsos e antigos. */
  group_id?: number | null;
  group_position?: number | null;
  group_state?: GroupChildState | null;
  /** Última exportação para pasta concluída deste lote; null/ausente se nunca foi exportado. */
  last_export?: LastExport | null;
  /** "links": importação por link (source_profile null); null = lote de Biblioteca; ausente em backends antigos. */
  import_mode?: "profile" | "links" | null;
  /** Só em lote por link: links por situação. */
  links?: BatchLinksSummary | null;
  /** Tipo de mídia pedido (importação unificada); null/ausente em lotes antigos de outro conteúdo. */
  media?: MediaChoice | null;
}

export interface BatchLinksSummary {
  total: number;
  pending: number;
  resolved: number;
  failed: number;
}

/** Um link de um lote por link (GET /api/batches/{id}/links). */
export interface BatchLink {
  position: number;
  url: string;
  shortcode: string;
  status: "pending" | "resolved" | "failed" | "cancelled";
  error_code: string | null;
  error_message: string | null;
  items_found: number | null;
  profile: string | null;
}

export interface BatchLinks {
  items: BatchLink[];
  total: number;
}

export interface ValidLink {
  shortcode: string;
  /** URL normalizada: é ela que vai em POST /api/import-links. */
  url: string;
}

/** Pré-validação dos links (POST /api/import-links/preview). */
export interface LinksPreview {
  valid: ValidLink[];
  invalid: InvalidProfileEntry[];
  duplicates: number;
}

export interface ImportLinksRequest {
  links: string[];
  preset_id: number | null;
  process: boolean;
  /** "Editar após baixar" (as mesmas configurações de POST /api/edits); exclusivo com `process`/`preset_id`. */
  edit?: EditSettings | null;
}

export type GroupChildState = "waiting" | "active" | "attention" | "completed" | "failed" | "cancelled";

/** Contrato: docs/CONTRACTS.md, seção 8.1. */
export interface ProfilesPreview {
  valid: string[];
  invalid: InvalidProfileEntry[];
  duplicates: number;
}

export interface InvalidProfileEntry {
  entry: string;
  reason: string;
}

export interface GroupProfiles {
  total: number;
  waiting: number;
  active: number;
  completed: number;
  attention: number;
  failed: number;
  cancelled: number;
}

export interface ImportGroup {
  id: number;
  content_type: ContentType;
  /** Por perfil; null = "Todos". */
  requested_limit: number | null;
  preset_id: number | null;
  preset_name: string | null;
  process: boolean;
  created_at: string;
  finished_at: string | null;
  status: "active" | "attention" | "completed" | "cancelled";
  /** Algum filho em atenção ou falho (vale também com o grupo ativo ou concluído). */
  attention: boolean;
  profiles: GroupProfiles;
  profile_end_count: number;
  /** Filhos cuja listagem terminou pelo corte do período de postagem. */
  period_end_count?: number;
  progress: BatchProgress;
  listing_dropped_total: number;
  /** Filtro de visualizações mínimas (soma dos perfis); null/ausente = sem filtro. */
  views_filter?: ViewsFilter | null;
  /** Última exportação para pasta concluída do grupo inteiro; null/ausente se nunca foi exportado. */
  last_export?: LastExport | null;
  media?: MediaChoice | null;
}

export interface SkippedProfile {
  profile: string;
  reason: string;
}

export interface ImportGroupDetail {
  group: ImportGroup;
  children: Batch[];
  next_after_position: number | null;
  /** Só na criação. */
  skipped?: SkippedProfile[] | null;
  duplicates?: number | null;
}

export interface ImportGroupList {
  items: ImportGroup[];
  total: number;
  next_before_id: number | null;
}

/**
 * Filtro de visualizações mínimas: vídeos analisados (= approved + below + no_count) por veredito. "Aprovados" não é
 * "baixados": vídeo aprovado que já estava na Biblioteca vira duplicata (o número real é `progress.downloaded`).
 */
/** Filtros da importação por perfil (visualizações mínimas e/ou período de postagem). */
export interface ViewsFilter {
  /** null = sem filtro de visualizações (só o período). */
  min_views: number | null;
  /** Máximo de visualizações; null/ausente = sem máximo. */
  max_views?: number | null;
  /** "Últimos N dias"; null = qualquer data. */
  posted_within_days?: number | null;
  /** Corte gravado na criação (UTC). */
  posted_after?: string | null;
  analyzed: number;
  approved: number;
  below: number;
  no_count: number;
  /** Publicações mais antigas que o corte do período (não baixadas). */
  out_of_period?: number;
  /** Acima do máximo de visualizações (só com `max_views`). */
  above?: number;
  /** Parte dos analisados que ainda é da página aberta da listagem. */
  pending: number;
}

export interface ListingDropped {
  unexpected_total: number;
  /** Motivo (nome fixo do backend) -> quantidade. */
  reasons: Record<string, number>;
  /** Parte do total que ainda é da página aberta da listagem (ex.: lote aguardando ação); já está no total. */
  pending?: number;
}

export interface Paged<T> {
  items: T[];
  total: number;
  /** Paginação keyset: próximo before_id; null no fim; ausente em backends antigos (só offset). */
  next_before_id?: number | null;
}

export interface JobsPage extends Paged<Job> {
  counts: Partial<Record<JobStatus, number>>;
}

export type SessionTestResult =
  | "valid"
  | "invalid"
  | "expired"
  | "checkpoint_required"
  | "rate_limited"
  | "restricted"
  | "api_refused"
  | "timeout"
  | "network_error"
  | "instagram_error"
  | "session_missing";

export interface SessionTest {
  ok: boolean;
  /** Ausente em respostas antigas; nesse caso usa-se code. */
  result?: SessionTestResult | null;
  code: string;
  message: string;
  username: string | null;
  http_status?: number | null;
  duration_ms?: number | null;
  detail?: string | null;
  /** true: a resposta não encerrou a sessão; false: encerrou ou pediu login; null: desconhecido. */
  session_recognized?: boolean | null;
  /** Estado da chamada de listagem usada pelo Media Tool; null em registros antigos. */
  listing_api?: ListingApiState | null;
  session_present?: boolean | null;
  bootstrap?: "ok" | "failed" | "unknown" | null;
  graphql?: "ok" | "failed" | "unknown" | null;
  tested_at: string;
}

export type InstagramProvider = "auto" | "web" | "gallery_dl";

export type ListingApiState = "ok" | "refused" | "rate_limited" | "unknown";

export interface InstagramStatus {
  session_configured: boolean;
  source: "file" | "env" | null;
  updated_at: string | null;
  last_test: SessionTest | null;
  /** Nomes dos cookies adicionais salvos (nunca os valores). */
  extra_keys?: string[];
  /** Pausa de proteção do Media Tool (rate limit ou aviso explícito de restrição), ISO ou null. */
  paused_until?: string | null;
  pause_reason?: "rate_limited" | "action_blocked" | "checkpoint_required" | "session_invalid" | "auth_check" | null;
  /** "session": downloads e processamento continuam, só consultas ao Instagram aguardam; "instagram": downloads também. */
  pause_scope?: "session" | "instagram" | null;
  /** Próxima verificação automática (ISO UTC) ou null. */
  next_attempt_at?: string | null;
  /** Checkpoint ou sessão inválida: o dono precisa agir (a verificação automática continua a cada 6 h). */
  action_required?: boolean;
}

/** Espaço em disco do storage. free_bytes = -1 quando não foi possível medir. */
export interface DiskStatus {
  free_bytes: number;
  required_bytes: number;
  ok?: boolean;
  disk_full?: boolean;
}

export interface Settings {
  ffmpeg_path: string | null;
  ffprobe_path: string | null;
  concurrency: number;
  /** Dois pools (ausentes em backends antigos): downloads 1-4 e processamentos FFmpeg 1-2. */
  download_concurrency?: number;
  processing_concurrency?: number;
  /** Ausente em backends antigos. */
  disk?: DiskStatus | null;
  default_preset_id: number | null;
  /** Ausente em backends antigos. */
  instagram_provider?: InstagramProvider;
  /** Somente leitura no frontend; não exibir. */
  instagram_web_doc_ids: Record<string, string>;
  effective:{ ffmpeg_path: string | null; ffprobe_path: string | null };
  storage: { originals: string; processed: string; temp: string; thumbs: string };
  database_path: string;
  instagram: InstagramStatus;
}

export interface SettingsUpdate {
  ffmpeg_path?: string | null;
  ffprobe_path?: string | null;
  concurrency?: number;
  download_concurrency?: number;
  processing_concurrency?: number;
  default_preset_id?: number | null;
  instagram_provider?: InstagramProvider | null;
}

export interface SessionInput {
  sessionid: string;
  csrftoken?: string;
  ds_user_id?: string;
  mid?: string;
  ig_did?: string;
}

export type CheckStatus = "ok" | "warn" | "error";

export interface ToolDiag {
  found: boolean;
  path: string | null;
  version: string | null;
}

export interface StorageDiag {
  path: string;
  exists: boolean;
  writable: boolean;
}

export interface Diagnostics {
  generated_at: string;
  overall: CheckStatus;
  checks: { id: string; label: string; status: CheckStatus; detail: string }[];
  python: { version: string; executable: string };
  sqlite: { version: string; path: string; schema_version: number | string | null };
  ffmpeg: ToolDiag;
  ffprobe: ToolDiag;
  gallery_dl: ToolDiag;
  storage: Record<string, StorageDiag>;
  worker: { running: boolean; concurrency: number; active_jobs: number };
  instagram: { session_configured: boolean };
  backend: { host: string; port: number; version: string };
}

export interface ImportRequest {
  profile: string;
  content_type: ContentType;
  /** "Últimos N": inteiro de 1 a 1.000.000; null = "Todos" (até o fim real do perfil). */
  limit: number | null;
  /** Opcionais na API (padrão: null e false = só baixar originais); o painel sempre manda os dois. */
  preset_id: number | null;
  process: boolean;
  /** "Editar após baixar" (as mesmas configurações de POST /api/edits); exclusivo com `process`/`preset_id`. */
  edit?: EditSettings | null;
  /** Visualizações mínimas (só Reels/Vídeos). A chave é OMITIDA sem filtro: o corpo fica igual ao de antes. */
  min_views?: number;
  /** Período de postagem: últimos N dias (1 a 3650). Omitida = qualquer data. */
  posted_within_days?: number;
}

export interface ImportGroupRequest {
  profiles: string[];
  content_type: ContentType;
  /** Por perfil. */
  limit: number | null;
  preset_id: number | null;
  process: boolean;
  /** "Editar após baixar" (as mesmas configurações de POST /api/edits); aplicada a todos os perfis; exclusivo com `process`/`preset_id`. */
  edit?: EditSettings | null;
  /** Por perfil; omitida sem filtro. */
  min_views?: number;
  /** O mesmo corte para todos os perfis; omitida = qualquer data. */
  posted_within_days?: number;
}

/** Corpo de POST /api/imports/classify (exatamente um dos dois). */
export type ClassifyRequest = { inputs: string[] } | { text: string };

export interface ClassifiedLink {
  shortcode: string;
  url: string;
  kind: "p" | "reel";
}

export interface ClassifyResult {
  profiles: string[];
  links: ClassifiedLink[];
  invalid: InvalidProfileEntry[];
  duplicates: number;
}

/** Corpo de POST /api/imports/start (importação unificada). */
export interface UnifiedImportRequest {
  inputs: string[];
  media: MediaChoice;
  /** POR PERFIL; null = todas (exige confirm_all). */
  limit: number | null;
  confirm_all: boolean;
  /** Só perfis; chaves omitidas sem filtro. */
  posted_within_days?: number;
  min_views?: number;
  max_views?: number;
  post_processing: "none" | "preset" | "edit";
  preset_id?: number;
  edit?: EditSettings;
}

export interface UnifiedImportResult {
  batch_ids: number[];
  group_id: number | null;
  links_batch_id: number | null;
  profiles: number;
  links: number;
  invalid: InvalidProfileEntry[];
  duplicates: number;
  skipped?: SkippedProfile[];
  /** Avisos sem erro (ex.: filtros de perfil ignorados nas publicações por link). Ausente em backends antigos. */
  notes?: string[];
}

export type MediaView = "all" | "originals" | "processed";

export interface MediaQuery {
  view?: MediaView;
  media_type?: MediaType;
  /** "cover" = só capas; omitido = todas. */
  role?: MediaRole;
  profile?: string;
  q?: string;
  limit?: number;
  offset?: number;
  before_id?: number;
}

/** Corpo de POST /api/media/process-by-filter (mesmo filtro da Biblioteca). */
export interface ProcessByFilterRequest {
  view: MediaView;
  media_type?: MediaType;
  role?: MediaRole;
  q?: string;
  profile?: string;
  preset_id: number;
}

export interface JobsQuery {
  status?: JobStatus[];
  batch_id?: number;
  ids?: number[];
  limit?: number;
  offset?: number;
  before_id?: number;
  /** Período de criação [created_from, created_to), ISO 8601 com fuso. */
  created_from?: string;
  created_to?: string;
}

/** Exportação para pasta. Contrato: docs/CONTRACTS.md, seção 8.2. */
export type ExportStatus = "pending" | "running" | "completed" | "cancelled" | "failed" | "expired";
/** `folder`: cópia para uma pasta; `zip`: um .zip plano preparado pelo servidor para download. */
export type ExportMode = "folder" | "zip";

export interface LastExport {
  id: number;
  /** Pasta de destino, só para exibição (o painel nunca envia caminho ao backend). */
  dest_path: string;
  status: ExportStatus;
  total: number;
  /** Gravadas na pasta (inclui as renomeadas). */
  copied: number;
  /** Arquivo idêntico já estava na pasta. */
  skipped_existing: number;
  /** Gravadas com outro nome porque havia um arquivo diferente com o mesmo nome. */
  renamed: number;
  /** Original ausente no storage. */
  missing: number;
  failed: number;
  finished_at: string | null;
  mode?: ExportMode;
}

export interface ExportJob extends LastExport {
  /** Só em mode = "zip". `download_url` (com o token de download) e `zip_bytes` só com o ZIP pronto. */
  zip_name?: string | null;
  zip_bytes?: number | null;
  zip_ready?: boolean;
  zip_expires_at?: string | null;
  download_url?: string | null;
  /** O que a exportação leva (ausente nas respostas antigas = "originals"). */
  content?: ExportContent;
  kind: "batch" | "group" | "library";
  batch_id: number | null;
  group_id: number | null;
  /** Arquivos já tratados: copied + skipped_existing + missing + failed. */
  processed: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
}

export interface ExportFilter {
  view: MediaView;
  media_type?: MediaType;
  role?: MediaRole;
  q?: string;
  profile?: string;
}

export type ExportSelection = { media_ids: number[] } | { filter: ExportFilter; exclude_ids?: number[] };

export type ExportSource =
  | { kind: "batch"; id: number }
  | { kind: "group"; id: number }
  | { kind: "library"; selection: ExportSelection };

/** `token`: pasta escolhida no seletor nativo; `default`: pasta padrão criada pelo servidor em Documentos. */
export type ExportDestination = { token: string } | { default: true } | { zip: true };

/** O que a exportação leva: originais (padrão), versões editadas/processadas ou os dois. */
export type ExportContent = "originals" | "edited" | "both";

export interface ExportRequest {
  source: ExportSource;
  destination: ExportDestination;
  /** Ausente = "originals" (o corpo antigo continua idêntico). */
  content?: ExportContent;
}

export interface PickFolderResult {
  cancelled: boolean;
  token: string | null;
  path_display: string | null;
}

// ---- Edição de vídeos (POST /api/edits) e upload de vídeo local (POST /api/library/uploads) ----

export type EditDurationMode = "original" | "15" | "30" | "60" | "custom";
export type EditOutputType = "mp4" | "auto";

export interface EditSettings {
  duration_mode: EditDurationMode;
  /** Obrigatório só em "custom" (segundos, > 0 e <= 7200); null nos demais. */
  custom_seconds: number | null;
  /** 1,0 a 1,5. */
  speed: number;
  sharpen: boolean;
  output_type: EditOutputType;
}

/** Recorte manual de um vídeo (segundos do original): trecho [start, end), end - start >= 0,1. */
export interface EditTrim {
  media_id: number;
  start: number;
  end: number;
}

export interface EditRequest {
  /** 1 a 500 vídeos. */
  media_ids: number[];
  settings: EditSettings;
  /** Opcional: no máximo um por vídeo de `media_ids`; para eles a duração geral é ignorada. */
  trims?: EditTrim[];
}

export interface UploadResult {
  media: Media;
  /** Conteúdo idêntico (sha256) a uma mídia já existente: nenhuma mídia nova foi criada. */
  duplicate: boolean;
}

/** Pasta da página Editar (GET /api/edit/folders). `key`: group:<id>, batch:<id>, local:<aaaa-mm-dd> ou other. */
export interface EditFolder {
  key: string;
  kind: "group" | "batch" | "local" | "other";
  import_mode: "profile" | "links" | null;
  profiles: string[];
  created_at: string | null;
  /** Dia local do envio (aaaa-mm-dd), só em `local`. */
  day: string | null;
  video_count: number;
  rendition_count: number;
  /** Originais + versões editadas, em bytes. */
  total_bytes: number;
  /** Vídeos desta pasta que também estão em outra pasta (apagar a pasta os tira de lá também). */
  shared_count: number;
}

export interface EditFolderList {
  items: EditFolder[];
}

/** Corpo de POST /api/media/delete: `media_ids` OU `folder`; `delete_original: true` confirma. */
export type MediaDeleteRequest =
  | { media_ids: number[]; delete_original: true }
  | { folder: string; delete_original: true };

export interface MediaDeleteIssue {
  id: number;
  code: string;
  message: string;
}

/** POST /api/imports/history/hide: itens OU `all_finished`; só oculta do histórico, salvo `delete_media`. */
export interface HistoryHideRequest {
  batch_ids?: number[];
  group_ids?: number[];
  all_finished?: boolean;
  /** Também apaga os vídeos dessas importações (mesmas proteções de POST /api/media/delete). */
  delete_media?: boolean;
  /** Só conta (vídeos e espaço), sem mudar nada. */
  dry_run?: boolean;
}

export interface HistoryHideResult {
  batches: number;
  groups: number;
  video_count: number;
  total_bytes: number;
  /** Desses vídeos, quantos também estão em outra importação ainda visível. */
  shared_count: number;
  dry_run: boolean;
  deleted: MediaDeleteResult | null;
}

export interface MediaDeleteResult {
  deleted: number;
  deleted_ids: number[];
  skipped: MediaDeleteIssue[];
  failed: MediaDeleteIssue[];
}

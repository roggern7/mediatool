// Tipos espelhando docs/CONTRACTS.md, seção 8.

/** Interface: "all" | "reels" | "photos_carousels". "posts", "videos" e "images" só aparecem em lotes antigos. */
export type ContentType = "reels" | "posts" | "videos" | "images" | "all" | "photos_carousels";
export type MediaType = "video" | "image";

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
  original_filename: string | null;
  original_sha256: string;
  file_size: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean | null;
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
  listing_end?: "profile_end" | "limit" | null;
  /** Grupo de importação (vários perfis); null/ausente em lotes avulsos e antigos. */
  group_id?: number | null;
  group_position?: number | null;
  group_state?: GroupChildState | null;
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
  progress: BatchProgress;
  listing_dropped_total: number;
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
}

export interface ImportGroupRequest {
  profiles: string[];
  content_type: ContentType;
  /** Por perfil. */
  limit: number | null;
  preset_id: number | null;
  process: boolean;
}

export type MediaView = "all" | "originals" | "processed";

export interface MediaQuery {
  view?: MediaView;
  media_type?: MediaType;
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
}

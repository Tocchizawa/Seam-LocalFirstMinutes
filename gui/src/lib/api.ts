const BASE_URL = "http://127.0.0.1:18900";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: { message: res.statusText } }));
    throw new Error(err.error?.message || err.detail?.message || res.statusText);
  }
  return res.json();
}

// Health
export const checkHealth = () => request<{ status: string }>("/health");

// Projects
export interface Member {
  name: string;
  role: string;
}

export interface CorrectionPair {
  wrong: string;
  correct: string;
}

export interface Project {
  id: string;
  name: string;
  repo_path: string | null;
  doc_dirs: string[];
  output_dir: string;
  members: Member[];
  glossary: string[];
  corrections?: CorrectionPair[];
  created_at: string;
  updated_at: string;
}

export interface ProjectCreate {
  name: string;
  repo_path?: string | null;
  doc_dirs?: string[];
  output_dir: string;
  members?: Member[];
  glossary?: string[];
  corrections?: CorrectionPair[];
}

export const listProjects = () => request<Project[]>("/api/projects");

export const createProject = (data: ProjectCreate) =>
  request<Project>("/api/projects", {
    method: "POST",
    body: JSON.stringify(data),
  });

export const updateProject = (id: string, data: Partial<ProjectCreate>) =>
  request<Project>(`/api/projects/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });

export const deleteProject = (id: string, deleteOutput = false) =>
  request<{ status: string }>(`/api/projects/${id}?delete_output=${deleteOutput}`, {
    method: "DELETE",
  });

// Settings
export const getSettings = () => request<Record<string, unknown>>("/api/settings");

export const updateSettings = (data: Record<string, unknown>) =>
  request<Record<string, unknown>>("/api/settings", {
    method: "PUT",
    body: JSON.stringify(data),
  });

export interface CliModelOption {
  slug: string;
  display_name: string;
  description?: string;
}

export const getCodexModels = () =>
  request<CliModelOption[]>("/api/settings/cli/codex/models");

export const getClaudeCodeModels = () =>
  request<CliModelOption[]>("/api/settings/cli/claude_code/models");

// OS 操作ユーティリティ (バックエンドで subprocess: pbcopy / open / open -R)
// Tauri webview の clipboard/opener が permission の問題で動かないため確実な経路。
export const writeClipboard = (text: string) =>
  request<{ status: string; bytes: number }>("/api/util/clipboard", {
    method: "POST",
    body: JSON.stringify({ text }),
  });

export const openInDefaultApp = (path: string) =>
  request<{ status: string; path: string }>("/api/util/open", {
    method: "POST",
    body: JSON.stringify({ path }),
  });

export const revealInFinder = (path: string) =>
  request<{ status: string; path: string }>("/api/util/reveal", {
    method: "POST",
    body: JSON.stringify({ path }),
  });

// Debug
export interface DebugStatus {
  debug_enabled: boolean;
  generated_at: string;
  runtime: Record<string, unknown>;
  process: Record<string, unknown>;
  system: Record<string, unknown>;
  gc: Record<string, unknown>;
  log_tail: string[];
}

export const getDebugStatus = (lines = 120) =>
  request<DebugStatus>(`/api/debug/status?lines=${Math.max(20, Math.min(500, lines))}`);

// Minutes
export interface Minutes {
  id: string;
  session_id: string;
  project_id: string;
  title: string;
  date: string;
  started_at: string;
  duration_sec: number;
  transcript: {
    start: number;
    end: number;
    text: string;
    speaker_id?: string | null;
    speaker_label?: string | null;
    speaker_confidence?: number | null;
  }[];
  summary: string;
  whisper_model?: string | null;
  llm_model?: string | null;
  created_at: string;
}

export const listMinutes = (projectId?: string, limit = 20, offset = 0) => {
  const params = new URLSearchParams();
  if (projectId) params.set("project", projectId);
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  return request<Minutes[]>(`/api/minutes?${params}`);
};

export const getMinutes = (id: string) => request<Minutes>(`/api/minutes/${id}`);

export const deleteMinutes = (id: string) =>
  request<{ status: string }>(`/api/minutes/${id}`, { method: "DELETE" });

export const updateMinutesTitle = (id: string, title: string) =>
  request<Minutes>(`/api/minutes/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });

export const updateMinutesSummary = (id: string, summary: string) =>
  request<Minutes>(`/api/minutes/${id}/summary`, {
    method: "PATCH",
    body: JSON.stringify({ summary }),
  });

export const moveMinutesToProject = (id: string, projectId: string) =>
  request<{ status: string }>(`/api/minutes/${id}/project`, {
    method: "PUT",
    body: JSON.stringify({ project_id: projectId }),
  });

export interface ImportAudioResponse {
  status: string;
  minutes: Minutes;
  session_id: string;
  audio_path: string;
  source_path: string;
  used_existing_transcript: boolean;
  start_retranscribe: boolean;
  summary_enqueued: boolean;
  retranscribe: {
    status: string;
    session_id: string;
  } | null;
}

export const importAudioMinutes = (
  projectId: string,
  sourcePath: string,
  options: { title?: string; startRetranscribe?: boolean } = {},
) =>
  request<ImportAudioResponse>("/api/minutes/import-audio", {
    method: "POST",
    body: JSON.stringify({
      project_id: projectId,
      source_path: sourcePath,
      title: options.title?.trim() || undefined,
      start_retranscribe: options.startRetranscribe,
    }),
  });

export interface MinutesSearchResult extends Minutes {
  highlights?: {
    title: string | null;
    summary: string | null;
    transcript: string | null;
  };
}

export const searchMinutes = (q: string, projectId?: string) => {
  const params = new URLSearchParams({ q });
  if (projectId) params.set("project", projectId);
  return request<MinutesSearchResult[]>(`/api/minutes/search?${params}`);
};

export const getMinutesMarkdown = (id: string) =>
  request<{ content: string }>(`/api/minutes/${id}/markdown`);

export const exportMinutes = (id: string) =>
  request<{ status: string; path: string }>(`/api/minutes/${id}/export`, {
    method: "POST",
  });

export const retranscribeMinutes = (id: string) =>
  request<{ status: string; session_id: string }>(
    `/api/minutes/${id}/retranscribe`,
    { method: "POST" },
  );

export const cancelRetranscribeMinutes = (id: string) =>
  request<{ status: string; session_id: string }>(
    `/api/minutes/${id}/retranscribe/cancel`,
    { method: "POST" },
  );

// Devices
export interface AudioDevice {
  id: number;
  name: string;
  channels: number;
  sample_rate: number;
  is_default: boolean;
  is_blackhole: boolean;
}

export const listDevices = (options: { refresh?: boolean } = {}) => {
  const params = new URLSearchParams();
  if (options.refresh) params.set("refresh", "true");
  const query = params.toString();
  return request<{
    devices: AudioDevice[];
    screen_capture_available: boolean;
    devices_refreshed?: boolean;
  }>(`/api/devices${query ? `?${query}` : ""}`);
};

// Recording
export const startRecording = (projectId: string, micDevice?: number | null, captureSystem = true) =>
  request<{ session_id: string; has_system_audio: boolean }>("/api/recording/start", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, mic_device: micDevice ?? null, capture_system: captureSystem }),
  });

export interface RecordingResult {
  session_id: string;
  wav_path: string | null;
  duration_sec: number;
  error: string | null;
}

export const stopRecording = () =>
  request<{ status: string }>("/api/recording/stop", { method: "POST" });

export const getRecordingStatus = () =>
  request<{ recording: boolean; elapsed_sec: number; session_id?: string | null; mic_muted?: boolean }>(
    "/api/recording/status",
  );

export const setMicMuted = (muted: boolean) =>
  request<{ status: string; muted: boolean }>("/api/recording/mic-mute", {
    method: "POST",
    body: JSON.stringify({ muted }),
  });

export const getLastRecording = () =>
  request<RecordingResult>("/api/recording/last");

export const audioPlayUrl = (sessionId: string) =>
  `${BASE_URL}/api/recording/play/${encodeURIComponent(sessionId)}?v=wav-playback-1`;

// Pipeline
export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  speaker_id?: string | null;
  speaker_label?: string | null;
  speaker_confidence?: number | null;
}

export const getSessionSegments = (sessionId: string) =>
  request<TranscriptSegment[]>(
    `/api/recording/sessions/${encodeURIComponent(sessionId)}/segments`,
  );

export interface SessionAudioInfo {
  name: string;
  size_bytes: number;
}

export const getSessionAudioInfo = (sessionId: string) =>
  request<SessionAudioInfo>(
    `/api/recording/sessions/${encodeURIComponent(sessionId)}/audio_info`,
  );

export interface RecoverSessionResponse {
  status: string;
  created: boolean;
  used_audio: boolean;
  used_segments: boolean;
  minutes: Minutes;
  retranscribe?: {
    status: string;
    reason?: string;
    session_id?: string;
  };
}

export const recoverSessionMinutes = (sessionId: string, startRetranscribe = false) =>
  request<RecoverSessionResponse>(
    `/api/recording/sessions/${encodeURIComponent(sessionId)}/recover?start_retranscribe=${startRetranscribe ? "true" : "false"}`,
    { method: "POST" },
  );

export interface PipelineStatus {
  session_id?: string;
  project_id?: string;
  state: "idle" | "recording" | "stopping" | "transcribing" | "done" | "error";
  message: string;
  progress?: number; // 0.0 - 1.0 (再文字起こし時のみ)
  // 細粒度ステージ (バックエンド src/pipeline/state.py の Stage に対応)
  stage?: string | null;
  stage_label?: string | null;
  stage_step?: number | null;     // 1..N (現在のステップ)
  stage_total?: number | null;    // N (全ステップ数)
  result: RecordingResult | null;
  transcript: {
    segments: TranscriptSegment[];
    count: number;
    chars: number;
    time_sec: number;
  } | null;
  error: string | null;
  started_at?: string;
}

export const getPipelineStatus = () =>
  request<PipelineStatus>("/api/recording/pipeline");

export const getPipelines = () =>
  request<PipelineStatus[]>("/api/recording/pipelines");

/** sessionId 指定で対応する pipeline を取得 (再文字起こしの状態復元用)。 */
export const getPipelineBySession = async (
  sessionId: string,
): Promise<PipelineStatus | null> => {
  const all = await getPipelines();
  return all.find((p) => p.session_id === sessionId) || null;
};

export const dismissPipeline = (sessionId: string) =>
  request<{ status: string; session_id: string }>(
    `/api/recording/pipelines/${sessionId}`,
    { method: "DELETE" },
  );

export const resetPipeline = () =>
  request<{ status: string; cleared?: number }>(
    "/api/recording/pipeline/reset",
    { method: "POST" },
  );

export interface RecoveryItem {
  session_id: string;
  started_at: string | null;
  date: string;
  hhmm: string;
  prior_state: string | null;
}
export interface RecoveryStatus {
  state: "idle" | "running" | "done";
  total: number;
  current: number;
  item: RecoveryItem | null;
  recovered: number;
  finished_at: string | null;
}
export const getRecoveryStatus = () =>
  request<RecoveryStatus>("/api/recording/recovery/status");

// WebSocket
export const WS_URL = "ws://127.0.0.1:18900/ws";

// Speaker memory (global)
export interface SpeakerProfile {
  id: string;
  label: string;
  appearances: number;
  total_audio_sec: number;
  created_at: string;
  updated_at: string;
  sample_available: boolean;
  sample_session_id?: string | null;
}

export const listSpeakers = () =>
  request<{ speakers: SpeakerProfile[] }>("/api/speakers");

export const renameSpeaker = (speakerId: string, label: string) =>
  request<{ speaker: { id: string; label: string; updated_at: string } }>(
    `/api/speakers/${speakerId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ label }),
    },
  );

export const deleteSpeaker = (speakerId: string) =>
  request<{ status: string; speaker_id: string; affected_segments: number }>(
    `/api/speakers/${speakerId}`,
    { method: "DELETE" },
  );

export const mergeSpeakers = (primaryId: string, sourceIds: string[]) =>
  request<{
    status: string;
    primary: SpeakerProfile;
    merged_ids: string[];
    affected_segments: number;
  }>(`/api/speakers/merge`, {
    method: "POST",
    body: JSON.stringify({ primary_id: primaryId, source_ids: sourceIds }),
  });

export const speakerSampleUrl = (speakerId: string) =>
  `${BASE_URL}/api/speakers/${speakerId}/sample`;

// Diarization (pyannote)
export interface DiarizationStatus {
  pyannote_available: boolean;
  has_hf_token: boolean;
}

export const getDiarizationStatus = () =>
  request<DiarizationStatus>("/api/speakers/diarization/status");

export const setHfToken = (token: string) =>
  request<{ status: string }>("/api/speakers/diarization/token", {
    method: "PUT",
    body: JSON.stringify({ token }),
  });

export const deleteHfToken = () =>
  request<{ status: string }>("/api/speakers/diarization/token", {
    method: "DELETE",
  });

export interface DiarizationTestResult {
  ok: boolean;
  code?: string;
  message: string;
}

export const testDiarization = () =>
  request<DiarizationTestResult>("/api/speakers/diarization/test", {
    method: "POST",
  });

// ─────────────────────────────────────────────────────────
// Summarize (要約)
// ─────────────────────────────────────────────────────────

export type SummarizeProvider =
  | "ollama"
  | "claude_api"
  | "openai"
  | "gemini"
  | "claude_code"
  | "codex";

export const SUMMARIZE_PROVIDERS: SummarizeProvider[] = [
  "ollama",
  "claude_api",
  "openai",
  "gemini",
  "claude_code",
  "codex",
];

export const CLOUD_PROVIDERS: SummarizeProvider[] = [
  "claude_api",
  "openai",
  "gemini",
];

export const CLI_PROVIDERS: SummarizeProvider[] = [
  "claude_code",
  "codex",
];

export const PROVIDER_LABELS: Record<SummarizeProvider, string> = {
  ollama: "Ollama (ローカル)",
  claude_api: "Claude API",
  openai: "OpenAI (GPT)",
  gemini: "Gemini",
  claude_code: "Claude Code (CLI)",
  codex: "Codex (CLI)",
};

export type SummarizeJobState =
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "skipped"
  | "none";

export interface SummarizeStatus {
  state: SummarizeJobState;
  minutes_id?: string;
  provider?: string | null;
  model?: string | null;
  partial_text?: string;
  error_code?: string | null;
  error_message?: string | null;
  started_at?: number | null;
  finished_at?: number | null;
  stage?: string | null;
  stage_label?: string | null;
  /** CLI provider が「いま何をしているか」を示す短いラベル (例: "Read: docs/README.md") */
  activity?: string | null;
}

export const triggerSummarize = (
  minutesId: string,
  provider?: SummarizeProvider | null,
) => {
  const qs = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return request<{ job_id: string; status: string }>(
    `/api/minutes/${minutesId}/summarize${qs}`,
    { method: "POST" },
  );
};

export const getSummarizeStatus = (minutesId: string) =>
  request<SummarizeStatus>(`/api/minutes/${minutesId}/summarize/status`);

export interface ActiveSummarize {
  minutes_id: string;
  project_id: string | null;
  state: string | null;
}

export const getActiveSummarizes = () =>
  request<ActiveSummarize[]>("/api/summarize/active");

export const cancelSummarize = (minutesId: string) =>
  request<{ state: string }>(`/api/minutes/${minutesId}/summarize/cancel`, {
    method: "POST",
  });

export const putApiKey = (provider: SummarizeProvider, token: string) =>
  request<{ status: string; provider: string }>(`/api/summarize/api-key`, {
    method: "PUT",
    body: JSON.stringify({ provider, token }),
  });

export const deleteApiKey = (provider: SummarizeProvider) =>
  request<{ status: string; provider: string }>(
    `/api/summarize/api-key/${encodeURIComponent(provider)}`,
    { method: "DELETE" },
  );

export const listApiKeys = () =>
  request<{ providers: Partial<Record<SummarizeProvider, boolean>> }>(
    `/api/summarize/api-keys`,
  );

export interface ProviderTestResult {
  ok: boolean;
  code: string;
  message: string;
  model?: string | null;
}

export const testSummarizeProvider = (provider: SummarizeProvider) =>
  request<ProviderTestResult>(`/api/summarize/test`, {
    method: "POST",
    body: JSON.stringify({ provider }),
  });

export const consentProvider = (provider: SummarizeProvider) =>
  request<{ status: string; provider: string }>(
    `/api/summarize/consent/${encodeURIComponent(provider)}`,
    { method: "POST" },
  );

export interface RecommendedProvider {
  provider: SummarizeProvider | null;
  reason: string;
}

export const getRecommendedProvider = () =>
  request<RecommendedProvider>("/api/summarize/recommended");

// ─────────────────────────────────────────────────────────
// Dictionary (用語集自動生成 / 誤転写補正)
// ─────────────────────────────────────────────────────────

export interface GlossarySuggestion {
  term: string;
  description: string;
}

export interface GlossaryJobStart {
  job_id: string;
  state: "running" | "done" | "error";
}

export interface GlossaryJobStatus {
  job_id: string;
  state: "running" | "done" | "error";
  elapsed_sec: number;
  provider: string;
  model: string;
  /** CLI provider (claude_code / codex) かどうか。true なら自由探索モード */
  is_cli: boolean;
  /** 現在 agent が行っている処理 (例: "Read: README.md") */
  current_activity: string;
  suggestions?: GlossarySuggestion[];
  error?: { code: string; message: string };
}

/** ジョブ起動: 即座に job_id を返す (CLI provider なら処理は数分かかる)。 */
export const startAutoGenerateGlossary = (projectId: string) =>
  request<GlossaryJobStart>(
    `/api/projects/${projectId}/glossary/auto-generate`,
    { method: "POST" },
  );

/** ジョブ進捗を取得。state=running の間は完了するまで再ポーリングする想定。 */
export const getAutoGenerateGlossaryStatus = (projectId: string, jobId: string) =>
  request<GlossaryJobStatus>(
    `/api/projects/${projectId}/glossary/auto-generate/${jobId}`,
  );

/** start + poll を一括で行うユーティリティ。1.5秒間隔でポーリング、最大15分。 */
export const autoGenerateGlossary = async (
  projectId: string,
  onProgress?: (status: GlossaryJobStatus) => void,
): Promise<GlossaryJobStatus> => {
  const { job_id } = await startAutoGenerateGlossary(projectId);
  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    const s = await getAutoGenerateGlossaryStatus(projectId, job_id);
    onProgress?.(s);
    if (s.state === "done" || s.state === "error") return s;
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error("用語生成がタイムアウトしました (15分)");
};

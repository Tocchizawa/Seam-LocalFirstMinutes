import { useState, useEffect, useMemo, useRef } from "react";
import {
  X, MoonStars, Sun, Monitor, CheckCircle, ArrowsClockwise,
  Play, Pause, MagnifyingGlass, Trash, Check, UsersThree, DownloadSimple,
} from "@phosphor-icons/react";
import { Select } from "../components/Select";
import { useImeSafeEnter } from "../lib/ime";
import {
  getSettings,
  updateSettings,
  listSpeakers,
  renameSpeaker,
  deleteSpeaker,
  mergeSpeakers,
  speakerSampleUrl,
  getDiarizationStatus,
  setHfToken,
  deleteHfToken,
  testDiarization,
  putApiKey,
  deleteApiKey,
  listApiKeys,
  testSummarizeProvider,
  consentProvider,
  getRecommendedProvider,
  getCodexModels,
  getClaudeCodeModels,
  getDefaultSummaryPrompt,
  getWhisperModels,
  downloadWhisperModel,
  deleteWhisperModel,
  PROVIDER_LABELS,
  SUMMARIZE_PROVIDERS,
  CLOUD_PROVIDERS,
  CLI_PROVIDERS,
  type SummarizeProvider,
  type ProviderTestResult,
  type RecommendedProvider,
  type DiarizationStatus,
  type DiarizationTestResult,
  type SpeakerProfile,
  type CliModelOption,
  type WhisperModelInfo,
  type WhisperDownloadStatus,
} from "../lib/api";
import { ask } from "@tauri-apps/plugin-dialog";
import { showToast } from "../lib/toast";
import { getThemeMode, setThemeMode, type ThemeMode } from "../lib/theme";
import { Spinner } from "../components/Spinner";
import { ModelDownloadProgress } from "../components/ModelDownloadProgress";
import { useRecording } from "../lib/recording-context";
import { formatSize } from "../lib/parse-hf-progress";
import {
  checkForAppUpdate,
  closeAppUpdate,
  getCurrentAppVersion,
  installAndRelaunchAppUpdate,
  updateErrorMessage,
  type AppUpdateInfo,
} from "../lib/updater";

interface Props {
  onClose: () => void;
}

type Category = "appearance" | "app" | "transcribe" | "speakers" | "speaker-list" | "ai" | "recording" | "debug";

type PerfMode = "full" | "auto" | "eco";

// モーダルを開き直しても直前のカテゴリを維持する (セッション内のみ)
let lastCategory: Category = "appearance";

function settingsFormSnapshot(data: Record<string, any>): string {
  const whisper = data.whisper || {};
  const perf = whisper.performance || {};
  const speakerMemory = whisper.speaker_memory || {};
  const ai = data.minutes_ai || {};
  const reminder = data.recording?.stop_forget_reminder || {};
  const appUpdate = data.app_update || {};
  const provider = String(ai.provider || "ollama");
  const validProvider = SUMMARIZE_PROVIDERS.includes(provider as SummarizeProvider)
    ? provider
    : "ollama";
  const readNumber = (value: unknown, fallback: number) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };
  const optionalNumber = (value: unknown) => {
    if (!value) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };

  return JSON.stringify({
    wm: String(whisper.model || "medium"),
    perfMode: perf.mode === "full" || perf.mode === "eco" ? perf.mode : "auto",
    perfCpuThreshold: Math.max(30, Math.min(95, Math.round(readNumber(perf.cpu_high_threshold, 75)))),
    perfMemoryThreshold: Math.max(50, Math.min(98, Math.round(readNumber(perf.memory_high_threshold, 85)))),
    perfThrottleRatio: Number(Math.max(0.1, Math.min(3, readNumber(perf.throttle_ratio, 0.5))).toFixed(2)),
    perfMaxThrottleSec: Number(Math.max(0.5, Math.min(10, readNumber(perf.max_throttle_sec, 3))).toFixed(1)),
    perfWorkerNice: Math.max(0, Math.min(19, Math.round(readNumber(perf.worker_nice, 3)))),
    perfMlxMemoryRatio: Number(Math.max(0.2, Math.min(0.8, readNumber(perf.mlx_memory_ratio, 0.4))).toFixed(2)),
    diarizationEnabled: Boolean(speakerMemory.diarization_enabled ?? true),
    speakerMemoryEnabled: Boolean(speakerMemory.enabled ?? true),
    speakerMatchThreshold: Number(readNumber(speakerMemory.match_threshold, 0.82).toFixed(2)),
    speakerMinAudioSec: Number(readNumber(speakerMemory.min_audio_sec, 1).toFixed(1)),
    diarProvider: speakerMemory.diarization_provider === "pyannote" ? "pyannote" : "legacy",
    diarMinSpeakers: optionalNumber(speakerMemory.pyannote_min_speakers),
    diarMaxSpeakers: optionalNumber(speakerMemory.pyannote_max_speakers),
    diarDevice: speakerMemory.pyannote_device === "cpu" || speakerMemory.pyannote_device === "mps"
      ? speakerMemory.pyannote_device
      : "auto",
    lm: String(data.ollama?.context_model || "qwen3:8b"),
    aiProvider: validProvider,
    aiAutoGenerate: Boolean(ai.auto_generate ?? true),
    aiGenerateTitle: Boolean(ai.generate_title ?? true),
    aiAutoDictionaryUpdate: Boolean(ai.auto_dictionary_update ?? ai.auto_correct_dictionary ?? true),
    aiTimeoutSec: Math.max(30, Math.round(readNumber(ai.timeout_sec, 300))),
    aiOllamaModel: String(ai.ollama?.model ?? "qwen3:8b"),
    aiOllamaCtx: Math.max(2048, Math.round(readNumber(ai.ollama?.num_ctx, 8192))),
    aiClaudeModel: String(ai.claude_api?.model ?? "claude-sonnet-4-6"),
    aiOpenAIModel: String(ai.openai?.model ?? "gpt-4o-mini"),
    aiGeminiModel: String(ai.gemini?.model ?? "gemini-2.0-flash"),
    aiClaudeCodeModel: String(ai.claude_code?.model ?? "sonnet"),
    aiCodexModel: String(ai.codex?.model ?? "").trim(),
    aiClaudeCodeLauncher: String(ai.claude_code?.launcher_command ?? "").trim(),
    aiCodexLauncher: String(ai.codex?.launcher_command ?? "").trim(),
    aiCustomPrompt: String(ai.custom_system_prompt ?? ""),
    stopForgetEnabled: Boolean(reminder.enabled ?? true),
    stopForgetSilenceSec: Math.max(10, Math.round(readNumber(reminder.silence_sec, 300))),
    stopForgetLevelThreshold: Number(readNumber(reminder.level_threshold, 0.02).toFixed(3)),
    ll: String(data.logging?.level || "INFO"),
    updateCheckOnStartup: Boolean(appUpdate.auto_install_on_startup ?? false)
      || appUpdate.check_on_startup !== false,
    updateAutoInstallOnStartup: Boolean(appUpdate.auto_install_on_startup ?? false),
    debugEnabled: Boolean(data.debug?.enabled),
  });
}

export function SettingsModal({ onClose }: Props) {
  const { recording } = useRecording();
  const recordingRef = useRef(recording);
  const [closing, setClosing] = useState(false);
  const [settings, setSettings] = useState<Record<string, any> | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [closePromptOpen, setClosePromptOpen] = useState(false);
  const [category, setCategory] = useState<Category>(lastCategory);

  const [wm, setWm] = useState("medium");
  const [diarizationEnabled, setDiarizationEnabled] = useState(true);
  const [speakerMemoryEnabled, setSpeakerMemoryEnabled] = useState(true);
  const [speakerMatchThreshold, setSpeakerMatchThreshold] = useState(0.82);
  const [speakerMinAudioSec, setSpeakerMinAudioSec] = useState(1.0);

  // ─── 文字起こしパフォーマンス ───
  const [perfMode, setPerfMode] = useState<PerfMode>("auto");
  const [perfCpuThreshold, setPerfCpuThreshold] = useState(75);
  const [perfMemoryThreshold, setPerfMemoryThreshold] = useState(85);
  const [perfThrottleRatio, setPerfThrottleRatio] = useState(0.5);
  const [perfMaxThrottleSec, setPerfMaxThrottleSec] = useState(3.0);
  const [perfWorkerNice, setPerfWorkerNice] = useState(3);
  const [perfMlxMemoryRatio, setPerfMlxMemoryRatio] = useState(0.4);

  const [lm, setLm] = useState("qwen3:8b");
  const [ll, setLl] = useState("INFO");

  // ─── 要約 AI 設定 ───
  const [aiProvider, setAiProvider] = useState<SummarizeProvider>("ollama");
  const [aiAutoGenerate, setAiAutoGenerate] = useState(true);
  const [aiGenerateTitle, setAiGenerateTitle] = useState(true);
  const [aiAutoDictionaryUpdate, setAiAutoDictionaryUpdate] = useState(true);
  const [aiTimeoutSec, setAiTimeoutSec] = useState(300);
  const [aiOllamaModel, setAiOllamaModel] = useState("qwen3:8b");
  const [aiOllamaCtx, setAiOllamaCtx] = useState(8192);
  const [aiClaudeModel, setAiClaudeModel] = useState("claude-sonnet-4-6");
  const [aiOpenAIModel, setAiOpenAIModel] = useState("gpt-4o-mini");
  const [aiGeminiModel, setAiGeminiModel] = useState("gemini-2.0-flash");
  const [aiClaudeCodeModel, setAiClaudeCodeModel] = useState("sonnet");
  const [aiCodexModel, setAiCodexModel] = useState("");
  const [aiClaudeCodeLauncher, setAiClaudeCodeLauncher] = useState("");
  const [aiCodexLauncher, setAiCodexLauncher] = useState("");
  const [codexModelChoices, setCodexModelChoices] = useState<CliModelOption[]>([]);
  const [claudeCodeChoices, setClaudeCodeChoices] = useState<CliModelOption[]>([]);
  const [aiConsent, setAiConsent] = useState<Record<string, boolean>>({});
  // ─── 要約 system prompt ───
  const [defaultPrompt, setDefaultPrompt] = useState("");
  const [aiCustomPrompt, setAiCustomPrompt] = useState(""); // 保存済み ("" = デフォルト使用)
  const [aiPromptDraft, setAiPromptDraft] = useState<string | null>(null); // null = 未編集
  const [aiKeysPresent, setAiKeysPresent] = useState<Partial<Record<SummarizeProvider, boolean>>>({});
  const [aiKeyDraft, setAiKeyDraft] = useState<Partial<Record<SummarizeProvider, string>>>({});
  const [aiKeyBusy, setAiKeyBusy] = useState<Partial<Record<SummarizeProvider, boolean>>>({});
  const [aiTestBusy, setAiTestBusy] = useState(false);
  const [aiTestResult, setAiTestResult] = useState<ProviderTestResult | null>(null);
  const [aiRecommended, setAiRecommended] = useState<RecommendedProvider | null>(null);
  const [aiConsentDialog, setAiConsentDialog] = useState<SummarizeProvider | null>(null);
  const [debugEnabled, setDebugEnabled] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>(getThemeMode());
  const [appVersion, setAppVersion] = useState("");
  const [updateCheckOnStartup, setUpdateCheckOnStartup] = useState(true);
  const [updateAutoInstallOnStartup, setUpdateAutoInstallOnStartup] = useState(false);
  const [updateBusy, setUpdateBusy] = useState(false);
  const [updateStatus, setUpdateStatus] = useState("");
  const [availableUpdate, setAvailableUpdate] = useState<AppUpdateInfo | null>(null);
  const [stopForgetEnabled, setStopForgetEnabled] = useState(true);
  const [stopForgetSilenceSec, setStopForgetSilenceSec] = useState(300);
  const [stopForgetLevelThreshold, setStopForgetLevelThreshold] = useState(0.02);

  const [speakersLoading, setSpeakersLoading] = useState(false);
  const [speakers, setSpeakers] = useState<SpeakerProfile[]>([]);
  const [speakersError, setSpeakersError] = useState("");
  const [labelDrafts, setLabelDrafts] = useState<Record<string, string>>({});
  const [renaming, setRenaming] = useState<Record<string, boolean>>({});
  const [selectedSpeakers, setSelectedSpeakers] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [mergeDialogOpen, setMergeDialogOpen] = useState(false);

  // 話者分離プロバイダ
  const [diarProvider, setDiarProvider] = useState<"legacy" | "pyannote">("legacy");
  const [diarMinSpeakers, setDiarMinSpeakers] = useState<string>("");
  const [diarMaxSpeakers, setDiarMaxSpeakers] = useState<string>("");
  const [diarDevice, setDiarDevice] = useState<"auto" | "cpu" | "mps">("auto");
  const [diarStatus, setDiarStatus] = useState<DiarizationStatus | null>(null);
  const [hfTokenDraft, setHfTokenDraft] = useState("");
  const [hfTokenSaving, setHfTokenSaving] = useState(false);
  const [hfTokenError, setHfTokenError] = useState("");
  const [diarTesting, setDiarTesting] = useState(false);
  const [diarTestResult, setDiarTestResult] = useState<DiarizationTestResult | null>(null);
  const [whisperModels, setWhisperModels] = useState<WhisperModelInfo[]>([]);
  const [whisperDownload, setWhisperDownload] = useState<WhisperDownloadStatus | null>(null);
  const [whisperModelBusy, setWhisperModelBusy] = useState<Record<string, boolean>>({});
  const [whisperModelError, setWhisperModelError] = useState("");
  const availableUpdateRef = useRef<AppUpdateInfo | null>(null);
  const speakersLoadedRef = useRef(false);
  const aiAuxLoadedRef = useRef(false);
  const diarStatusLoadedRef = useRef(false);
  const closeRef = useRef<() => void>(() => {});

  const refreshDiarStatus = async () => {
    try {
      setDiarStatus(await getDiarizationStatus());
    } catch {
      setDiarStatus(null);
    }
  };

  const refreshWhisperModels = async () => {
    try {
      const result = await getWhisperModels();
      setWhisperModels(result.models);
      setWhisperDownload(result.download);
    } catch (e) {
      setWhisperModelError(e instanceof Error ? e.message : "Whisperモデルの状態取得に失敗しました");
    }
  };

  const categories = useMemo(
    () => [
      { key: "appearance" as const, label: "外観", group: "基本" },
      { key: "app" as const, label: "アプリ", group: "基本" },
      { key: "recording" as const, label: "録音", group: "音声" },
      { key: "transcribe" as const, label: "文字起こし", group: "音声" },
      { key: "speakers" as const, label: "話者分離", group: "音声" },
      { key: "speaker-list" as const, label: "話者一覧", group: "音声" },
      { key: "ai" as const, label: "要約", group: "AI" },
      { key: "debug" as const, label: "デバッグ", group: "開発" },
    ],
    [],
  );

  const refreshSpeakers = async () => {
    setSpeakersLoading(true);
    setSpeakersError("");
    try {
      const res = await listSpeakers();
      setSpeakers(res.speakers);
      const drafts: Record<string, string> = {};
      res.speakers.forEach((sp) => { drafts[sp.id] = sp.label; });
      setLabelDrafts(drafts);
    } catch (e) {
      setSpeakers([]);
      setSpeakersError(e instanceof Error ? e.message : "話者一覧の取得に失敗しました");
    } finally {
      setSpeakersLoading(false);
    }
  };

  useEffect(() => {
    void getCurrentAppVersion().then(setAppVersion).catch(() => setAppVersion(""));
  }, []);

  useEffect(() => {
    recordingRef.current = recording;
  }, [recording]);

  useEffect(() => {
    availableUpdateRef.current = availableUpdate;
  }, [availableUpdate]);

  useEffect(() => () => {
    void closeAppUpdate(availableUpdateRef.current);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getSettings().then((s) => {
      if (cancelled) return;
      setSettings(s);
      setWm((s.whisper as any)?.model || "medium");
      setLm((s.ollama as any)?.context_model || "qwen3:8b");
      setLl((s.logging as any)?.level || "INFO");
      setDebugEnabled(Boolean((s.debug as any)?.enabled));
      const appUpdate = (s.app_update as any) || {};
      const autoInstall = Boolean(appUpdate.auto_install_on_startup ?? false);
      setUpdateAutoInstallOnStartup(autoInstall);
      setUpdateCheckOnStartup(autoInstall || appUpdate.check_on_startup !== false);

      const perf = (s.whisper as any)?.performance || {};
      const mode = String(perf.mode ?? "auto");
      setPerfMode(mode === "full" || mode === "eco" ? mode : "auto");
      setPerfCpuThreshold(Number(perf.cpu_high_threshold ?? 75));
      setPerfMemoryThreshold(Number(perf.memory_high_threshold ?? 85));
      setPerfThrottleRatio(Number(perf.throttle_ratio ?? 0.5));
      setPerfMaxThrottleSec(Number(perf.max_throttle_sec ?? 3.0));
      setPerfWorkerNice(Number(perf.worker_nice ?? 3));
      setPerfMlxMemoryRatio(Number(perf.mlx_memory_ratio ?? 0.4));

      const speakerMemory = (s.whisper as any)?.speaker_memory || {};
      setDiarizationEnabled(Boolean(speakerMemory.diarization_enabled ?? true));
      setSpeakerMemoryEnabled(Boolean(speakerMemory.enabled ?? true));
      setSpeakerMatchThreshold(Number(speakerMemory.match_threshold ?? 0.82));
      setSpeakerMinAudioSec(Number(speakerMemory.min_audio_sec ?? 1.0));
      setDiarProvider(speakerMemory.diarization_provider === "pyannote" ? "pyannote" : "legacy");
      setDiarMinSpeakers(speakerMemory.pyannote_min_speakers ? String(speakerMemory.pyannote_min_speakers) : "");
      setDiarMaxSpeakers(speakerMemory.pyannote_max_speakers ? String(speakerMemory.pyannote_max_speakers) : "");
      const dev = String(speakerMemory.pyannote_device ?? "auto");
      setDiarDevice(dev === "cpu" || dev === "mps" ? dev : "auto");

      const reminder = (s.recording as any)?.stop_forget_reminder || {};
      setStopForgetEnabled(Boolean(reminder.enabled ?? true));
      setStopForgetSilenceSec(Number(reminder.silence_sec ?? 300));
      setStopForgetLevelThreshold(Number(reminder.level_threshold ?? 0.02));

      // ─── 要約 AI 設定 ロード ───
      const ai = (s.minutes_ai as any) || {};
      const validProviders: SummarizeProvider[] = [
        "ollama", "claude_api", "openai", "gemini", "claude_code", "codex",
      ];
      const provider = String(ai.provider || "ollama") as SummarizeProvider;
      setAiProvider(validProviders.includes(provider) ? provider : "ollama");
      setAiAutoGenerate(Boolean(ai.auto_generate ?? true));
      setAiGenerateTitle(Boolean(ai.generate_title ?? true));
      // 旧キー auto_correct_dictionary が残っていれば fallback (後方互換)
      setAiAutoDictionaryUpdate(
        Boolean(ai.auto_dictionary_update ?? ai.auto_correct_dictionary ?? true),
      );
      setAiTimeoutSec(Number(ai.timeout_sec ?? 300));
      setAiOllamaModel(String(ai.ollama?.model ?? "qwen3:8b"));
      setAiOllamaCtx(Number(ai.ollama?.num_ctx ?? 8192));
      setAiClaudeModel(String(ai.claude_api?.model ?? "claude-sonnet-4-6"));
      setAiOpenAIModel(String(ai.openai?.model ?? "gpt-4o-mini"));
      setAiGeminiModel(String(ai.gemini?.model ?? "gemini-2.0-flash"));
      setAiClaudeCodeModel(String(ai.claude_code?.model ?? "sonnet"));
      setAiCodexModel(String(ai.codex?.model ?? ""));
      setAiClaudeCodeLauncher(String(ai.claude_code?.launcher_command ?? ""));
      setAiCodexLauncher(String(ai.codex?.launcher_command ?? ""));
      setAiCustomPrompt(String(ai.custom_system_prompt ?? ""));
      setAiConsent(ai.consent || {});

      setSettingsLoading(false);
    }).catch((e) => {
      if (cancelled) return;
      setSettings({});
      setSettingsLoading(false);
      setSpeakersError(e instanceof Error ? e.message : "設定の取得に失敗しました");
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (category !== "speaker-list" || speakersLoadedRef.current) return;
    speakersLoadedRef.current = true;
    void refreshSpeakers();
  }, [category]);

  useEffect(() => {
    if (category !== "speakers" || diarStatusLoadedRef.current) return;
    diarStatusLoadedRef.current = true;
    void refreshDiarStatus();
  }, [category]);

  useEffect(() => {
    if (category !== "transcribe") return () => {};
    let cancelled = false;
    const refresh = async () => {
      try {
        const result = await getWhisperModels();
        if (cancelled) return;
        setWhisperModels(result.models);
        setWhisperDownload(result.download);
        setWhisperModelError("");
      } catch (e) {
        if (!cancelled) {
          setWhisperModelError(e instanceof Error ? e.message : "Whisperモデルの状態取得に失敗しました");
        }
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 700);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [category]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") closeRef.current(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // 開いていたカテゴリを次回オープン時に復元する
  useEffect(() => {
    lastCategory = category;
  }, [category]);

  // Cmd+S で保存 (macOS 標準の操作感)
  const saveRef = useRef<() => void>(() => {});
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // 要約タブを開いた時だけ補助情報を取得する。設定モーダルの初期表示を塞がない。
  useEffect(() => {
    let cancelled = false;
    let completed = false;
    if (category !== "ai" || aiAuxLoadedRef.current) return () => {};
    aiAuxLoadedRef.current = true;
    Promise.all([
      getCodexModels().catch(() => []),
      getClaudeCodeModels().catch(() => []),
      getDefaultSummaryPrompt().catch(() => null),
      listApiKeys().catch(() => null),
      getRecommendedProvider().catch(() => null),
    ]).then(([codexModels, claudeModels, prompt, keys, recommended]) => {
      completed = true;
      if (cancelled) return;
      setCodexModelChoices(codexModels);
      setClaudeCodeChoices(claudeModels);
      if (prompt) setDefaultPrompt(prompt.prompt);
      if (keys) setAiKeysPresent(keys.providers || {});
      if (recommended) setAiRecommended(recommended);
    });
    return () => {
      cancelled = true;
      if (!completed) aiAuxLoadedRef.current = false;
    };
  }, [category]);

  const finishClose = () => {
    if (closing) return;
    setClosing(true);
    setTimeout(onClose, 180);
  };

  // textarea に表示する実効プロンプト (編集中 > 保存済みカスタム > デフォルト)
  const promptValue = aiPromptDraft ?? (aiCustomPrompt || defaultPrompt);
  const promptIsCustom =
    promptValue.trim() !== ""
    && (!defaultPrompt || promptValue.trim() !== defaultPrompt.trim());

  const currentSettingsSnapshot = useMemo(() => settingsFormSnapshot({
    whisper: {
      model: wm,
      performance: {
        mode: perfMode,
        cpu_high_threshold: perfCpuThreshold,
        memory_high_threshold: perfMemoryThreshold,
        throttle_ratio: perfThrottleRatio,
        max_throttle_sec: perfMaxThrottleSec,
        worker_nice: perfWorkerNice,
        mlx_memory_ratio: perfMlxMemoryRatio,
      },
      speaker_memory: {
        diarization_enabled: diarizationEnabled,
        enabled: speakerMemoryEnabled,
        match_threshold: speakerMatchThreshold,
        min_audio_sec: speakerMinAudioSec,
        diarization_provider: diarProvider,
        pyannote_device: diarDevice,
        pyannote_min_speakers: diarMinSpeakers || null,
        pyannote_max_speakers: diarMaxSpeakers || null,
      },
    },
    ollama: { context_model: lm },
    minutes_ai: {
      provider: aiProvider,
      auto_generate: aiAutoGenerate,
      generate_title: aiGenerateTitle,
      auto_dictionary_update: aiAutoDictionaryUpdate,
      custom_system_prompt: promptIsCustom ? promptValue : "",
      timeout_sec: aiTimeoutSec,
      ollama: { model: aiOllamaModel, num_ctx: aiOllamaCtx },
      claude_api: { model: aiClaudeModel },
      openai: { model: aiOpenAIModel },
      gemini: { model: aiGeminiModel },
      claude_code: { model: aiClaudeCodeModel, launcher_command: aiClaudeCodeLauncher },
      codex: { model: aiCodexModel, launcher_command: aiCodexLauncher },
    },
    recording: {
      stop_forget_reminder: {
        enabled: stopForgetEnabled,
        silence_sec: stopForgetSilenceSec,
        level_threshold: stopForgetLevelThreshold,
      },
    },
    logging: { level: ll },
    app_update: {
      check_on_startup: updateCheckOnStartup,
      auto_install_on_startup: updateAutoInstallOnStartup,
    },
    debug: { enabled: debugEnabled },
  }), [
    wm, perfMode, perfCpuThreshold, perfMemoryThreshold, perfThrottleRatio,
    perfMaxThrottleSec, perfWorkerNice, perfMlxMemoryRatio, diarizationEnabled,
    speakerMemoryEnabled, speakerMatchThreshold, speakerMinAudioSec, diarProvider,
    diarDevice, diarMinSpeakers, diarMaxSpeakers, lm, aiProvider, aiAutoGenerate,
    aiGenerateTitle, aiAutoDictionaryUpdate, promptIsCustom, promptValue, aiTimeoutSec,
    aiOllamaModel, aiOllamaCtx, aiClaudeModel, aiOpenAIModel, aiGeminiModel,
    aiClaudeCodeModel, aiCodexModel, aiClaudeCodeLauncher, aiCodexLauncher,
    stopForgetEnabled, stopForgetSilenceSec, stopForgetLevelThreshold, ll,
    updateCheckOnStartup, updateAutoInstallOnStartup, debugEnabled,
  ]);
  const savedSettingsSnapshot = useMemo(
    () => (settings ? settingsFormSnapshot(settings) : ""),
    [settings],
  );
  const isDirty = Boolean(settings) && currentSettingsSnapshot !== savedSettingsSnapshot;

  const handleClose = () => {
    if (closing) return;
    if (closePromptOpen) {
      setClosePromptOpen(false);
      return;
    }
    if (isDirty) {
      setClosePromptOpen(true);
      return;
    }
    finishClose();
  };
  closeRef.current = handleClose;

  const save = async (): Promise<boolean> => {
    setSaving(true);
    setSaved(false);
    // デフォルトと同一 (または空) なら "" を保存 = 既定プロンプト使用
    const customPromptToSave = promptIsCustom ? promptValue : "";
    try {
      const u = await updateSettings({
        whisper: {
          model: wm,
          performance: {
            mode: perfMode,
            cpu_high_threshold: Math.max(30, Math.min(95, Math.round(perfCpuThreshold))),
            memory_high_threshold: Math.max(50, Math.min(98, Math.round(perfMemoryThreshold))),
            throttle_ratio: Number(Math.max(0.1, Math.min(3, perfThrottleRatio)).toFixed(2)),
            max_throttle_sec: Number(Math.max(0.5, Math.min(10, perfMaxThrottleSec)).toFixed(1)),
            worker_nice: Math.max(0, Math.min(19, Math.round(perfWorkerNice))),
            mlx_memory_ratio: Number(Math.max(0.2, Math.min(0.8, perfMlxMemoryRatio)).toFixed(2)),
          },
          speaker_memory: {
            diarization_enabled: diarizationEnabled,
            enabled: speakerMemoryEnabled,
            match_threshold: Number(speakerMatchThreshold.toFixed(2)),
            min_audio_sec: Number(speakerMinAudioSec.toFixed(1)),
            diarization_provider: diarProvider,
            pyannote_device: diarDevice,
            pyannote_min_speakers: diarMinSpeakers ? Number(diarMinSpeakers) : null,
            pyannote_max_speakers: diarMaxSpeakers ? Number(diarMaxSpeakers) : null,
          },
        },
        ollama: { context_model: lm },
        minutes_ai: {
          provider: aiProvider,
          auto_generate: aiAutoGenerate,
          generate_title: aiGenerateTitle,
          auto_dictionary_update: aiAutoDictionaryUpdate,
          custom_system_prompt: customPromptToSave,
          timeout_sec: Math.max(30, Math.round(aiTimeoutSec)),
          ollama: {
            model: aiOllamaModel,
            num_ctx: Math.max(2048, Math.round(aiOllamaCtx)),
          },
          claude_api: { model: aiClaudeModel },
          openai: { model: aiOpenAIModel },
          gemini: { model: aiGeminiModel },
          claude_code: {
            model: aiClaudeCodeModel,
            launcher_command: aiClaudeCodeLauncher.trim(),
          },
          codex: {
            model: aiCodexModel.trim(),
            launcher_command: aiCodexLauncher.trim(),
          },
        },
        recording: {
          stop_forget_reminder: {
            enabled: stopForgetEnabled,
            silence_sec: Math.max(10, Math.round(stopForgetSilenceSec)),
            level_threshold: Number(stopForgetLevelThreshold.toFixed(3)),
          },
        },
        logging: { level: ll },
        app_update: {
          check_on_startup: updateCheckOnStartup || updateAutoInstallOnStartup,
          auto_install_on_startup: updateAutoInstallOnStartup,
        },
        debug: { enabled: debugEnabled },
      });
      setSettings(u);
      setAiCustomPrompt(customPromptToSave);
      window.dispatchEvent(new CustomEvent("settings-updated", { detail: u }));
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      return true;
    } catch (e) {
      showToast({
        kind: "err",
        text: `設定の保存に失敗: ${e instanceof Error ? e.message : "不明なエラー"}`,
      });
      return false;
    } finally {
      setSaving(false);
    }
  };
  saveRef.current = () => { if (!saving && isDirty) void save(); };

  const pickTheme = (m: ThemeMode) => {
    setTheme(m);
    setThemeMode(m);
  };

  const setAutoInstallOnStartup = (enabled: boolean) => {
    setUpdateAutoInstallOnStartup(enabled);
    if (enabled) setUpdateCheckOnStartup(true);
  };

  const handleDownloadWhisperModel = async (model: WhisperModelInfo) => {
    if (recordingRef.current) {
      showToast({ kind: "info", text: "録音中はモデルをダウンロードできません" });
      return;
    }
    setWhisperModelBusy((prev) => ({ ...prev, [model.name]: true }));
    setWhisperModelError("");
    try {
      const result = await downloadWhisperModel(model.name);
      setWhisperDownload(result.download);
      await refreshWhisperModels();
    } catch (e) {
      setWhisperModelError(e instanceof Error ? e.message : "モデルのダウンロード開始に失敗しました");
    } finally {
      setWhisperModelBusy((prev) => ({ ...prev, [model.name]: false }));
    }
  };

  const handleDeleteWhisperModel = async (model: WhisperModelInfo) => {
    if (recordingRef.current) {
      showToast({ kind: "info", text: "録音中はモデルを削除できません" });
      return;
    }
    const ok = await ask(
      `${model.label} を削除します。次回使用時に再ダウンロードが必要になります。続行しますか?`,
      { title: "Whisperモデルの削除", kind: "warning", okLabel: "削除", cancelLabel: "キャンセル" },
    );
    if (!ok) return;
    setWhisperModelBusy((prev) => ({ ...prev, [model.name]: true }));
    setWhisperModelError("");
    try {
      await deleteWhisperModel(model.name);
      await refreshWhisperModels();
    } catch (e) {
      setWhisperModelError(e instanceof Error ? e.message : "モデルの削除に失敗しました");
    } finally {
      setWhisperModelBusy((prev) => ({ ...prev, [model.name]: false }));
    }
  };

  const installUpdate = async (info: AppUpdateInfo) => {
    if (recordingRef.current) {
      const message = "録音中はアップデートできません。録音終了後に実行してください";
      setUpdateStatus(message);
      showToast({ kind: "info", text: message, ttl: 7000 });
      return;
    }

    setUpdateBusy(true);
    setUpdateStatus("アップデートを開始しています...");
    try {
      await installAndRelaunchAppUpdate(info, (progress) => {
        setUpdateStatus(progress.message);
      }, {
        shouldContinue: () => !recordingRef.current,
        abortMessage: "録音中のためアップデートを中断しました",
      });
    } catch (e) {
      const message = updateErrorMessage(e);
      const interrupted = message.includes("中断");
      setUpdateStatus(interrupted ? message : `アップデート失敗: ${message}`);
      showToast({
        kind: interrupted ? "info" : "err",
        text: interrupted ? message : `アップデート失敗: ${message}`,
        ttl: 7000,
      });
      setUpdateBusy(false);
    }
  };

  const handleCheckUpdate = async () => {
    if (updateBusy) return;
    if (availableUpdate) {
      await installUpdate(availableUpdate);
      return;
    }

    setUpdateBusy(true);
    setUpdateStatus("アップデートを確認中...");
    try {
      await closeAppUpdate(availableUpdateRef.current);
      setAvailableUpdate(null);
      const result = await checkForAppUpdate({ timeoutMs: 15_000 });
      if (!result) {
        setUpdateStatus("最新版です");
        showToast({ kind: "ok", text: "Seam は最新版です" });
        return;
      }

      setAvailableUpdate(result);
      setUpdateStatus(`Seam ${result.version} を利用できます`);
      if (recordingRef.current) {
        showToast({
          kind: "info",
          text: `Seam ${result.version} を利用できます。録音終了後にインストールしてください`,
          ttl: 7000,
        });
        return;
      }
      const ok = await ask(
        `Seam ${result.version} にアップデートします。インストール後にアプリを再起動します。`,
        {
          title: "アップデート",
          kind: "info",
          okLabel: "アップデート",
          cancelLabel: "あとで",
        },
      );
      if (ok) {
        await installUpdate(result);
      }
    } catch (e) {
      const message = updateErrorMessage(e);
      setUpdateStatus(`確認失敗: ${message}`);
      showToast({ kind: "err", text: `確認失敗: ${message}`, ttl: 7000 });
    } finally {
      setUpdateBusy(false);
    }
  };

  // ─── 要約 AI 関連ハンドラ ───
  const handleSelectAiProvider = (p: SummarizeProvider) => {
    // cloud provider で同意未取得なら同意モーダル提示
    const isCloud = (CLOUD_PROVIDERS as readonly string[]).includes(p);
    if (isCloud && !aiConsent[p]) {
      setAiConsentDialog(p);
      return;
    }
    setAiProvider(p);
  };

  const acceptAiConsent = async (p: SummarizeProvider) => {
    try {
      await consentProvider(p);
      setAiConsent((prev) => ({ ...prev, [p]: true }));
      setAiProvider(p);
      setAiConsentDialog(null);
    } catch (e) {
      showToast({
        kind: "err",
        text: `同意の保存に失敗: ${e instanceof Error ? e.message : ""}`,
      });
    }
  };

  const handleSaveApiKey = async (p: SummarizeProvider) => {
    const token = (aiKeyDraft[p] || "").trim();
    if (!token) return;
    setAiKeyBusy((prev) => ({ ...prev, [p]: true }));
    try {
      await putApiKey(p, token);
      setAiKeysPresent((prev) => ({ ...prev, [p]: true }));
      setAiKeyDraft((prev) => ({ ...prev, [p]: "" }));
      showToast({ kind: "ok", text: `${PROVIDER_LABELS[p]} のAPIキーを保存しました` });
    } catch (e) {
      showToast({
        kind: "err",
        text: `APIキー保存失敗: ${e instanceof Error ? e.message : ""}`,
      });
    } finally {
      setAiKeyBusy((prev) => ({ ...prev, [p]: false }));
    }
  };

  const handleDeleteApiKey = async (p: SummarizeProvider) => {
    setAiKeyBusy((prev) => ({ ...prev, [p]: true }));
    try {
      await deleteApiKey(p);
      setAiKeysPresent((prev) => ({ ...prev, [p]: false }));
      showToast({ kind: "ok", text: `${PROVIDER_LABELS[p]} のAPIキーを削除しました` });
    } catch (e) {
      showToast({
        kind: "err",
        text: `削除失敗: ${e instanceof Error ? e.message : ""}`,
      });
    } finally {
      setAiKeyBusy((prev) => ({ ...prev, [p]: false }));
    }
  };

  const handleTestProvider = async () => {
    setAiTestBusy(true);
    setAiTestResult(null);
    try {
      const r = await testSummarizeProvider(aiProvider);
      setAiTestResult(r);
    } catch (e) {
      setAiTestResult({
        ok: false,
        code: "REQUEST_FAILED",
        message: e instanceof Error ? e.message : "テスト失敗",
      });
    } finally {
      setAiTestBusy(false);
    }
  };

  const toggleSelected = (id: string) => {
    setSelectedSpeakers((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const clearSelection = () => setSelectedSpeakers(new Set());

  const doDeleteSelected = async () => {
    if (selectedSpeakers.size === 0) return;
    const ids = Array.from(selectedSpeakers);
    const ok = await ask(
      `${ids.length} 件の話者プロファイルを削除します。\n削除した話者の発言は「話者?」になります。続行しますか?`,
      { title: "話者の削除", kind: "warning", okLabel: "削除", cancelLabel: "キャンセル" },
    );
    if (!ok) return;
    setBulkBusy(true);
    let totalAffected = 0;
    try {
      for (const id of ids) {
        try {
          const r = await deleteSpeaker(id);
          totalAffected += r.affected_segments || 0;
        } catch (e) {
          showToast({ kind: "err", text: `削除失敗 (${id}): ${e instanceof Error ? e.message : ""}` });
        }
      }
      showToast({ kind: "ok", text: `${ids.length} 件削除 (セグメント ${totalAffected} 件を更新)` });
      clearSelection();
      await refreshSpeakers();
    } finally {
      setBulkBusy(false);
    }
  };

  const doMergeConfirmed = async (primaryId: string) => {
    if (selectedSpeakers.size < 2) return;
    const sources = Array.from(selectedSpeakers).filter((id) => id !== primaryId);
    if (sources.length === 0) return;
    const primaryLabel = speakers.find((s) => s.id === primaryId)?.label || primaryId;
    setBulkBusy(true);
    try {
      const r = await mergeSpeakers(primaryId, sources);
      showToast({
        kind: "ok",
        text: `「${primaryLabel}」に統合 (セグメント ${r.affected_segments} 件を更新)`,
      });
      clearSelection();
      setMergeDialogOpen(false);
      await refreshSpeakers();
    } catch (e) {
      showToast({ kind: "err", text: `統合失敗: ${e instanceof Error ? e.message : ""}` });
    } finally {
      setBulkBusy(false);
    }
  };

  const doRenameSpeaker = async (speakerId: string) => {
    const next = (labelDrafts[speakerId] || "").trim();
    if (!next) return;
    setRenaming((prev) => ({ ...prev, [speakerId]: true }));
    setSpeakersError("");
    try {
      await renameSpeaker(speakerId, next);
      await refreshSpeakers();
    } catch (e) {
      setSpeakersError(e instanceof Error ? e.message : "話者名の更新に失敗しました");
    } finally {
      setRenaming((prev) => ({ ...prev, [speakerId]: false }));
    }
  };

  return (
    <div className={`fixed inset-0 flex items-center justify-center z-50 ${closing ? "anim-modal-overlay-out" : "anim-modal-overlay-in"}`}>
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={handleClose}
        style={{ background: "rgba(0,0,0,0.4)" }}
      />

      <div className={`dialog-shell relative w-[820px] max-w-[95vw] h-[640px] max-h-[86vh] flex flex-col overflow-hidden ${closing ? "anim-modal-out" : "anim-modal-in"}`}>
        <header className="flex items-center justify-between p-4 px-5 border-b border-(--border) shrink-0">
          <h2 className="text-[14px] font-semibold text-(--t1)">設定</h2>
          <button onClick={handleClose} className="icon-btn" title="閉じる">
            <X size={14} weight="bold" />
          </button>
        </header>

        <div className="flex flex-1 min-h-0 overflow-hidden">
          <aside className="w-[160px] border-r border-(--border) p-3 bg-(--surface) overflow-y-auto shrink-0">
            <div className="flex flex-col gap-3">
              {Array.from(new Set(categories.map((c) => c.group))).map((group) => (
                <div key={group}>
                  <p className="settings-nav-group-title">{group}</p>
                  <div className="flex flex-col gap-1">
                    {categories.filter((c) => c.group === group).map((c) => (
                      <button
                        key={c.key}
                        onClick={() => setCategory(c.key)}
                        className={`settings-nav-item ${category === c.key ? "active" : ""}`}
                      >
                        {c.label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </aside>

          <main className="flex-1 min-w-0 flex flex-col overflow-hidden">
            <div className="flex-1 overflow-y-auto px-7 py-6">
            {!settings ? (
              <div className="flex h-full min-h-[240px] flex-col items-center justify-center gap-2">
                <Spinner size={20} />
                <span className="text-[11px] text-(--t3)">
                  {settingsLoading ? "設定を読み込んでいます..." : "設定を表示できませんでした"}
                </span>
              </div>
            ) : (
              <div className="anim-fade-in">
                {category === "appearance" && (
                  <SGroup title="外観">
                    <SRow label="テーマ">
                      <ThemePicker value={theme} onChange={pickTheme} />
                    </SRow>
                  </SGroup>
                )}

                {category === "app" && (
                  <>
                    <SGroup title="アプリ">
                      <SRow label="現在のバージョン">
                        <span className="text-[12px] text-(--t2)">{appVersion || "取得中..."}</span>
                      </SRow>
                      <SRow label="起動時に更新を確認">
                        <SToggle
                          value={updateCheckOnStartup}
                          onChange={(v) => {
                            setUpdateCheckOnStartup(v);
                            if (!v) setUpdateAutoInstallOnStartup(false);
                          }}
                        />
                      </SRow>
                      <SRow
                        label="起動時に自動アップデート"
                        hint="ONの場合、更新を見つけたら自動でインストールして再起動します"
                      >
                        <SToggle
                          value={updateAutoInstallOnStartup}
                          onChange={setAutoInstallOnStartup}
                        />
                      </SRow>
                      <SRow
                        label="アップデート"
                        hint={updateStatus || "GitHub Releases の更新フィードを確認します"}
                      >
                        <button
                          onClick={handleCheckUpdate}
                          disabled={updateBusy}
                          className="btn h-7 px-2.5 text-[11px]"
                        >
                          {updateBusy ? <Spinner size={12} /> : <ArrowsClockwise size={12} />}
                          {updateBusy
                            ? "処理中..."
                            : availableUpdate
                              ? "インストール"
                              : "確認"}
                        </button>
                      </SRow>
                    </SGroup>
                  </>
                )}

                {category === "transcribe" && (
                  <>
                    <SGroup
                      title="Whisper モデル"
                      hint="行を選ぶと録音に使うモデルになります。未取得のモデルは録音開始時または右側のボタンからダウンロードします。"
                    >
                      {whisperModelError && (
                        <p className="mb-1.5 text-[11px] text-(--danger)">{whisperModelError}</p>
                      )}
                      {whisperModels.length === 0 ? (
                        <div className="flex items-center gap-2 py-2 text-[11px] text-(--t3)">
                          <Spinner size={12} />
                          モデル一覧を確認しています...
                        </div>
                      ) : (
                        <div className="flex flex-col gap-1">
                          {whisperModels.map((model) => {
                            const busy = Boolean(whisperModelBusy[model.name]);
                            const isCurrentDownload = whisperDownload?.model === model.name
                              && whisperDownload.state === "downloading";
                            const anotherDownload = whisperDownload?.state === "downloading"
                              && !isCurrentDownload;
                            const stateLabel = model.state === "loaded"
                              ? "メモリ上にロード済み"
                              : model.state === "downloaded"
                                ? "ダウンロード済み"
                                : model.state === "downloading"
                                  ? "ダウンロード中"
                                  : model.state === "error"
                                    ? "ダウンロード失敗"
                                    : "未ダウンロード";
                            return (
                              <div
                                key={model.name}
                                className={`overflow-hidden rounded-md border ${model.name === wm
                                  ? "border-(--accent) bg-(--accent-soft)"
                                  : "border-(--border) bg-(--surface-1)"}`}
                              >
                                <div className="flex items-center">
                                  <button
                                    type="button"
                                    onClick={() => setWm(model.name)}
                                    aria-pressed={model.name === wm}
                                    className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-1.5 text-left cursor-pointer"
                                  >
                                    <span
                                      className={`h-2 w-2 shrink-0 rounded-full border ${model.name === wm
                                        ? "border-(--accent) bg-(--accent)"
                                        : "border-(--t4)"}`}
                                      aria-hidden="true"
                                    />
                                    <span className="min-w-0 flex-1">
                                      <span className="flex items-center gap-1.5">
                                        <span className="truncate text-[11px] text-(--t1)">{model.label}</span>
                                        {model.name === wm && (
                                          <span className="shrink-0 text-[9px] text-(--accent)">選択中</span>
                                        )}
                                      </span>
                                      <span className="mt-0.5 block truncate text-[9px] text-(--t3)">
                                        {stateLabel}
                                        {model.size_bytes > 0
                                          ? ` · ${formatSize(model.size_bytes)}`
                                          : ""}
                                      </span>
                                    </span>
                                  </button>
                                  {model.downloaded ? (
                                    <button
                                      type="button"
                                      className="btn mr-1.5 h-6 shrink-0 px-2 text-[10px]"
                                      onClick={() => void handleDeleteWhisperModel(model)}
                                      disabled={busy || recording || anotherDownload || model.state === "downloading"}
                                      title="モデルを削除"
                                    >
                                      {busy ? <Spinner size={11} /> : <Trash size={11} />}
                                      削除
                                    </button>
                                  ) : (
                                    <button
                                      type="button"
                                      className="btn mr-1.5 h-6 shrink-0 px-2 text-[10px]"
                                      onClick={() => void handleDownloadWhisperModel(model)}
                                      disabled={busy || recording || anotherDownload || model.state === "downloading"}
                                    >
                                      {busy || model.state === "downloading"
                                        ? <Spinner size={11} />
                                        : <DownloadSimple size={11} />}
                                      {model.state === "error" ? "再試行" : "DL"}
                                    </button>
                                  )}
                                </div>
                                {isCurrentDownload && (
                                  <div className="px-2.5 pb-1.5">
                                    <ModelDownloadProgress
                                      status={whisperDownload}
                                      modelLabel={model.label}
                                      compact
                                    />
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </SGroup>

                    <SGroup
                      title="パフォーマンス"
                      hint="文字起こし中に Mac が重くなる場合の調整。抑制するほど文字起こしの反映は遅れますが、他のアプリの動作が軽くなります"
                    >
                      <SRadioList
                        value={perfMode}
                        onChange={setPerfMode}
                        options={[
                          {
                            key: "full",
                            label: "全力",
                            hint: "常に最速で処理。負荷は最大。",
                          },
                          {
                            key: "auto",
                            label: "自動 (推奨)",
                            hint: "CPU またはメモリ使用率が高い間だけ処理を抑制して他アプリを優先。",
                          },
                          {
                            key: "eco",
                            label: "省電力",
                            hint: "常に処理を抑えめにする。バッテリー駆動や低スペック環境向け。",
                          },
                        ]}
                      />
                      {perfMode !== "full" && (
                        <>
                          {perfMode === "auto" && (
                            <SRow
                              label="抑制を開始する CPU 使用率 (%)"
                              hint="30〜95。これを超えている間だけ処理を抑制"
                            >
                              <input
                                type="number"
                                min={30}
                                max={95}
                                step={5}
                                value={perfCpuThreshold}
                                onChange={(e) => setPerfCpuThreshold(Number(e.target.value || 75))}
                                className="input s-control"
                              />
                            </SRow>
                          )}
                          {perfMode === "auto" && (
                            <SRow
                              label="抑制を開始するメモリ使用率 (%)"
                              hint="50〜98。システムメモリが逼迫したときも処理を抑制"
                            >
                              <input
                                type="number"
                                min={50}
                                max={98}
                                step={5}
                                value={perfMemoryThreshold}
                                onChange={(e) => setPerfMemoryThreshold(Number(e.target.value || 85))}
                                className="input s-control"
                              />
                            </SRow>
                          )}
                          <SRow
                            label="抑制の強さ"
                            hint="0.1〜3.0。処理時間 × この係数だけ休止 (大きいほど軽く・遅く)"
                          >
                            <input
                              type="number"
                              min={0.1}
                              max={3}
                              step={0.1}
                              value={perfThrottleRatio}
                              onChange={(e) => setPerfThrottleRatio(Number(e.target.value || 0.5))}
                              className="input s-control"
                            />
                          </SRow>
                          <SRow
                            label="休止の上限 (秒)"
                            hint="0.5〜10。1チャンクあたりの最大休止時間"
                          >
                            <input
                              type="number"
                              min={0.5}
                              max={10}
                              step={0.5}
                              value={perfMaxThrottleSec}
                              onChange={(e) => setPerfMaxThrottleSec(Number(e.target.value || 3))}
                              className="input s-control"
                            />
                          </SRow>
                        </>
                      )}
                      <SRow
                        label="Seam プロセスの優先度"
                        hint="0〜19。大きいほど他アプリを優先。変更は次の文字起こしから適用"
                      >
                        <input
                          type="number"
                          min={0}
                          max={19}
                          step={1}
                          value={perfWorkerNice}
                          onChange={(e) => setPerfWorkerNice(Number(e.target.value || 3))}
                          className="input s-control"
                        />
                      </SRow>
                      <SRow
                        label="Whisper Metal メモリ目安 (搭載 RAM 比)"
                        hint="0.2〜0.8。relaxed 設定のためハード上限ではありません。反映はアプリ再起動後"
                      >
                        <input
                          type="number"
                          min={0.2}
                          max={0.8}
                          step={0.05}
                          value={perfMlxMemoryRatio}
                          onChange={(e) => setPerfMlxMemoryRatio(Number(e.target.value || 0.4))}
                          className="input s-control"
                        />
                      </SRow>
                    </SGroup>
                  </>
                )}

                {category === "speakers" && (
                  <>
                    <SGroup
                      title="話者分離"
                      hint="OFF にすると話者ラベルを一切付与しません。文字起こしの負荷が下がり、処理も速くなります"
                    >
                      <SRow label="話者分離を有効にする">
                        <SToggle value={diarizationEnabled} onChange={setDiarizationEnabled} />
                      </SRow>
                    </SGroup>

                    {diarizationEnabled && (<>
                    <SGroup title="話者分離エンジン" hint="録音終了後にどのアルゴリズムで話者を判定するか">
                      <SRadioList
                        value={diarProvider}
                        onChange={setDiarProvider}
                        options={[
                          {
                            key: "legacy",
                            label: "高速モード",
                            hint: "端末内の軽量アルゴリズム。録音中もリアルタイム判定。精度は控えめ。",
                          },
                          {
                            key: "pyannote",
                            label: "高精度モード (pyannote.audio)",
                            hint: "録音終了後にニューラルモデルで再ラベリング。録音中はラベル無し。",
                          },
                        ]}
                      />
                    </SGroup>

                    {diarProvider === "pyannote" && (
                      <SGroup title="pyannote 設定">
                        <SRow
                          label="HuggingFace トークン"
                          hint={
                            diarStatus?.has_hf_token
                              ? "保存済み"
                              : "未設定 — HF で利用規約承認 + トークン発行が必要"
                          }
                        >
                          <div className="flex items-center gap-1.5 w-full">
                            <input
                              type="password"
                              value={hfTokenDraft}
                              onChange={(e) => setHfTokenDraft(e.target.value)}
                              placeholder={diarStatus?.has_hf_token ? "再入力で上書き" : "hf_xxx..."}
                              className="input s-control"
                              autoComplete="off"
                              spellCheck={false}
                            />
                            <button
                              onClick={async () => {
                                if (!hfTokenDraft.trim()) return;
                                setHfTokenSaving(true);
                                setHfTokenError("");
                                try {
                                  await setHfToken(hfTokenDraft.trim());
                                  setHfTokenDraft("");
                                  await refreshDiarStatus();
                                } catch (e) {
                                  setHfTokenError(e instanceof Error ? e.message : "保存に失敗");
                                } finally {
                                  setHfTokenSaving(false);
                                }
                              }}
                              disabled={hfTokenSaving || !hfTokenDraft.trim()}
                              className="btn h-7 px-2.5 text-[11px] shrink-0"
                            >
                              {hfTokenSaving ? "..." : "保存"}
                            </button>
                            {diarStatus?.has_hf_token && (
                              <button
                                onClick={async () => {
                                  try {
                                    await deleteHfToken();
                                    await refreshDiarStatus();
                                  } catch { /* noop */ }
                                }}
                                className="btn h-7 px-2.5 text-[11px] shrink-0"
                                title="保存済みトークンを削除"
                              >
                                ✕
                              </button>
                            )}
                          </div>
                        </SRow>
                        {hfTokenError && (
                          <p className="text-[11px] text-(--danger) -mt-1">{hfTokenError}</p>
                        )}
                        {diarStatus?.has_hf_token && (
                          <SRow
                            label="接続テスト"
                            hint={
                              diarTestResult
                                ? diarTestResult.ok
                                  ? "✓ ロード成功"
                                  : `エラー: ${diarTestResult.code || "LOAD_FAILED"}`
                                : undefined
                            }
                          >
                            <button
                              onClick={async () => {
                                setDiarTesting(true);
                                setDiarTestResult(null);
                                try {
                                  setDiarTestResult(await testDiarization());
                                } catch (e) {
                                  setDiarTestResult({
                                    ok: false,
                                    message: e instanceof Error ? e.message : "テスト失敗",
                                  });
                                } finally {
                                  setDiarTesting(false);
                                }
                              }}
                              disabled={diarTesting}
                              className="btn h-7 px-3 text-[11px]"
                            >
                              {diarTesting ? "確認中..." : "テスト"}
                            </button>
                          </SRow>
                        )}
                        {diarTestResult && !diarTestResult.ok && diarTestResult.code === "GATED_REPO" && (
                          <p className="text-[11px] text-(--t2) leading-relaxed -mt-1">
                            モデル利用規約の承認が必要:{" "}
                            <a
                              href="https://huggingface.co/pyannote/speaker-diarization-3.1"
                              target="_blank"
                              rel="noreferrer"
                              className="text-(--accent) hover:underline"
                            >
                              speaker-diarization-3.1
                            </a>
                            ,{" "}
                            <a
                              href="https://huggingface.co/pyannote/segmentation-3.0"
                              target="_blank"
                              rel="noreferrer"
                              className="text-(--accent) hover:underline"
                            >
                              segmentation-3.0
                            </a>
                            ,{" "}
                            <a
                              href="https://huggingface.co/pyannote/speaker-diarization-community-1"
                              target="_blank"
                              rel="noreferrer"
                              className="text-(--accent) hover:underline"
                            >
                              community-1
                            </a>
                          </p>
                        )}
                        <SRow label="最少話者数" hint="任意 (空欄で自動)">
                          <input
                            type="number"
                            min={1}
                            max={20}
                            value={diarMinSpeakers}
                            onChange={(e) => setDiarMinSpeakers(e.target.value)}
                            className="input s-control"
                            placeholder="自動"
                          />
                        </SRow>
                        <SRow label="最大話者数" hint="任意 (空欄で自動)">
                          <input
                            type="number"
                            min={1}
                            max={20}
                            value={diarMaxSpeakers}
                            onChange={(e) => setDiarMaxSpeakers(e.target.value)}
                            className="input s-control"
                            placeholder="自動"
                          />
                        </SRow>
                        <SRow label="デバイス">
                          <Select
                            value={diarDevice}
                            onChange={(v) => setDiarDevice(v as "auto" | "cpu" | "mps")}
                            options={[
                              { value: "auto", label: "自動" },
                              { value: "mps", label: "MPS (Apple GPU)" },
                              { value: "cpu", label: "CPU" },
                            ]}
                          />
                        </SRow>
                      </SGroup>
                    )}

                    <SGroup title="話者記憶" hint="全プロジェクト共通の話者プロファイル">
                      <SRow label="話者記憶を有効にする">
                        <SToggle value={speakerMemoryEnabled} onChange={setSpeakerMemoryEnabled} />
                      </SRow>
                      <SRow label="照合しきい値" hint="0.1〜0.99 (高いほど厳しい)">
                        <input
                          type="number"
                          step={0.01}
                          min={0.1}
                          max={0.99}
                          value={speakerMatchThreshold}
                          onChange={(e) => setSpeakerMatchThreshold(Number(e.target.value || 0.82))}
                          className="input s-control"
                        />
                      </SRow>
                      <SRow label="最小音声秒数" hint="この長さ以下の発話は無視">
                        <input
                          type="number"
                          step={0.1}
                          min={0.4}
                          max={6}
                          value={speakerMinAudioSec}
                          onChange={(e) => setSpeakerMinAudioSec(Number(e.target.value || 1.0))}
                          className="input s-control"
                        />
                      </SRow>
                    </SGroup>
                    </>)}

                  </>
                )}

                {category === "speaker-list" && (
                  <SpeakerListPanel
                    speakers={speakers}
                    loading={speakersLoading}
                    error={speakersError}
                    selected={selectedSpeakers}
                    bulkBusy={bulkBusy}
                    labelDrafts={labelDrafts}
                    renaming={renaming}
                    onToggleSelect={toggleSelected}
                    onClearSelection={clearSelection}
                    onChangeName={(id, v) =>
                      setLabelDrafts((prev) => ({ ...prev, [id]: v }))
                    }
                    onCommitName={doRenameSpeaker}
                    onOpenMerge={() => setMergeDialogOpen(true)}
                    onDelete={doDeleteSelected}
                    onRefresh={refreshSpeakers}
                  />
                )}

                {category === "ai" && (
                  <>
                    <SGroup
                      title="要約プロバイダ"
                      hint="議事録要約を生成するモデル/サービス。録音停止時に自動実行。"
                    >
                      {aiRecommended?.provider && (
                        <p className="text-[11px] text-(--t3) mb-2 leading-relaxed">
                          推奨: <span className="text-(--t1) font-medium">{PROVIDER_LABELS[aiRecommended.provider]}</span>
                          {" — "}{aiRecommended.reason}
                        </p>
                      )}
                      <div className="flex flex-col">
                        {SUMMARIZE_PROVIDERS.map((p) => (
                          <ProviderRadioRow
                            key={p}
                            provider={p}
                            active={aiProvider === p}
                            recommended={aiRecommended?.provider === p}
                            keyPresent={aiKeysPresent[p]}
                            consented={aiConsent[p]}
                            onSelect={handleSelectAiProvider}
                          />
                        ))}
                      </div>
                      <div className="flex items-center gap-2 mt-3">
                        <button
                          onClick={handleTestProvider}
                          disabled={aiTestBusy}
                          className="btn h-7 px-2.5 text-[11px]"
                        >
                          {aiTestBusy && <Spinner size={12} />}
                          接続テスト
                        </button>
                        {aiTestResult && (
                          <span
                            className={`text-[11px] flex-1 truncate ${
                              aiTestResult.ok ? "text-(--success)" : "text-(--danger)"
                            }`}
                            title={aiTestResult.message}
                          >
                            {aiTestResult.ok ? "✓ " : "⚠ "}
                            {aiTestResult.ok
                              ? `Ready${aiTestResult.model ? ` (${aiTestResult.model})` : ""}`
                              : `[${aiTestResult.code}] ${aiTestResult.message}`}
                          </span>
                        )}
                      </div>
                    </SGroup>

                    {/* 選択中 provider の詳細設定 */}
                    {aiProvider === "ollama" && (
                      <SGroup title="Ollama 設定">
                        <SRow label="モデル" hint="Qwen3 4B/8B/14B/32B から選択">
                          <Select
                            value={aiOllamaModel}
                            onChange={setAiOllamaModel}
                            options={[
                              { value: "qwen3:4b", label: "qwen3:4b (~2.5GB, 軽量)" },
                              { value: "qwen3:8b", label: "qwen3:8b (~5GB, 既定)" },
                              { value: "qwen3:14b", label: "qwen3:14b (~9GB, 32GB+ Mac推奨)" },
                              { value: "qwen3:32b", label: "qwen3:32b (~20GB, 64GB+ Mac推奨)" },
                            ]}
                          />
                        </SRow>
                        <SRow label="コンテキスト長" hint="長尺会議は大きく">
                          <input
                            type="number"
                            min={2048}
                            max={131072}
                            step={1024}
                            value={aiOllamaCtx}
                            onChange={(e) => setAiOllamaCtx(Number(e.target.value || 8192))}
                            className="input s-control"
                          />
                        </SRow>
                      </SGroup>
                    )}

                    {aiProvider === "claude_api" && (
                      <SGroup title="Claude API 設定">
                        <SRow label="モデル">
                          <Select
                            value={aiClaudeModel}
                            onChange={setAiClaudeModel}
                            options={[
                              { value: "claude-haiku-4-5", label: "claude-haiku-4.5 (高速・低コスト)" },
                              { value: "claude-sonnet-4-6", label: "claude-sonnet-4.6 (推奨)" },
                              { value: "claude-opus-4-7", label: "claude-opus-4.7 (最高精度)" },
                            ]}
                          />
                        </SRow>
                        <SRow label="APIキー" hint="60分会議1件 ≈ Sonnet $0.05 / Opus $0.22">
                          <ApiKeyField
                            provider="claude_api"
                            present={!!aiKeysPresent.claude_api}
                            draft={aiKeyDraft.claude_api || ""}
                            busy={!!aiKeyBusy.claude_api}
                            onDraftChange={(v) =>
                              setAiKeyDraft((prev) => ({ ...prev, claude_api: v }))
                            }
                            onSave={() => handleSaveApiKey("claude_api")}
                            onDelete={() => handleDeleteApiKey("claude_api")}
                          />
                        </SRow>
                      </SGroup>
                    )}

                    {aiProvider === "openai" && (
                      <SGroup title="OpenAI (GPT) 設定">
                        <SRow label="モデル">
                          <Select
                            value={aiOpenAIModel}
                            onChange={setAiOpenAIModel}
                            options={[
                              { value: "gpt-4o-mini", label: "gpt-4o-mini (推奨・低コスト)" },
                              { value: "gpt-4o", label: "gpt-4o" },
                              { value: "o1-mini", label: "o1-mini (reasoning)" },
                            ]}
                          />
                        </SRow>
                        <SRow label="APIキー" hint="60分会議1件 ≈ gpt-4o-mini $0.002 / gpt-4o $0.04">
                          <ApiKeyField
                            provider="openai"
                            present={!!aiKeysPresent.openai}
                            draft={aiKeyDraft.openai || ""}
                            busy={!!aiKeyBusy.openai}
                            onDraftChange={(v) =>
                              setAiKeyDraft((prev) => ({ ...prev, openai: v }))
                            }
                            onSave={() => handleSaveApiKey("openai")}
                            onDelete={() => handleDeleteApiKey("openai")}
                          />
                        </SRow>
                      </SGroup>
                    )}

                    {aiProvider === "gemini" && (
                      <SGroup title="Gemini 設定" hint="provider 実装は v1 後続フェーズで対応予定">
                        <SRow label="モデル">
                          <Select
                            value={aiGeminiModel}
                            onChange={setAiGeminiModel}
                            options={[
                              { value: "gemini-2.0-flash", label: "gemini-2.0-flash" },
                              { value: "gemini-1.5-pro", label: "gemini-1.5-pro" },
                            ]}
                          />
                        </SRow>
                        <p className="text-[11px] text-(--t3) leading-relaxed">
                          ※ Gemini provider は実装予定。現状は設定保存のみ。
                        </p>
                      </SGroup>
                    )}

                    {aiProvider === "claude_code" && (
                      <SGroup title="Claude Code (CLI) 設定" hint="Claude Code CLI のサブスクを使用、APIキー不要">
                        <SRow label="モデルエイリアス" hint="alias は常に最新版を指します">
                          <Select
                            value={aiClaudeCodeModel}
                            onChange={setAiClaudeCodeModel}
                            options={
                              claudeCodeChoices.length > 0
                                ? claudeCodeChoices.map((m) => ({
                                    value: m.slug,
                                    label: m.display_name,
                                  }))
                                : [
                                    { value: "haiku", label: "haiku (高速)" },
                                    { value: "sonnet", label: "sonnet (推奨)" },
                                    { value: "opus", label: "opus (最高精度)" },
                                  ]
                            }
                          />
                        </SRow>
                        <SRow
                          label="起動コマンド (任意)"
                          hint="zsh function を使う場合に指定 (例: source ~/.zshrc; my_claude)"
                        >
                          <input
                            type="text"
                            value={aiClaudeCodeLauncher}
                            onChange={(e) => setAiClaudeCodeLauncher(e.target.value)}
                            placeholder="未指定なら PATH の claude を直接実行"
                            className="input s-control"
                          />
                        </SRow>
                        <p className="text-[11px] text-(--t3) leading-relaxed">
                          既定では PATH の <code className="px-1 py-0.5 bg-(--surface-2) rounded">claude</code> を実行します。
                          起動コマンドを指定した場合は <code className="px-1 py-0.5 bg-(--surface-2) rounded">/bin/zsh -ic</code> 経由で実行します。
                          docs フォルダを <code className="px-1 py-0.5 bg-(--surface-2) rounded">--add-dir</code> で渡し、
                          KNOWLEDGE.md 等を agent が自動参照します。
                        </p>
                      </SGroup>
                    )}

                    {aiProvider === "codex" && (
                      <SGroup title="Codex (CLI) 設定" hint="ChatGPT サブスクを使用、APIキー不要">
                        <SRow
                          label="モデル"
                          hint={codexModelChoices.length === 0
                            ? "codex CLI のキャッシュから取得 (一度 codex を起動するとモデル一覧が表示されます)"
                            : "codex CLI が提供するモデルから選択"}
                        >
                          <Select
                            value={aiCodexModel}
                            onChange={setAiCodexModel}
                            placeholder="CLIデフォルト (自動)"
                            options={
                              (() => {
                                const opts = [
                                  ...codexModelChoices.map((m) => ({
                                    value: m.slug,
                                    label: m.display_name,
                                    hint: m.description,
                                  })),
                                ];
                                if (
                                  aiCodexModel
                                  && !opts.some((o) => o.value === aiCodexModel)
                                ) {
                                  opts.push({
                                    value: aiCodexModel,
                                    label: `${aiCodexModel} (カスタム)`,
                                    hint: undefined,
                                  });
                                }
                                return opts;
                              })()
                            }
                          />
                        </SRow>
                        <SRow label="CLIデフォルトに戻す" hint="--model を付けず、codex CLI 側の既定モデルを使います">
                          <button
                            type="button"
                            className="btn h-7 px-2.5 text-[11px]"
                            onClick={() => setAiCodexModel("")}
                            disabled={!aiCodexModel}
                          >
                            自動に戻す
                          </button>
                        </SRow>
                        <SRow
                          label="起動コマンド (任意)"
                          hint="zsh function を使う場合に指定 (例: source ~/.zshrc; my_codex)"
                        >
                          <input
                            type="text"
                            value={aiCodexLauncher}
                            onChange={(e) => setAiCodexLauncher(e.target.value)}
                            placeholder="未指定なら PATH の codex を直接実行"
                            className="input s-control"
                          />
                        </SRow>
                        <p className="text-[11px] text-(--t3) leading-relaxed">
                          既定では PATH の <code className="px-1 py-0.5 bg-(--surface-2) rounded">codex</code> を実行します。
                          起動コマンドを指定した場合は <code className="px-1 py-0.5 bg-(--surface-2) rounded">/bin/zsh -ic</code> 経由で実行します。
                          docs フォルダを <code className="px-1 py-0.5 bg-(--surface-2) rounded">--add-dir</code> で渡し、
                          KNOWLEDGE.md 等を agent が自動参照します。
                        </p>
                      </SGroup>
                    )}

                    <SGroup title="動作">
                      <SRow label="録音停止時に自動要約" hint="provider未設定/同意未取得時はスキップ">
                        <SToggle value={aiAutoGenerate} onChange={setAiAutoGenerate} />
                      </SRow>
                      <SRow label="議事録タイトルも自動生成" hint="要約と同時に短いタイトルを生成し、議事録名を上書き">
                        <SToggle value={aiGenerateTitle} onChange={setAiGenerateTitle} />
                      </SRow>
                      <SRow
                        label="要約後にプロジェクトの辞書を自動更新"
                        hint="議事録の transcript とドキュメントを照合し、新規用語と誤転写ペアを project に追記。次回以降の文字起こし精度が上がる"
                      >
                        <SToggle value={aiAutoDictionaryUpdate} onChange={setAiAutoDictionaryUpdate} />
                      </SRow>
                      <SRow label="タイムアウト (秒)" hint="生成時間がこれを超えたら失敗扱い">
                        <input
                          type="number"
                          min={30}
                          max={1800}
                          step={30}
                          value={aiTimeoutSec}
                          onChange={(e) => setAiTimeoutSec(Number(e.target.value || 300))}
                          className="input s-control"
                        />
                      </SRow>
                    </SGroup>

                    <SGroup
                      title="要約プロンプト"
                      hint="要約生成に使う指示文 (system prompt)。編集するとカスタムとして保存され、すべてのプロバイダに適用されます"
                    >
                      <div className="flex items-center gap-2 mt-3 mb-2">
                        <span
                          className={`text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wider font-semibold ${
                            promptIsCustom
                              ? "bg-(--accent-soft) text-(--accent)"
                              : "bg-(--surface-2) text-(--t3)"
                          }`}
                        >
                          {promptIsCustom ? "カスタム" : "デフォルト"}
                        </span>
                        <span className="flex-1" />
                        <button
                          type="button"
                          className="btn h-7 px-2.5 text-[11px]"
                          disabled={!defaultPrompt || !promptIsCustom}
                          onClick={() => setAiPromptDraft(defaultPrompt)}
                          title="既定のプロンプトに戻す (保存で確定)"
                        >
                          デフォルトに戻す
                        </button>
                      </div>
                      <textarea
                        className="textarea prompt-editor"
                        value={promptValue}
                        onChange={(e) => setAiPromptDraft(e.target.value)}
                        rows={14}
                        spellCheck={false}
                        placeholder={defaultPrompt ? "" : "既定プロンプトを読み込み中..."}
                      />
                      <p className="text-[11px] text-(--t3) leading-relaxed mt-1.5">
                        デフォルトと同じ内容にすると自動的に「デフォルト」扱いに戻ります。
                        タイトル生成のプロンプトは変更されません。
                      </p>
                    </SGroup>
                  </>
                )}

                {category === "recording" && (
                  <SGroup title="録音停止忘れ通知" hint="自動停止はせず、無音が続いたら通知のみ">
                    <SRow label="通知を有効にする">
                      <SToggle value={stopForgetEnabled} onChange={setStopForgetEnabled} />
                    </SRow>
                    <SRow label="無音秒数" hint="この秒数を超えたら通知">
                      <input
                        type="number"
                        min={10}
                        max={3600}
                        value={stopForgetSilenceSec}
                        onChange={(e) => setStopForgetSilenceSec(Number(e.target.value || 300))}
                        className="input s-control"
                      />
                    </SRow>
                    <SRow label="無音判定しきい値" hint="この音量以下を無音と判定">
                      <input
                        type="number"
                        step={0.001}
                        min={0}
                        max={1}
                        value={stopForgetLevelThreshold}
                        onChange={(e) => setStopForgetLevelThreshold(Number(e.target.value || 0.02))}
                        className="input s-control"
                      />
                    </SRow>
                  </SGroup>
                )}

                {category === "debug" && (
                  <SGroup title="デバッグ">
                    <SRow label="ログレベル">
                      <Select
                        value={ll}
                        onChange={setLl}
                        options={[
                          { value: "DEBUG", label: "DEBUG" },
                          { value: "INFO", label: "INFO" },
                          { value: "WARNING", label: "WARNING" },
                          { value: "ERROR", label: "ERROR" },
                        ]}
                      />
                    </SRow>
                    <SRow label="デバッグモード" hint="メモリ/キュー/処理状態/ログを表示">
                      <SToggle value={debugEnabled} onChange={setDebugEnabled} />
                    </SRow>
                  </SGroup>
                )}
              </div>
            )}
            </div>

            {settings && (
              <footer className="settings-save-footer sticky bottom-0 flex items-center gap-3 px-5 py-3 shrink-0">
                <button
                  onClick={() => void save()}
                  disabled={saving || !isDirty}
                  className="btn btn-primary h-8 px-4 text-[12px]"
                >
                  {saving ? "保存中..." : "保存"}
                </button>
                {saved && (
                  <span className="anim-fade-in flex items-center gap-1 text-[11px] text-(--success)">
                    <CheckCircle size={12} weight="fill" />
                    保存しました
                  </span>
                )}
                {!saved && isDirty && (
                  <span className="text-[11px] text-(--warning)">未保存の変更があります</span>
                )}
                {!saved && !isDirty && (
                  <span className="text-[11px] text-(--t4)">変更はありません</span>
                )}
                <span className="flex-1" />
                <span className="text-[10px] text-(--t4)">⌘S で保存</span>
              </footer>
            )}
          </main>
        </div>
      </div>

      {mergeDialogOpen && (
        <MergeSpeakersDialog
          speakers={speakers.filter((s) => selectedSpeakers.has(s.id))}
          busy={bulkBusy}
          onCancel={() => setMergeDialogOpen(false)}
          onConfirm={doMergeConfirmed}
        />
      )}

      {aiConsentDialog && (
        <ConsentDialog
          provider={aiConsentDialog}
          onCancel={() => setAiConsentDialog(null)}
          onAccept={() => acceptAiConsent(aiConsentDialog)}
        />
      )}

      {closePromptOpen && (
        <UnsavedChangesDialog
          busy={saving}
          onCancel={() => setClosePromptOpen(false)}
          onDiscard={() => {
            setClosePromptOpen(false);
            finishClose();
          }}
          onSave={async () => {
            const ok = await save();
            if (ok) {
              setClosePromptOpen(false);
              finishClose();
            }
          }}
        />
      )}
    </div>
  );
}

/* ─────────── Settings 用ヘルパー ─────────── */

function SGroup({
  title, hint, children,
}: {
  title?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="s-group">
      {title && <p className="s-group-title">{title}</p>}
      {hint && <p className="s-group-hint">{hint}</p>}
      <div className="s-rows">{children}</div>
    </section>
  );
}

function SRow({
  label, hint, children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="s-row">
      <div className="s-row-label">
        <span>{label}</span>
        {hint && <span className="s-row-hint">{hint}</span>}
      </div>
      <div className="s-row-control">{children}</div>
    </div>
  );
}

function SToggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      className={`s-toggle ${value ? "is-on" : ""}`}
      role="switch"
      aria-checked={value}
    >
      <span className="s-toggle-dot" />
    </button>
  );
}

function SRadioList<T extends string>({
  value, onChange, options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { key: T; label: string; hint?: string }[];
}) {
  return (
    <div className="s-radio-list">
      {options.map((opt) => {
        const active = value === opt.key;
        return (
          <button
            key={opt.key}
            type="button"
            onClick={() => onChange(opt.key)}
            className={`s-radio-row ${active ? "is-active" : ""}`}
          >
            <span className={`s-radio-dot ${active ? "is-on" : ""}`} />
            <div className="s-radio-text">
              <span className="s-radio-label">{opt.label}</span>
              {opt.hint && <span className="s-radio-hint">{opt.hint}</span>}
            </div>
          </button>
        );
      })}
    </div>
  );
}

/* ───────── 要約 AI 関連 ヘルパー ───────── */

/** Provider 選択行 — radio + ステータスバッジ + cloud マーク */
function ProviderRadioRow({
  provider, active, recommended, keyPresent, consented, onSelect,
}: {
  provider: SummarizeProvider;
  active: boolean;
  recommended: boolean;
  keyPresent: boolean | undefined;
  consented: boolean | undefined;
  onSelect: (p: SummarizeProvider) => void;
}) {
  const isCloud = (CLOUD_PROVIDERS as readonly string[]).includes(provider);
  const isCli = (CLI_PROVIDERS as readonly string[]).includes(provider);
  // ステータス文言
  let badge: { label: string; variant: "ok" | "warn" | "info" } | null = null;
  if (isCloud && !keyPresent) {
    badge = { label: "APIキー要", variant: "warn" };
  } else if (isCloud && !consented) {
    badge = { label: "未同意", variant: "warn" };
  } else if (isCli) {
    badge = { label: "CLI", variant: "info" };
  } else if (isCloud) {
    badge = { label: "Ready", variant: "ok" };
  }

  return (
    <button
      type="button"
      onClick={() => onSelect(provider)}
      className={`s-radio-row ${active ? "is-active" : ""}`}
    >
      <span className={`s-radio-dot ${active ? "is-on" : ""}`} />
      <div className="s-radio-text">
        <span className="s-radio-label flex items-center gap-1.5">
          {PROVIDER_LABELS[provider]}
          {recommended && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-(--accent-soft) text-(--accent) font-semibold uppercase tracking-wider">
              推奨
            </span>
          )}
        </span>
      </div>
      {badge && (
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wider font-semibold shrink-0 ${
            badge.variant === "ok"
              ? "bg-(--success-soft) text-(--success)"
              : badge.variant === "warn"
              ? "bg-(--accent-soft) text-(--danger)"
              : "bg-(--surface-2) text-(--t3)"
          }`}
        >
          {badge.label}
        </span>
      )}
    </button>
  );
}

/** APIキー入力 + 保存/削除 inline form */
function ApiKeyField({
  provider: _provider, present, draft, busy, onDraftChange, onSave, onDelete,
}: {
  provider: SummarizeProvider;
  present: boolean;
  draft: string;
  busy: boolean;
  onDraftChange: (v: string) => void;
  onSave: () => void;
  onDelete: () => void;
}) {
  if (present && !draft) {
    // 既に保存済み → 表示のみ + 削除/変更ボタン
    return (
      <div className="flex items-center gap-2 flex-1">
        <span className="text-[12px] text-(--t2) font-mono">●●●●●●●● 保存済み</span>
        <button
          onClick={() => onDraftChange(" ")}
          disabled={busy}
          className="btn h-7 px-2.5 text-[11px]"
        >
          変更
        </button>
        <button
          onClick={onDelete}
          disabled={busy}
          className="btn btn-ghost btn-danger h-7 px-2.5 text-[11px]"
        >
          {busy ? <Spinner size={12} /> : "削除"}
        </button>
      </div>
    );
  }
  // onDelete は present && !draft の分岐でのみ使うため、編集モードでは使わない
  void onDelete;
  return <ApiKeyFieldEditing
    present={present}
    draft={draft}
    busy={busy}
    onDraftChange={onDraftChange}
    onSave={onSave}
  />;
}

function ApiKeyFieldEditing({
  present, draft, busy, onDraftChange, onSave,
}: {
  present: boolean;
  draft: string;
  busy: boolean;
  onDraftChange: (v: string) => void;
  onSave: () => void;
}) {
  const { imeHandlers, isImeEnter } = useImeSafeEnter();
  return (
    <div className="flex items-center gap-2 flex-1" {...imeHandlers}>
      <input
        type="password"
        value={draft.trim() ? draft : ""}
        onChange={(e) => onDraftChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !isImeEnter(e) && draft.trim()) onSave();
        }}
        placeholder={present ? "新しいキーを入力" : "sk-..."}
        className="input s-control"
        autoComplete="off"
        disabled={busy}
      />
      <button
        onClick={onSave}
        disabled={busy || !draft.trim()}
        className="btn btn-primary h-7 px-2.5 text-[11px] shrink-0"
      >
        {busy ? <Spinner size={12} /> : "保存"}
      </button>
      {present && (
        <button
          onClick={() => onDraftChange("")}
          disabled={busy}
          className="btn h-7 px-2.5 text-[11px] shrink-0"
        >
          キャンセル
        </button>
      )}
    </div>
  );
}

/** Cloud provider 初回利用同意モーダル */
function ConsentDialog({
  provider, onCancel, onAccept,
}: {
  provider: SummarizeProvider;
  onCancel: () => void;
  onAccept: () => void;
}) {
  const costHint: Record<string, string> = {
    claude_api: "60分会議1件 ≈ Sonnet $0.05 / Opus $0.22 / Haiku $0.005",
    openai: "60分会議1件 ≈ gpt-4o-mini $0.002 / gpt-4o $0.04",
    gemini: "60分会議1件 ≈ Flash $0.001 / Pro $0.02",
  };
  return (
    <div className="fixed inset-0 z-60 flex items-center justify-center anim-modal-overlay-in">
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={onCancel}
        style={{ background: "rgba(0,0,0,0.4)" }}
      />
      <div className="dialog-shell relative w-[440px] max-w-[92vw] flex flex-col anim-modal-in">
        <header className="px-5 py-4 border-b border-(--border)">
          <h3 className="text-[14px] font-semibold text-(--t1)">
            {PROVIDER_LABELS[provider]} の利用同意
          </h3>
        </header>
        <div className="px-5 py-4 flex flex-col gap-3">
          <p className="text-[12px] text-(--t2) leading-relaxed">
            <strong className="text-(--danger)">⚠️ 文字起こしテキストが外部API ({PROVIDER_LABELS[provider]}) に送信されます。</strong>
            機密情報を含む会議では利用しないでください。
          </p>
          {costHint[provider] && (
            <div className="text-[11px] text-(--t3) bg-(--surface) border border-(--border) rounded-md px-3 py-2">
              <span className="font-medium text-(--t2)">概算コスト:</span> {costHint[provider]}
            </div>
          )}
          <p className="text-[11px] text-(--t3) leading-relaxed">
            この同意は1度だけ求められます。設定画面から取り消せます。
          </p>
        </div>
        <footer className="px-4 py-3 border-t border-(--border) flex items-center justify-end gap-2">
          <button onClick={onCancel} className="btn h-7 px-3 text-[11px]">
            キャンセル
          </button>
          <button onClick={onAccept} className="btn btn-primary h-7 px-3 text-[11px]">
            理解した上で利用する
          </button>
        </footer>
      </div>
    </div>
  );
}

function UnsavedChangesDialog({
  busy, onCancel, onDiscard, onSave,
}: {
  busy: boolean;
  onCancel: () => void;
  onDiscard: () => void;
  onSave: () => void | Promise<void>;
}) {
  return (
    <div className="fixed inset-0 z-60 flex items-center justify-center anim-modal-overlay-in">
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={busy ? undefined : onCancel}
        style={{ background: "rgba(0,0,0,0.4)" }}
      />
      <div
        className="dialog-shell relative w-[420px] max-w-[92vw] flex flex-col anim-modal-in"
        role="dialog"
        aria-modal="true"
        aria-labelledby="unsaved-settings-title"
      >
        <header className="px-5 py-4 border-b border-(--border)">
          <h3 id="unsaved-settings-title" className="text-[14px] font-semibold text-(--t1)">
            設定を閉じますか？
          </h3>
        </header>
        <div className="px-5 py-4">
          <p className="text-[12px] text-(--t2) leading-relaxed">
            保存していない変更があります！保存せずに閉じると、変更内容は破棄されます。
          </p>
        </div>
        <footer className="px-4 py-3 border-t border-(--border) flex items-center justify-end gap-2">
          <button onClick={onCancel} disabled={busy} className="btn h-7 px-3 text-[11px]">
            キャンセル
          </button>
          <button onClick={onDiscard} disabled={busy} className="btn h-7 px-3 text-[11px]">
            保存せず閉じる
          </button>
          <button onClick={() => void onSave()} disabled={busy} className="btn btn-primary h-7 px-3 text-[11px]">
            {busy ? <Spinner size={11} /> : "保存して閉じる"}
          </button>
        </footer>
      </div>
    </div>
  );
}

/* ───────── 話者一覧 ───────── */

function speakerColor(key: string | null | undefined): string {
  const k = key || "?";
  let hash = 5381;
  for (let i = 0; i < k.length; i++) hash = ((hash << 5) + hash + k.charCodeAt(i)) | 0;
  const palette = [
    "hsl(210, 35%, 55%)",
    "hsl(35, 40%, 55%)",
    "hsl(150, 30%, 50%)",
    "hsl(265, 30%, 60%)",
    "hsl(180, 30%, 50%)",
    "hsl(0, 35%, 60%)",
    "hsl(85, 30%, 50%)",
  ];
  return palette[Math.abs(hash) % palette.length];
}

function SpeakerNameInput({
  value, dirty, onChange, onCommit, onCancel, disabled,
}: {
  value: string;
  dirty: boolean;
  onChange: (v: string) => void;
  onCommit: () => void;
  onCancel: () => void;
  disabled?: boolean;
}) {
  const { imeHandlers, isImeEnter } = useImeSafeEnter();
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onBlur={() => { if (dirty) onCommit(); }}
      {...imeHandlers}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          if (isImeEnter(e)) return;
          e.preventDefault();
          (e.target as HTMLInputElement).blur();
        } else if (e.key === "Escape") {
          onCancel();
          (e.target as HTMLInputElement).blur();
        }
      }}
      className="spk-name-input"
      placeholder="話者名"
      disabled={disabled}
    />
  );
}

function SpeakerListPanel({
  speakers, loading, error, selected, bulkBusy, labelDrafts, renaming,
  onToggleSelect, onClearSelection, onChangeName, onCommitName,
  onOpenMerge, onDelete, onRefresh,
}: {
  speakers: SpeakerProfile[];
  loading: boolean;
  error: string;
  selected: Set<string>;
  bulkBusy: boolean;
  labelDrafts: Record<string, string>;
  renaming: Record<string, boolean>;
  onToggleSelect: (id: string) => void;
  onClearSelection: () => void;
  onChangeName: (id: string, value: string) => void;
  onCommitName: (id: string) => void;
  onOpenMerge: () => void;
  onDelete: () => void;
  onRefresh: () => void;
}) {
  const [query, setQuery] = useState("");
  const [playingId, setPlayingId] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // unmount 時にオーディオ停止
  useEffect(() => {
    return () => {
      const a = audioRef.current;
      if (a) {
        try { a.pause(); } catch {}
        a.src = "";
        audioRef.current = null;
      }
    };
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return speakers;
    return speakers.filter((sp) =>
      sp.label.toLowerCase().includes(q) || sp.id.toLowerCase().includes(q),
    );
  }, [speakers, query]);

  const toggleAudio = (id: string) => {
    if (playingId === id && audioRef.current) {
      audioRef.current.pause();
      setPlayingId(null);
      return;
    }
    if (audioRef.current) {
      try { audioRef.current.pause(); } catch {}
      audioRef.current = null;
    }
    const a = new Audio(speakerSampleUrl(id));
    a.onended = () => setPlayingId(null);
    a.onpause = () => {
      if (playingId === id) setPlayingId(null);
    };
    a.play().then(() => setPlayingId(id)).catch(() => setPlayingId(null));
    audioRef.current = a;
  };

  const selectedCount = selected.size;

  return (
    <div className="speaker-panel">
      {/* ツールバー */}
      <div className="speaker-toolbar">
        <div className="speaker-search">
          <MagnifyingGlass size={12} weight="regular" className="text-(--t3) shrink-0" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="話者名で絞り込み"
            className="speaker-search-input"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="icon-btn !w-5 !h-5"
              aria-label="クリア"
            >
              <X size={10} weight="bold" />
            </button>
          )}
        </div>
        <button onClick={onRefresh} className="btn h-7 px-2 text-[11px] shrink-0" title="再読込">
          <ArrowsClockwise size={12} />
        </button>
      </div>

      {/* バルクアクションバー (常時表示, 選択数で disabled 切替) */}
      <div className={`speaker-bulkbar ${selectedCount > 0 ? "is-active" : ""}`}>
        <span className="text-[11px] text-(--t2)">
          {selectedCount > 0 ? `${selectedCount} 件選択中` : "チェックで選択 → 統合 / 削除"}
        </span>
        <button
          onClick={onOpenMerge}
          disabled={bulkBusy || selectedCount < 2}
          className="btn h-7 px-2.5 text-[11px]"
          title={selectedCount < 2 ? "2件以上選択すると統合できます" : "選択した話者を統合"}
        >
          <UsersThree size={12} weight="regular" />
          統合
        </button>
        <button
          onClick={onDelete}
          disabled={bulkBusy || selectedCount === 0}
          className="btn btn-ghost btn-danger h-7 px-2.5 text-[11px]"
        >
          <Trash size={12} weight="regular" />
          削除
        </button>
        <button
          onClick={onClearSelection}
          disabled={bulkBusy || selectedCount === 0}
          className="btn h-7 px-2.5 text-[11px]"
        >
          選択解除
        </button>
      </div>

      {/* 一覧 */}
      {loading ? (
        <div className="flex justify-center py-12">
          <Spinner size={18} />
        </div>
      ) : speakers.length === 0 ? (
        <p className="text-[12px] text-(--t3) text-center py-12">
          話者プロファイルはまだありません。録音して文字起こしすると自動作成されます。
        </p>
      ) : filtered.length === 0 ? (
        <p className="text-[12px] text-(--t3) text-center py-12">
          一致する話者が見つかりません
        </p>
      ) : (
        <div className="speaker-rows-v2">
          {filtered.map((sp) => {
            const checked = selected.has(sp.id);
            const isPlaying = playingId === sp.id;
            const draft = labelDrafts[sp.id] ?? "";
            const dirty = draft.trim().length > 0 && draft !== sp.label;
            const busy = Boolean(renaming[sp.id]);
            return (
              <div
                key={sp.id}
                className={`spk-row ${checked ? "is-selected" : ""}`}
              >
                <button
                  onClick={() => onToggleSelect(sp.id)}
                  className={`spk-check ${checked ? "is-on" : ""}`}
                  aria-label={`${sp.label} を選択`}
                  title="選択"
                >
                  {checked && <Check size={11} weight="bold" />}
                </button>
                <div
                  className="spk-avatar"
                  style={{ background: speakerColor(sp.id) }}
                  aria-hidden
                >
                  {(sp.label || "?").slice(0, 1)}
                </div>
                <div className="spk-name-wrap">
                  <SpeakerNameInput
                    value={draft}
                    dirty={dirty}
                    onChange={(v) => onChangeName(sp.id, v)}
                    onCommit={() => onCommitName(sp.id)}
                    onCancel={() => onChangeName(sp.id, sp.label)}
                    disabled={busy}
                  />
                  <p className="spk-stats">
                    出現 {sp.appearances} 回 · {sp.total_audio_sec.toFixed(1)} 秒
                  </p>
                </div>
                <div className="spk-actions">
                  {sp.sample_available ? (
                    <button
                      onClick={() => toggleAudio(sp.id)}
                      className="spk-play-btn"
                      title={isPlaying ? "停止" : "サンプル再生"}
                    >
                      {isPlaying
                        ? <Pause size={11} weight="fill" />
                        : <Play size={11} weight="fill" />}
                    </button>
                  ) : (
                    <span className="spk-no-sample" title="サンプル音声なし">—</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {error && <p className="text-[11px] text-(--danger) mt-2">{error}</p>}
    </div>
  );
}

function MergeSpeakersDialog({
  speakers, busy, onCancel, onConfirm,
}: {
  speakers: SpeakerProfile[];
  busy: boolean;
  onCancel: () => void;
  onConfirm: (primaryId: string) => void;
}) {
  const defaultPrimary = useMemo(() => {
    if (speakers.length === 0) return "";
    const sorted = [...speakers].sort((a, b) => b.appearances - a.appearances);
    return sorted[0].id;
  }, [speakers]);
  const [primaryId, setPrimaryId] = useState(defaultPrimary);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => () => {
    const a = audioRef.current;
    if (a) {
      try { a.pause(); } catch {}
      audioRef.current = null;
    }
  }, []);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (busy) return;
      if (e.isComposing) return; // IME 変換確定中
      if (e.key === "Escape") { e.preventDefault(); onCancel(); }
      else if (e.key === "Enter" && primaryId) { e.preventDefault(); onConfirm(primaryId); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [busy, primaryId, onCancel, onConfirm]);

  const toggleAudio = (id: string) => {
    if (playingId === id && audioRef.current) {
      audioRef.current.pause();
      setPlayingId(null);
      return;
    }
    if (audioRef.current) {
      try { audioRef.current.pause(); } catch {}
      audioRef.current = null;
    }
    const a = new Audio(speakerSampleUrl(id));
    a.onended = () => setPlayingId(null);
    a.onpause = () => { if (playingId === id) setPlayingId(null); };
    a.play().then(() => setPlayingId(id)).catch(() => setPlayingId(null));
    audioRef.current = a;
  };

  return (
    <div className="fixed inset-0 z-60 flex items-center justify-center anim-modal-overlay-in">
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={busy ? undefined : onCancel}
        style={{ background: "rgba(0,0,0,0.4)" }}
      />
      <div className="dialog-shell relative w-[460px] max-w-[92vw] flex flex-col anim-modal-in">
        <header className="px-5 py-4 border-b border-(--border)">
          <h3 className="text-[14px] font-semibold text-(--t1)">話者を統合</h3>
          <p className="text-[11px] text-(--t3) mt-1">
            {speakers.length} 人を1人に統合します。残す話者を選んでください。
          </p>
        </header>
        <div className="px-3 py-2 max-h-[420px] overflow-y-auto">
          {speakers.map((sp) => {
            const isPrimary = sp.id === primaryId;
            const isPlaying = playingId === sp.id;
            return (
              <div
                key={sp.id}
                role="radio"
                aria-checked={isPrimary}
                tabIndex={busy ? -1 : 0}
                onClick={() => !busy && setPrimaryId(sp.id)}
                onKeyDown={(e) => {
                  if (busy) return;
                  if (e.nativeEvent.isComposing) return;
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setPrimaryId(sp.id);
                  }
                }}
                className={`merge-card ${isPrimary ? "is-primary" : ""} ${busy ? "is-busy" : ""}`}
              >
                <span className={`s-radio-dot ${isPrimary ? "is-on" : ""}`} />
                <div
                  className="spk-avatar"
                  style={{ background: speakerColor(sp.id) }}
                  aria-hidden
                >
                  {(sp.label || "?").slice(0, 1)}
                </div>
                <div className="merge-card-text">
                  <span className="merge-card-name">{sp.label || sp.id}</span>
                  <span className="merge-card-stats">
                    出現 {sp.appearances} 回 · {sp.total_audio_sec.toFixed(1)} 秒
                  </span>
                </div>
                {sp.sample_available && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); toggleAudio(sp.id); }}
                    className="spk-play-btn"
                    aria-label={isPlaying ? "停止" : "サンプル再生"}
                    title={isPlaying ? "停止" : "サンプル再生"}
                  >
                    {isPlaying
                      ? <Pause size={11} weight="fill" />
                      : <Play size={11} weight="fill" />}
                  </button>
                )}
              </div>
            );
          })}
        </div>
        <footer className="px-4 py-3 border-t border-(--border) flex items-center justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={busy}
            className="btn h-7 px-3 text-[11px]"
          >
            キャンセル
          </button>
          <button
            onClick={() => onConfirm(primaryId)}
            disabled={busy || !primaryId}
            className="btn btn-primary h-7 px-3 text-[11px]"
          >
            {busy ? <Spinner size={11} /> : "統合"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function ThemePicker({ value, onChange }: { value: ThemeMode; onChange: (m: ThemeMode) => void }) {
  const opts: Array<{ v: ThemeMode; label: string; Icon: typeof Sun }> = [
    { v: "light", label: "ライト", Icon: Sun },
    { v: "dark", label: "ダーク", Icon: MoonStars },
    { v: "system", label: "システム", Icon: Monitor },
  ];
  return (
    <div className="flex gap-1 p-0.5 bg-(--surface-2) rounded-md">
      {opts.map((o) => {
        const active = value === o.v;
        return (
          <button
            key={o.v}
            onClick={() => onChange(o.v)}
            className={`px-2.5 py-1 text-[11px] font-medium border-none rounded cursor-pointer transition-all flex items-center gap-1.5 ${
              active ? "text-(--t1) bg-(--surface-3)" : "text-(--t3) bg-transparent hover:text-(--t2)"
            }`}
          >
            <o.Icon size={12} weight={active ? "bold" : "regular"} />
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

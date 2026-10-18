import { useCallback, useEffect, useRef, useState } from "react";

export type LiveKind = "context" | "point" | "proposal" | "decision" | "concern" | "question" | "action";
export const LIVE_KINDS: Record<LiveKind, string> = {
  context: "背景", point: "論点・メモ", proposal: "提案", decision: "決定", concern: "懸念", question: "未解決", action: "アクション",
};
export interface LiveBlock {
  id: string; topic_id: string; content_md: string; kind: LiveKind;
  owner: "human" | "ai"; revision: number; position: number; source_segment_seqs: number[];
}
export interface LiveTopic {
  id: string; title: string; title_owner: "human" | "ai"; revision: number; position: number; blocks: LiveBlock[];
}
export interface LiveSuggestion {
  suggested_operation_json?: string;
  id: string; reason?: string; status?: string; operation?: Record<string, unknown>; proposed_operation?: Record<string, unknown>;
}
export interface LiveSnapshot {
  session_id: string; minutes_id?: string | null; provider: string; status: string;
  note_revision: number; last_processed_segment_seq: number; active_topic_id?: string | null;
  last_error?: string | null; topics: LiveTopic[]; suggestions: LiveSuggestion[];
  stopped?: boolean; queue_age_sec?: number;
  pending_segments: number; processing: boolean; llm_calls: number; model?: string;
}
export interface LiveEvent {
  operation_json?: string; undo_json?: string;
  id: string; actor: string; operation?: string; operation_type?: string;
  note_revision?: number; revision?: number; created_at?: string;
  before?: unknown; after?: unknown; undone?: boolean;
}
export interface LiveNotesConfig {
  enabled: boolean; provider: "inherit" | "codex" | "claude_code";
  consent: { codex: boolean; claude_code: boolean };
  models: { codex: string; claude_code: string };
  min_interval_sec: number; target_interval_sec: number; max_wait_sec: number;
  min_pending_chars: number; max_batch_chars: number; max_operations_per_batch: number;
  max_calls_per_hour: number; invocation_timeout_sec: number; max_structural_repair_calls: number;
  debounce_edit_ms: number; preserve_human_edits: boolean;
}
export const LIVE_DEFAULTS: LiveNotesConfig = {
  enabled: false, provider: "inherit", consent: { codex: false, claude_code: false },
  models: { codex: "", claude_code: "" },
  min_interval_sec: 20, target_interval_sec: 35, max_wait_sec: 60, min_pending_chars: 120,
  max_batch_chars: 6000, max_operations_per_batch: 20, max_calls_per_hour: 90,
  invocation_timeout_sec: 120, max_structural_repair_calls: 1, debounce_edit_ms: 500, preserve_human_edits: true,
};
export function normalizeLiveConfig(value: Partial<LiveNotesConfig> | undefined): LiveNotesConfig {
  return { ...LIVE_DEFAULTS, ...value, consent: { ...LIVE_DEFAULTS.consent, ...value?.consent }, models: { ...LIVE_DEFAULTS.models, ...value?.models } };
}
const BASE = "http://127.0.0.1:18900";
export class LiveApiError extends Error {
  constructor(public status: number, public detail: unknown, message: string) { super(message); }
}
export async function liveRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const serialized = body === undefined ? undefined : JSON.stringify(body);
  const response = await fetch(`${BASE}/api/live-notes${path}`, {
    method,
    keepalive: method === "PATCH" && !!serialized && new TextEncoder().encode(serialized).byteLength < 60000, headers: { "Content-Type": "application/json" },
    ...(serialized === undefined ? {} : { body: serialized }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = data.detail ?? data.error ?? {};
    const message = typeof detail === "string" ? detail : detail.message || response.statusText;
    throw new LiveApiError(response.status, detail, message);
  }
  return response.json();
}
export const sessionPath = (id: string) => `/${encodeURIComponent(id)}`;
export const getLiveSnapshot = (id: string) => liveRequest<LiveSnapshot>(sessionPath(id));
export interface LiveSessionListItem {
  session_id: string; project_id: string; minutes_id?: string | null; status: string; started_at: string; last_error?: string | null; note_revision: number;
}
export const listLiveSessions = () => liveRequest<LiveSessionListItem[]>("");
export const newRequestId = () => crypto.randomUUID();
export async function downloadLiveMarkdown(sessionId: string) {
  const response = await fetch(`${BASE}/api/live-notes${sessionPath(sessionId)}/export.md`);
  if (!response.ok) throw new Error("Markdownの出力に失敗しました");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = `${sessionId}-notes.md`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A single coalesced snapshot request; events arriving during it force another pass.
 * Server revisions never replace a newer local snapshot. Editors own their durable drafts.
 */
export function useLiveNotes(sessionId: string | null | undefined) {
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [connected, setConnected] = useState(true);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const sessionRef = useRef(sessionId); sessionRef.current = sessionId;
  const knownRevision = useRef(-1), mutationGeneration = useRef(0);
  const accept = useCallback((next: LiveSnapshot) => {
    if (next.session_id !== sessionRef.current) return;
    knownRevision.current = Math.max(knownRevision.current, next.note_revision);
    setSnapshot((previous) => !previous || previous.session_id !== next.session_id || next.note_revision >= previous.note_revision ? next : previous);
    setError(null); setUnavailable(false);
  }, []);
  useEffect(() => {
    setSnapshot(null); setError(null); setUnavailable(false); knownRevision.current = -1;
    if (!sessionId) { refreshRef.current = async () => {}; return; }
    let stopped = false, running = false, requested = false;
    const refresh = async () => {
      requested = true;
      if (running) return;
      running = true;
      try {
        while (requested && !stopped) {
          requested = false;
          try {
            const generation = mutationGeneration.current;
            const next = await getLiveSnapshot(sessionId);
            if (!stopped) {
              if (generation !== mutationGeneration.current) requested = true;
              else { accept(next); setConnected(true); }
            }
          } catch (cause) {
            if (!stopped) {
              if (cause instanceof LiveApiError && cause.status === 404) setUnavailable(true);
              else { setError(cause instanceof Error ? cause.message : "接続に失敗しました"); setConnected(false); }
            }
          }
        }
      } finally { running = false; }
    };
    refreshRef.current = refresh;
    const onEvent = (event: Event) => {
      const data = (event as CustomEvent).detail;
      if (data?.session_id !== sessionId) return;
      if (event.type === "recording-ws:live_note_patch" && typeof data.revision === "number" && data.revision <= knownRevision.current) return;
      void refresh();
    };
    const onOpen = () => { setConnected(true); void refresh(); };
    const onOnline = () => { void refresh(); };
    const events = ["live_note_patch", "live_note_status", "live_note_conflict", "live_note_completed", "live_note_error"];
    for (const name of events) window.addEventListener(`recording-ws:${name}`, onEvent);
    window.addEventListener("recording-ws:__open", onOpen);
    window.addEventListener("online", onOnline);
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 15000);
    return () => {
      stopped = true; window.clearInterval(timer);
      for (const name of events) window.removeEventListener(`recording-ws:${name}`, onEvent);
      window.removeEventListener("recording-ws:__open", onOpen);
      window.removeEventListener("online", onOnline);
    };
  }, [sessionId, accept]);
  const mutate = useCallback(async (path: string, method: string, body: Record<string, unknown>) => {
    if (!sessionId) throw new Error("セッションがありません");
    try {
      const next = await liveRequest<LiveSnapshot>(`${sessionPath(sessionId)}${path}`, method, body);
      mutationGeneration.current++;
      accept(next); return next;
    } catch (cause) { void refreshRef.current(); throw cause; }
  }, [sessionId, accept]);
  return { snapshot, error, unavailable, connected, refresh: () => refreshRef.current(), mutate };
}
export type LiveNotesController = ReturnType<typeof useLiveNotes>;

interface DurableDraft {
  version: 1; value: Record<string, string>; revision: number; request_id: string;
}
export function draftKey(sessionId: string, entity: string, id: string) {
  return `seam:live-note-draft:v1:${sessionId}:${entity}:${id}`;
}
function readDraft(key: string): DurableDraft | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return value?.version === 1 && value.value && typeof value.revision === "number" ? value : null;
  } catch { return null; }
}
/** Requests remain idempotent across a dropped response/reload. A conflict never rebases
 * silently; only the user's explicit retry opts into the latest server revision. */
export function useDurableEdit({ sessionId, entity, id, revision, value, save }: {
  sessionId: string; entity: string; id: string; revision: number;
  value: Record<string, string>;
  save: (value: Record<string, string>, revision: number, requestId: string) => Promise<number>;
}) {
  const key = draftKey(sessionId, entity, id);
  const [draft, setDraft] = useState<DurableDraft | null>(() => readDraft(key));
  const draftRef = useRef(draft);
  const latestRef = useRef({ revision, value, save }); latestRef.current = { revision, value, save };
  const [status, setStatus] = useState(""), [storageError, setStorageError] = useState(false);
  const busy = useRef(false), mounted = useRef(true), composing = useRef(false);
  const timer = useRef<number | null>(null);
  const commitRef = useRef<(rebase?: boolean) => Promise<void>>(async () => {});
  const persist = (next: DurableDraft | null) => {
    // A delayed response from an unmounted editor must not erase a new editor's draft.
    if (!mounted.current) { const stored = readDraft(key); if (stored && stored.request_id !== draftRef.current?.request_id) return; }
    draftRef.current = next;
    if (mounted.current) setDraft(next);
    try { if (next) localStorage.setItem(key, JSON.stringify(next)); else localStorage.removeItem(key); if (mounted.current) setStorageError(false); }
    catch { if (mounted.current) setStorageError(true); }
  };
  const schedule = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { if (!composing.current) void commitRef.current(); }, 500);
  };
  commitRef.current = async (rebase = false) => {
    if (busy.current || composing.current || !draftRef.current) return;
    const pending = rebase ? { ...draftRef.current, revision: latestRef.current.revision, request_id: newRequestId() } : draftRef.current;
    if (rebase) persist(pending);
    busy.current = true;
    if (mounted.current) setStatus("保存中…");
    try {
      const nextRevision = await latestRef.current.save(pending.value, pending.revision, pending.request_id);
      if (draftRef.current?.request_id === pending.request_id) persist(null);
      else if (draftRef.current) { persist({ ...draftRef.current, revision: nextRevision }); schedule(); }
      if (mounted.current) setStatus("保存済み");
    } catch (cause) {
      if (mounted.current) setStatus(cause instanceof LiveApiError && cause.status === 409
        ? "競合：入力を保持しています。最新の内容を確認して再保存してください。"
        : `未保存：${cause instanceof Error ? cause.message : "接続に失敗しました"}`);
    } finally { busy.current = false; }
  };
  useEffect(() => {
    mounted.current = true;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (draftRef.current) { void commitRef.current(); event.preventDefault(); event.returnValue = ""; }
    };
    const onHidden = () => { if (document.visibilityState === "hidden") void commitRef.current(); };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("visibilitychange", onHidden);
    return () => {
      mounted.current = false;
      if (timer.current !== null) window.clearTimeout(timer.current);
      void commitRef.current();
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("visibilitychange", onHidden);
    };
  }, []);
  const update = (nextValue: Record<string, string>) => {
    persist({ version: 1, value: nextValue, revision: draftRef.current?.revision ?? latestRef.current.revision, request_id: newRequestId() });
    setStatus("未保存"); schedule();
  };
  return {
    value: draft?.value ?? value, dirty: !!draft, status, storageError,
    conflict: !!draft && draft.revision !== revision,
    update, retry: () => commitRef.current(draftRef.current?.revision !== latestRef.current.revision), saveNow: () => commitRef.current(),
    compositionStart: () => { composing.current = true; },
    compositionEnd: () => { composing.current = false; if (draftRef.current) schedule(); },
  };
}

export function useNewNoteDraft(key: string) {
  const load = () => {
    try { const saved = JSON.parse(localStorage.getItem(key) || "null"); if (saved && typeof saved.text === "string" && typeof saved.request_id === "string") return saved; } catch {}
    return { text: "", request_id: newRequestId() };
  };
  const [draft, setDraft] = useState<{ text: string; request_id: string }>(load);
  useEffect(() => { setDraft(load()); }, [key]);
  const [storageError, setStorageError] = useState(false);
  const update = (text: string) => {
    const next = { text, request_id: newRequestId() };
    setDraft(next);
    try { localStorage.setItem(key, JSON.stringify(next)); setStorageError(false); } catch { setStorageError(true); }
  };
  const clear = () => {
    setDraft({ text: "", request_id: newRequestId() });
    try { localStorage.removeItem(key); setStorageError(false); } catch { setStorageError(true); }
  };
  return { ...draft, update, clear, storageError };
}

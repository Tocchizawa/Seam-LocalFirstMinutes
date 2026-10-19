import { useEffect, useRef, useState, type ReactNode } from "react";
import { LiveNoteDocumentEditor } from "./LiveNoteDocumentEditor";
import {
  downloadLiveMarkdown, liveRequest, newRequestId, sessionPath,
  type LiveEvent, type LiveNotesController,
} from "../../lib/live-notes";
import "./live-notes.css";

export function LiveNotesPanel({ controller, transcript }: { controller: LiveNotesController; transcript?: ReactNode }) {
  const { snapshot, error, storageError, connected, mutate } = controller;
  const [tab, setTab] = useState<"note" | "history">("note");
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const lastRevision = useRef<number | null>(null);
  const [updates, setUpdates] = useState(0);
  useEffect(() => {
    if (snapshot && lastRevision.current !== null && snapshot.note_revision > lastRevision.current && tab !== "note") {
      setUpdates((value) => value + snapshot.note_revision - (lastRevision.current ?? snapshot.note_revision));
    }
    lastRevision.current = snapshot?.note_revision ?? null;
  }, [snapshot?.note_revision, tab]);
  useEffect(() => {
    if (tab !== "history" || !snapshot) return;
    let cancelled = false;
    liveRequest<{ events: LiveEvent[]; total: number }>(`${sessionPath(snapshot.session_id)}/history`)
      .then((result) => { if (!cancelled) { setEvents(result.events); setHistoryTotal(result.total); } })
      .catch((cause) => { if (!cancelled) setActionError(String(cause)); });
    return () => { cancelled = true; };
  }, [tab, snapshot?.session_id, snapshot?.note_revision]);
  if (!snapshot) return <div className="live-note-empty">{error || "ノートを読み込み中…"}</div>;
  const sessionId = snapshot.session_id;
  const run = async (action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true); setActionError(null);
    try { await action(); } catch (cause) { setActionError(cause instanceof Error ? cause.message : "操作に失敗しました。入力は保持されます。"); }
    finally { setBusy(false); }
  };
  const control = (action: string) => run(() => mutate("/control", "POST", { action }));
  const paused = (snapshot as unknown as { paused?: boolean }).paused || snapshot.status === "paused";
  const status = snapshot.status === "completed" || snapshot.status === "stopped" ? (snapshot.processing ? "会議終了 · 残りの議論を整理中" : "会議終了") : paused ? "AIを一時停止中" : snapshot.processing ? "AIが議論を整理中" : snapshot.status === "error" ? "AIエラー · 録音・手動編集は継続できます" : snapshot.status === "completed" || snapshot.status === "stopped" ? "会議終了" : "発話を待機中";
  return (
    <section className="live-notes-panel" aria-label="リアルタイム議論ノート">
      <div className="live-note-status" role="status" aria-live="polite">
        <span className={`live-note-status-dot ${snapshot.processing ? "processing" : ""}`} />
        <span>{status}</span><span className="live-note-provider">{snapshot.provider}{snapshot.model ? ` · ${snapshot.model}` : ""}</span>
        <span>未整理 {snapshot.pending_segments ?? 0}件 · 呼び出し {snapshot.llm_calls ?? 0}回</span>
        {snapshot.stopped && snapshot.pending_segments > 0 && <span>整理残りあり</span>}
        {(snapshot.queue_age_sec ?? 0) > 60 && <span>更新が遅れています · 最古の未整理発話 {Math.floor(snapshot.queue_age_sec ?? 0)}秒待機</span>}
        {!connected && <span>再接続待ち · 下書きは端末に保持</span>}
        {storageError && <span role="alert">最新ノートを端末に保存できません。空き容量やブラウザーの保存設定を確認してください。</span>}
      </div>
      <div className="live-note-privacy">音声認識はローカルで実行。発話とノートの文脈は選択したクラウドLLMへ送信されます。</div>
      {(error || snapshot.last_error || actionError) && <div className="live-note-error" role="alert">{actionError || error || snapshot.last_error}<button className="btn" onClick={() => { void controller.refresh(); }}>状態を再取得</button></div>}
      <div className="live-note-nav">
        <div role="tablist" aria-label="会議の表示">
          <button id={`note-tab-${sessionId}`} role="tab" aria-selected={tab === "note"} aria-controls={`note-panel-${sessionId}`} onClick={() => { setTab("note"); setUpdates(0); }}>ノート{updates > 0 ? ` +${updates}` : ""}</button>
          <button id={`history-tab-${sessionId}`} role="tab" aria-selected={tab === "history"} aria-controls={`history-panel-${sessionId}`} onClick={() => setTab("history")}>更新履歴</button>
        </div>
        <div className="live-note-actions">
          <button className="btn" disabled={busy} onClick={() => { void control(paused ? "resume" : "pause"); }}>{paused ? "AIを再開" : "AIを一時停止"}</button>
          <button className="btn" disabled={busy || snapshot.processing} onClick={() => { void control("retry"); }}>残りを整理 / 再試行</button>
          <button className="btn" disabled={busy} onClick={() => { void run(() => downloadLiveMarkdown(sessionId)); }}>Markdown出力</button>
        </div>
      </div>
      <div id={`note-panel-${sessionId}`} role="tabpanel" aria-labelledby={`note-tab-${sessionId}`} hidden={tab !== "note"} className="live-note-body seam-v2-note-body">
        <LiveNoteDocumentEditor controller={controller} snapshot={snapshot} onError={setActionError} />
        {transcript && <details className="live-note-transcript-disclosure"><summary>文字起こしを表示</summary><div>{transcript}</div></details>}
      </div>
      <div id={`history-panel-${sessionId}`} role="tabpanel" aria-labelledby={`history-tab-${sessionId}`} hidden={tab !== "history"} className="live-note-history">
        <h3>更新履歴</h3><p>手動操作とAIの更新を保存しています。元に戻す操作も履歴に残ります。</p>
        {events.length === 0 ? <p>更新履歴はまだありません。</p> : events.map((event) => <article key={event.id}>
          <div><strong>{event.actor === "human" ? "手動" : event.actor === "ai" ? "AI" : event.actor}</strong> · {event.operation_type || eventActions(event)} · rev {event.note_revision ?? event.revision}<time>{event.created_at}</time></div>
          <details><summary>変更内容</summary><OperationDetails value={event.operation_json ? parseJson(event.operation_json) : event.after} /></details>
          {!event.undone && event.undo_json && <button className="btn" disabled={busy} onClick={() => { void run(() => mutate(`/history/${encodeURIComponent(event.id)}/undo`, "POST", { expected_revision: snapshot.note_revision, request_id: newRequestId() })); }}>元に戻す</button>}
        </article>)}
        {events.length < historyTotal && <button className="btn" disabled={busy} onClick={() => { void run(async () => {
          const page = await liveRequest<{ events: LiveEvent[]; total: number }>(`${sessionPath(sessionId)}/history?offset=${events.length}`);
          setEvents((previous) => [...previous, ...page.events.filter((item) => !previous.some((existing) => existing.id === item.id))]); setHistoryTotal(page.total);
        }); }}>以前の履歴を読み込む（{events.length}/{historyTotal}）</button>}
      </div>
    </section>
  );
}

function parseJson(value: string | undefined): unknown {
  try { return value ? JSON.parse(value) : null; } catch { return value; }
}
function eventActions(event: LiveEvent): string {
  const operations = parseJson(event.operation_json);
  return Array.isArray(operations) ? operations.map((operation) => actionLabel(operation.action)).join(" / ") : typeof event.operation === "string" ? event.operation : "更新";
}
const ACTION_LABELS: Record<string, string> = { create_topic:"見出しを追加", add_block:"メモを追加", update_block:"メモを更新", edit_block:"メモを手動編集", rename_topic:"見出しを更新", edit_topic:"見出しを手動編集", delete_topic:"論点を削除", delete_block:"メモを削除", reorder:"論点を並べ替え", set_active_topic:"議論中の論点を変更", undo:"変更を元に戻す", resolve:"保留提案を確認", suggestions:"競合案を保留" };
function actionLabel(action: unknown): string { return typeof action === "string" ? ACTION_LABELS[action] || "ノートを更新" : "ノートを更新"; }
function OperationDetails({value}: {value:unknown}) {
  const operations = Array.isArray(value) ? value : [value];
  return <div>{operations.filter((operation) => operation && typeof operation === "object").map((operation, index) => {
    const fields = operation.request || operation.block || operation;
    const content = fields.markdown || fields.content_md;
    return <div key={index} className="live-note-operation"><strong>{actionLabel(operation.action)}</strong>{fields.title && <p>{String(fields.title)}</p>}{content && <p className="whitespace-pre-wrap">{String(content)}</p>}{Array.isArray(fields.source_segment_seqs) && fields.source_segment_seqs.length > 0 && <small>根拠の発話: {fields.source_segment_seqs.join(", ")}</small>}</div>;
  })}</div>;
}

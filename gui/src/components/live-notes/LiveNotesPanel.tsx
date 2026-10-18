import { useEffect, useRef, useState, type ReactNode } from "react";
import { ask } from "@tauri-apps/plugin-dialog";
import { LiveNoteBlockEditor } from "./LiveNoteBlockEditor";
import {
  downloadLiveMarkdown, liveRequest, newRequestId, sessionPath, useDurableEdit, useNewNoteDraft,
  type LiveEvent, type LiveNotesController, type LiveSuggestion, type LiveTopic,
} from "../../lib/live-notes";
import "./live-notes.css";

export function LiveNotesPanel({ controller, transcript }: { controller: LiveNotesController; transcript?: ReactNode }) {
  const { snapshot, error, connected, mutate } = controller;
  const [tab, setTab] = useState<"note" | "transcript" | "history">("note");
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const topicDraft = useNewNoteDraft(`seam:live-new-topic:v1:${snapshot?.session_id ?? "loading"}`);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [search, setSearch] = useState("");
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
  const remove = async (path: string, revision: number, label: string) => {
    const message = `${label}を削除しますか？更新履歴から元に戻せます。未保存の入力は端末の下書きに残ります。`;
    const isDesktop = "__TAURI_INTERNALS__" in window;
    const confirmed = isDesktop ? await ask(message, {title:"議論ノートの削除",kind:"warning",okLabel:"削除",cancelLabel:"キャンセル"}) : window.confirm(message);
    if (!confirmed) return;
    void run(() => mutate(path, "DELETE", { expected_revision: revision, request_id: newRequestId() }));
  };
  const topics = [...snapshot.topics].sort((left, right) => left.position - right.position);
  const visibleTopics = search.trim() ? topics.filter((topic) => `${topic.title}\n${topic.blocks.map((block) => block.content_md).join("\n")}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())) : topics;
  const reorder = (index: number, delta: number) => {
    const ids = topics.map((topic) => topic.id);
    [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
    void run(() => mutate("/topics/reorder", "POST", { topic_ids: ids, expected_revision: snapshot.note_revision, request_id: newRequestId() }));
  };
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
      </div>
      <div className="live-note-privacy">音声認識はローカルで実行。発話とノートの文脈は選択したクラウドLLMへ送信されます。</div>
      {(error || snapshot.last_error || actionError) && <div className="live-note-error" role="alert">{actionError || error || snapshot.last_error}<button className="btn" onClick={() => { void controller.refresh(); }}>状態を再取得</button></div>}
      <div className="live-note-nav">
        <div role="tablist" aria-label="会議の表示">
          <button id={`note-tab-${sessionId}`} role="tab" aria-selected={tab === "note"} aria-controls={`note-panel-${sessionId}`} onClick={() => { setTab("note"); setUpdates(0); }}>ノート{updates > 0 ? ` +${updates}` : ""}</button>
          {transcript && <button id={`transcript-tab-${sessionId}`} role="tab" aria-selected={tab === "transcript"} aria-controls={`transcript-panel-${sessionId}`} onClick={() => setTab("transcript")}>文字起こし</button>}
          <button id={`history-tab-${sessionId}`} role="tab" aria-selected={tab === "history"} aria-controls={`history-panel-${sessionId}`} onClick={() => setTab("history")}>更新履歴</button>
        </div>
        <div className="live-note-actions">
          <button className="btn" disabled={busy} onClick={() => { void control(paused ? "resume" : "pause"); }}>{paused ? "AIを再開" : "AIを一時停止"}</button>
          <button className="btn" disabled={busy || snapshot.processing} onClick={() => { void control("retry"); }}>残りを整理 / 再試行</button>
          <button className="btn" disabled={busy} onClick={() => { void run(() => downloadLiveMarkdown(sessionId)); }}>Markdown出力</button>
        </div>
      </div>
      <div id={`note-panel-${sessionId}`} role="tabpanel" aria-labelledby={`note-tab-${sessionId}`} hidden={tab !== "note"} className="live-note-body">
        <aside className="live-note-outline" aria-label="議論の目次">
          <label htmlFor={`live-search-${sessionId}`}>目次 · {topics.length}論点</label>
          <input id={`live-search-${sessionId}`} className="input" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="論点・メモを検索" />
          <nav>{visibleTopics.map((topic) => <a key={topic.id} href={`#live-topic-${topic.id}`} onClick={(event) => { event.preventDefault(); document.getElementById(`live-topic-${topic.id}`)?.scrollIntoView({ block: "start" }); }} aria-current={topic.id === snapshot.active_topic_id ? "location" : undefined}>{topic.title}<span>{topic.blocks.length}</span></a>)}</nav>
        </aside>
        <div className="live-note-document">
          {snapshot.suggestions?.filter((suggestion) => !suggestion.status || suggestion.status === "pending").map((suggestion) => <Suggestion key={suggestion.id} suggestion={suggestion} busy={busy} resolve={(action) => { void run(() => mutate(`/suggestions/${encodeURIComponent(suggestion.id)}/resolve`, "POST", { action, expected_revision: snapshot.note_revision, request_id: newRequestId() })); }} />)}
          {topics.length === 0 && <div className="live-note-empty"><h3>会議の議論がここに蓄積されます</h3><p>確定した発話をまとめて整理します。待機中も見出しとメモを手動で追加できます。</p></div>}
          {topics.map((topic, index) => <TopicSection key={topic.id} topic={topic} sessionId={sessionId} controller={controller} busy={busy} hidden={!visibleTopics.includes(topic)}
            onDelete={() => { void remove(`/topics/${encodeURIComponent(topic.id)}`, topic.revision, "この論点とメモ"); }}
            onDeleteBlock={(id, revision) => { void remove(`/blocks/${encodeURIComponent(id)}`, revision, "このメモ"); }}
            onMoveUp={index > 0 ? () => reorder(index, -1) : undefined}
            onMoveDown={index < topics.length - 1 ? () => reorder(index, 1) : undefined}
            run={run} />)}
          <form className="live-note-add-topic" onSubmit={(event) => { event.preventDefault(); const title = topicDraft.text.trim(); if (title) void run(async () => { await mutate("/topics", "POST", { title, request_id: topicDraft.request_id }); topicDraft.clear(); }); }}>
            <label className="sr-only" htmlFor={`new-topic-${sessionId}`}>新しい論点の見出し</label>
            <input id={`new-topic-${sessionId}`} className="input" placeholder="新しい論点の見出し" value={topicDraft.text} onChange={(event) => topicDraft.update(event.target.value)} maxLength={120} disabled={busy} />
            <button type="submit" className="btn" disabled={busy || !topicDraft.text.trim()}>見出しを追加</button>
          </form>
          {topicDraft.storageError && <p role="alert">追加中の見出しを端末に保存できませんでした。画面を閉じずに追加してください。</p>}
        </div>
      </div>
      {transcript && <div id={`transcript-panel-${sessionId}`} role="tabpanel" aria-labelledby={`transcript-tab-${sessionId}`} hidden={tab !== "transcript"} className="live-note-transcript">{transcript}</div>}
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

function TopicSection({ sessionId, topic, controller, busy, hidden, onDelete, onDeleteBlock, onMoveUp, onMoveDown, run }: {
  sessionId: string; topic: LiveTopic; controller: LiveNotesController; busy: boolean; hidden: boolean;
  onDelete: () => void; onDeleteBlock: (id: string, revision: number) => void;
  onMoveUp?: () => void; onMoveDown?: () => void; run: (action: () => Promise<unknown>) => Promise<void>;
}) {
  const title = useDurableEdit({ sessionId, entity: "topic", id: topic.id, revision: topic.revision, value: { title: topic.title },
    save: async (value, revision, requestId) => {
      const next = await controller.mutate(`/topics/${encodeURIComponent(topic.id)}`, "PATCH", { ...value, expected_revision: revision, request_id: requestId });
      return next.topics.find((row) => row.id === topic.id)?.revision ?? revision;
    },
  });
  const blockDraft = useNewNoteDraft(`seam:live-new-block:v1:${sessionId}:${topic.id}`);
  return <section id={`live-topic-${topic.id}`} hidden={hidden} className="live-note-topic" data-topic-id={topic.id}>
    <div className="live-note-topic-heading">
      <label className="sr-only" htmlFor={`topic-title-${topic.id}`}>論点の見出し</label>
      <input id={`topic-title-${topic.id}`} aria-label="論点の見出し" value={title.value.title} maxLength={120} onChange={(event) => title.update({ title: event.target.value })} onCompositionStart={title.compositionStart} onCompositionEnd={title.compositionEnd} />
      <span className="live-note-owner">{topic.title_owner === "human" ? "手動" : "AI"}</span>
      <button className="btn btn-ghost" aria-label="論点を上へ移動" disabled={busy || !onMoveUp} onClick={onMoveUp}>↑</button>
      <button className="btn btn-ghost" aria-label="論点を下へ移動" disabled={busy || !onMoveDown} onClick={onMoveDown}>↓</button>
      <button className="btn btn-ghost" aria-label="論点を削除" disabled={busy} onClick={onDelete}>削除</button>
    </div>
    <div className="live-note-save-state" aria-live="polite">{title.storageError ? "端末の下書き保存に失敗しました" : title.status}{title.dirty && <button className="btn btn-ghost" onClick={() => { void title.retry(); }}>見出しを再保存</button>}</div>
    {title.conflict && <p role="alert" className="live-note-conflict">見出しが更新されました。サーバー: {topic.title}。入力は保持されています。</p>}
    {[...topic.blocks].sort((left, right) => left.position - right.position).map((block) => <LiveNoteBlockEditor key={block.id} sessionId={sessionId} block={block} controller={controller} onDelete={() => onDeleteBlock(block.id, block.revision)} />)}
    <form className="live-note-add-block" onSubmit={(event) => { event.preventDefault(); const content = blockDraft.text.trim(); if (content) void run(async () => { await controller.mutate("/blocks", "POST", { topic_id: topic.id, content_md: content, kind: "point", request_id: blockDraft.request_id }); blockDraft.clear(); }); }}>
      <label className="sr-only" htmlFor={`new-block-${topic.id}`}>追加するメモ</label>
      <textarea id={`new-block-${topic.id}`} className="input" placeholder="この論点にメモを追加（Markdown）" value={blockDraft.text} disabled={busy} rows={2} onChange={(event) => blockDraft.update(event.target.value)} />
      <button className="btn" type="submit" disabled={busy || !blockDraft.text.trim()}>メモを追加</button>
    </form>
    {blockDraft.storageError && <p role="alert">追加中のメモを端末に保存できませんでした。画面を閉じずに追加してください。</p>}
  </section>;
}
function Suggestion({ suggestion, busy, resolve }: { suggestion: LiveSuggestion; busy: boolean; resolve: (action: string) => void }) {
  return <aside className="live-note-conflict" role="note"><strong>AIの保留提案 · 手動編集を保護しました</strong><p>{suggestion.reason}</p>
    <details><summary>提案の内容を確認</summary><OperationDetails value={suggestion.operation || suggestion.proposed_operation || parseJson(suggestion.suggested_operation_json)} /></details>
    <button className="btn" disabled={busy} onClick={() => resolve("accept")}>内容を確認して採用</button> <button className="btn btn-ghost" disabled={busy} onClick={() => resolve("dismiss")}>見送る</button>
  </aside>;
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

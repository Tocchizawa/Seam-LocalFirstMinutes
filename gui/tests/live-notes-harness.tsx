/** Browser-only synthetic fixture. No microphone, files, CLI, or cloud request. */
import { createRoot } from "react-dom/client";
import { useState } from "react";
import { LiveNotesPanel } from "../src/components/live-notes/LiveNotesPanel";
import { DetailView } from "../src/pages/DetailView";
import type { Minutes } from "../src/lib/api";
import { LiveNotesSettings } from "../src/components/live-notes/LiveNotesSettings";
import { LIVE_DEFAULTS, useLiveNotes, type LiveEvent, type LiveSnapshot, type LiveTopic } from "../src/lib/live-notes";
import "../src/App.css";

let snapshot: LiveSnapshot = {
  session_id: "test-session", provider: "codex", status: "recording", note_revision: 1,
  last_processed_segment_seq: 1, pending_segments: 0, processing: false, llm_calls: 1, suggestions: [],
  topics: [{ id: "topic-a", title: "認証方式", title_owner: "ai", revision: 0, position: 0,
    blocks: [{ id: "block-a", topic_id: "topic-a", kind: "proposal", owner: "ai", revision: 0, position: 0, content_md: "既存の認証基盤を利用する案", source_segment_seqs: [1] }] }],
};
try { const saved = sessionStorage.getItem("seam:test-snapshot"); if (saved) snapshot = JSON.parse(saved); } catch {}
const legacyMinutes: Minutes = {
  id: "legacy-minutes", session_id: "legacy-session", project_id: "test-project", title: "従来の議事録", date: "2026-10-17", started_at: "2026-10-17T09:00:00", duration_sec: 0,
  transcript: [{start:0,end:10,text:"従来の文字起こし本文"}], summary:"## 従来の要約\n既存の議事録を表示します。", created_at:"2026-10-17T09:00:00", llm_model:"ollama:test",
};
let offline = sessionStorage.getItem("seam:test-offline") === "true", delay = false, snapshotDelay = false, rejectSuggestionResolution = false, history: LiveEvent[] = [], previous: LiveSnapshot | null = null;
Object.defineProperty(window, "__seamFixture", { configurable: true, get: () => snapshot });
const requests = new Map<string, LiveSnapshot>();
const requestLog: Array<{ method: string; path: string; status?: number; error?: string }> = [];
Object.defineProperty(window, "__seamFixtureActions", { configurable: true, value: { aiUpdate, requestLog: () => requestLog, seedThreeUnits, deleteMiddleAndUpdateLast, seedSuggestion, rejectNextSuggestionResolution, seedV3Document } });
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const publish = () => window.dispatchEvent(new CustomEvent("recording-ws:live_note_patch", { detail: { session_id: snapshot.session_id, revision: snapshot.note_revision } }));
const commit = (action: string, actor = "human", notify = true) => {
  snapshot.note_revision++;
  history.unshift({ id: `event-${snapshot.note_revision}`, actor, note_revision: snapshot.note_revision, operation_json: JSON.stringify([{ action }]), undo_json: previous ? JSON.stringify(previous) : undefined, created_at: new Date().toISOString() });
  sessionStorage.setItem("seam:test-snapshot", JSON.stringify(snapshot));
  if (notify) publish();
};
window.fetch = async (input, init) => {
  const url = new URL(String(input));
  if (url.hostname !== "127.0.0.1" || url.port !== "18900") throw new Error("Test fixture blocks non-local API calls");
  const request: { method: string; path: string; status?: number; error?: string } = { method: init?.method || "GET", path: url.pathname };
  requestLog.push(request);
  if (!url.pathname.startsWith("/api/live-notes")) {
    let result: unknown = {};
    if (url.pathname.endsWith("/pipelines")) result = [];
    else if (url.pathname.endsWith("/summarize/status")) result = { state: "none" };
    else if (url.pathname.endsWith("/summary") && init?.method === "PATCH") { legacyMinutes.summary = JSON.parse(String(init.body)).summary; result = legacyMinutes; }
    else if (url.pathname === "/api/minutes/legacy-minutes") result = legacyMinutes;
    else if (url.pathname.endsWith("/markdown")) result = { content: legacyMinutes.summary };
    else if (url.pathname.endsWith("/export")) result = { status: "ok", path: "/synthetic/legacy.md" };
    return new Response(JSON.stringify(result), {headers:{"Content-Type":"application/json"}});
  }
  if (url.pathname === "/api/live-notes/legacy-session") return new Response(JSON.stringify({detail:"No live note"}), {status:404,headers:{"Content-Type":"application/json"}});
  if (offline) { request.error = "Synthetic network disconnected"; throw new TypeError("Synthetic network disconnected"); }
  const method = init?.method || "GET";
  const path = url.pathname.replace("/api/live-notes/test-session", "");
  const body = init?.body ? JSON.parse(String(init.body)) : {};
  if (method !== "GET" && delay) await new Promise((resolve) => setTimeout(resolve, 2000));
  const json = (value: unknown, status = 200) => { request.status = status; return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); };
  if (url.pathname.endsWith("/preflight")) return json({ ok: true, test_fixture: true });
  if (method === "GET") {
    if (path === "/history") return json({ events: history.slice(Number(url.searchParams.get("offset") || 0), Number(url.searchParams.get("offset") || 0) + 50), total: history.length });
    if (path === "/export.md") return new Response(snapshot.topics.map((topic) => `## ${topic.title}\n${topic.blocks.map((block) => block.content_md).join("\n")}`).join("\n\n"));
    const captured = clone(snapshot);
    if (snapshotDelay) { snapshotDelay = false; await new Promise((resolve) => setTimeout(resolve, 400)); }
    return json(captured);
  }
  if (body.request_id && requests.has(body.request_id)) return path === "/document/edits" ? json({ snapshot: requests.get(body.request_id), id_map: {} }) : json(requests.get(body.request_id));
  if (path.startsWith("/suggestions/") && path.endsWith("/resolve")) {
    if (rejectSuggestionResolution) { rejectSuggestionResolution = false; return json({ detail: { code: "REVISION_CONFLICT", message: "版数が更新されました" } }, 409); }
    const suggestionId = path.split("/")[2];
    const suggestion = snapshot.suggestions.find((item) => item.id === suggestionId);
    if (suggestion) suggestion.status = body.action === "accept" ? "accepted" : "dismissed";
    commit(`resolve_suggestion_${body.action}`);
    if (body.request_id) requests.set(body.request_id, clone(snapshot));
    return json(clone(snapshot));
  }
  previous = clone(snapshot);
  if (path === "/document/edits") {
    const idMap: Record<string, string> = {};
    for (const operation of body.operations || []) {
      const targetTopic = snapshot.topics.find((row) => row.id === operation.id);
      const targetBlock = snapshot.topics.flatMap((row) => row.blocks).find((row) => row.id === operation.id);
      if (operation.op === "update_document_title" && Number(snapshot.document?.title_revision ?? 0) !== operation.expected_revision) return json({ detail: { code: "REVISION_CONFLICT", message: "タイトルが更新されました" } }, 409);
      if (operation.op === "update_document_takeaway" && snapshot.note_revision !== operation.expected_revision) return json({ detail: { code: "REVISION_CONFLICT", message: "要点が更新されました" } }, 409);
      if (operation.op === "update_topic" && targetTopic && targetTopic.revision !== operation.expected_revision) return json({ detail: { code: "REVISION_CONFLICT", message: "見出しが更新されました" } }, 409);
      if (operation.op === "update_block" && targetBlock && targetBlock.revision !== operation.expected_revision) return json({ detail: { code: "REVISION_CONFLICT", message: "本文が更新されました" } }, 409);
    }
    for (const operation of body.operations || []) {
      const targetTopic = snapshot.topics.find((row) => row.id === operation.id);
      const targetBlock = snapshot.topics.flatMap((row) => row.blocks).find((row) => row.id === operation.id);
      if (operation.op === "update_document_title") { snapshot.document = { ...snapshot.document, title: operation.title, title_revision: Number(snapshot.document?.title_revision ?? 0) + 1 }; }
      else if (operation.op === "update_document_takeaway") { snapshot.document = { ...snapshot.document, takeaway: operation.takeaway }; }
      else if (operation.op === "update_topic" && targetTopic) { targetTopic.title = operation.title; targetTopic.title_owner = "human"; targetTopic.revision++; }
      else if (operation.op === "update_block" && targetBlock) { targetBlock.content_md = operation.markdown; targetBlock.owner = "human"; targetBlock.revision++; }
      else if (operation.op === "delete_block" && targetBlock) snapshot.topics.forEach((row) => { row.blocks = row.blocks.filter((entry) => entry.id !== targetBlock.id); });
      else if (operation.op === "delete_topic" && targetTopic) snapshot.topics = snapshot.topics.filter((row) => row.id !== targetTopic.id);
      else if (operation.op === "create_topic") { const id = `topic-${operation.client_id}`; idMap[operation.client_id] = id; snapshot.topics.push({ id, title: operation.title, title_owner: "human", revision: 0, position: Number(operation.position ?? snapshot.topics.length), blocks: [] }); }
      else if (operation.op === "create_block") { const topicId = operation.topic_id || idMap[operation.client_topic_id]; const parent = snapshot.topics.find((row) => row.id === topicId); if (parent) { const id = `block-${operation.client_id}`; idMap[operation.client_id] = id; parent.blocks.push({ id, topic_id: topicId, content_md: operation.markdown, kind: operation.kind, owner: "human", revision: 0, position: Number(operation.position ?? parent.blocks.length), source_segment_seqs: [] }); } }
      else if (operation.op === "reorder_topics") snapshot.topics.forEach((row) => { row.position = operation.ids.indexOf(row.id); });
    }
    commit("document_edit");
    if (body.request_id) requests.set(body.request_id, clone(snapshot));
    return json({ snapshot: clone(snapshot), id_map: idMap });
  }
  const id = path.split("/")[2];
  const topic = snapshot.topics.find((row) => row.id === id);
  const block = snapshot.topics.flatMap((row) => row.blocks).find((row) => row.id === id);
  if (path.includes("/blocks/") || path.includes("/topics/") && path !== "/topics/reorder") {
    const target = block || topic;
    if (!target) return json({ detail: { message: "Not found" } }, 404);
    if (target.revision !== body.expected_revision) return json({ detail: { code: "REVISION_CONFLICT", message: "版数が更新されました", latest: target } }, 409);
  }
  if (method === "PATCH" && block) { block.content_md = body.content_md; block.kind = body.kind || block.kind; block.owner = "human"; block.revision++; }
  else if (method === "PATCH" && topic) { topic.title = body.title; topic.title_owner = "human"; topic.revision++; }
  else if (method === "DELETE" && block) snapshot.topics.forEach((row) => { row.blocks = row.blocks.filter((entry) => entry.id !== id); });
  else if (method === "DELETE" && topic) snapshot.topics = snapshot.topics.filter((row) => row.id !== id);
  else if (path === "/topics") snapshot.topics.push({ id: `topic-${body.request_id}`, title: body.title, title_owner: "human", revision: 0, position: snapshot.topics.length, blocks: [] });
  else if (path === "/blocks") snapshot.topics.find((row) => row.id === body.topic_id)?.blocks.push({ id: `block-${body.request_id}`, topic_id: body.topic_id, content_md: body.content_md, kind: body.kind, owner: "human", revision: 0, position: 99, source_segment_seqs: [] });
  else if (path === "/topics/reorder") snapshot.topics.forEach((row) => { row.position = body.topic_ids.indexOf(row.id); });
  else if (path === "/control") { snapshot.status = body.action === "pause" ? "paused" : "recording"; snapshot.processing = body.action === "retry"; }
  else if (path.endsWith("/undo") && previous) {
    const event = history.find((row) => row.id === id);
    if (event?.undo_json) snapshot = JSON.parse(event.undo_json);
  }
  commit(`${method} ${path}`);
  if (body.request_id) requests.set(body.request_id, clone(snapshot));
  return json(clone(snapshot));
};
function aiUpdate(same = false, notify = true) {
  previous = clone(snapshot);
  const topic = snapshot.topics.find((row) => row.id === "topic-a")!;
  if (same) { topic.blocks[0].content_md = "AIから遅れて届いた更新"; topic.blocks[0].revision++; }
  else { topic.blocks.push({ id: `block-ai-${snapshot.note_revision}`, topic_id: topic.id, kind: "concern", owner: "ai", revision: 0, position: topic.blocks.length, content_md: "既存基盤の移行期間を確認する", source_segment_seqs: [2] }); }
  snapshot.llm_calls++; snapshot.last_processed_segment_seq++;
  commit("add_block", "ai", notify);
}
function seedV3Document() {
  previous = clone(snapshot);
  snapshot.document = { title: "認証方式の整理", title_revision: 0, takeaway: "会議の合意を短くまとめます。", unresolved_count: 1, needs_verification_count: 1 };
  snapshot.topics = [
    { id: "topic-takeaway", title: "会議の要点", title_owner: "ai", revision: 0, position: 0, blocks: [{ id: "block-takeaway", topic_id: "topic-takeaway", kind: "context", owner: "ai", revision: 0, position: 0, content_md: "会議の合意を短くまとめます。", source_segment_seqs: [1] }] },
    { id: "topic-a", title: "認証方式", title_owner: "ai", revision: 0, position: 1, blocks: [{ id: "block-a", topic_id: "topic-a", kind: "proposal", owner: "ai", revision: 0, position: 0, content_md: "既存の認証基盤を利用する案", source_segment_seqs: [2] }] },
  ];
  commit("seed_v3_document", "ai");
}
function seedThreeUnits() {
  previous = clone(snapshot);
  const topic = snapshot.topics.find((row) => row.id === "topic-a")!;
  topic.blocks = [
    topic.blocks[0],
    { id: "remote-b", topic_id: topic.id, kind: "point", owner: "ai", revision: 0, position: 1, content_md: "中間のメモ", source_segment_seqs: [3] },
    { id: "remote-c", topic_id: topic.id, kind: "point", owner: "ai", revision: 0, position: 2, content_md: "最後のメモ", source_segment_seqs: [4] },
  ];
  commit("seed_remote_units", "ai");
}
function deleteMiddleAndUpdateLast() {
  previous = clone(snapshot);
  const topic = snapshot.topics.find((row) => row.id === "topic-a")!;
  topic.blocks = topic.blocks.filter((block) => block.id !== "remote-b");
  const last = topic.blocks.find((block) => block.id === "remote-c")!;
  last.content_md = "更新された最後のメモ";
  last.revision++;
  topic.blocks.forEach((block, index) => { block.position = index; });
  commit("delete_and_update_remote_units", "ai");
}
function seedSuggestion() {
  previous = clone(snapshot);
  snapshot.suggestions = [{
    id: "suggestion-review-a", status: "pending", reason: "この範囲に新しいAI更新があるため、確認が必要です。",
    suggested_operation_json: JSON.stringify({ op: "update_block", topic_id: "topic-a", block_id: "block-a", markdown: "推奨する認証方式を比較して決める。" }),
    presentation: { summary: "認証方式のメモを更新する提案です。", reason_code: "newer_ai_update", target_title: "認証方式", proposed_md: "推奨する認証方式を比較して決める。", source_segments: [{ seq: 7, start_sec: 24, text: "候補を比較してから方式を決めましょう。" }] },
  }];
  commit("seed_suggestion", "ai");
}
function rejectNextSuggestionResolution() { rejectSuggestionResolution = true; }
function setRichMarkdown() {
  previous = clone(snapshot);
  const block = snapshot.topics[0].blocks[0];
  block.content_md = "**要点**を整理します。\n\n- 確認済み\n- 保留中\n\n```ts\nconst ready = true\n```\n\n| 部品 | 役割 |\n| --- | --- |\n| NriDeps | 共通依存 |\n| NewNriPoint | 受付 |";
  block.revision++;
  commit("update_block", "ai");
}
function addTopic(title = "運用・監視") {
  previous = clone(snapshot);
  snapshot.topics.push({ id: `topic-ai-${snapshot.note_revision}`, title, title_owner: "ai", revision: 0, position: snapshot.topics.length, blocks: [] });
  commit("create_topic", "ai");
}
function Fixture() {
  const controller = useLiveNotes("test-session");
  const [settings, setSettings] = useState(false);
  const [legacy, setLegacy] = useState(false);
  const [config, setConfig] = useState(LIVE_DEFAULTS);
  return <div style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 5, padding: 6, background: "#f0f0f0" }} aria-label="合成試験の操作">
      <button className="btn" onClick={() => addTopic()}>新話題</button><button className="btn" onClick={() => aiUpdate()}>話題復帰 / 別ブロック更新</button><button className="btn" onClick={seedV3Document}>v3文書fixture</button>
      <button className="btn" onClick={() => aiUpdate(true)}>同一ブロック競合</button>
      <button className="btn" onClick={seedSuggestion}>提案レビューfixture</button>
      <button className="btn" onClick={rejectNextSuggestionResolution}>次の提案操作で409</button>
      <button className="btn" onClick={setRichMarkdown}>Markdown構造fixture</button>
      <button className="btn" onClick={() => { offline = true; sessionStorage.setItem("seam:test-offline", "true"); publish(); }}>接続断</button>
      <button className="btn" onClick={() => { offline = false; sessionStorage.removeItem("seam:test-offline"); window.dispatchEvent(new CustomEvent("recording-ws:__open")); }}>再接続</button>
      <button className="btn" onClick={() => { delay = !delay; }}>応答遅延切替</button>
      <button className="btn" onClick={() => { snapshot.status = "completed"; snapshot.processing = false; snapshot.minutes_id = "minutes-test"; commit("stop"); }}>停止</button>
      <button className="btn" onClick={() => { for (let i = 0; i < 21; i++) addTopic(`論点 ${i + 1}`); for (let i = 0; i < 39; i++) aiUpdate(); }}>60分相当リプレイ</button>
      <button className="btn" onClick={() => { snapshotDelay = true; window.dispatchEvent(new CustomEvent("recording-ws:__open")); }}>遅いスナップショット</button>
      <button className="btn" onClick={() => setLegacy((value) => !value)}>従来議事録切替</button>
      <button className="btn" onClick={() => setSettings((value) => !value)}>設定表示切替</button>
      <button className="btn" onClick={() => { document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; }}>テーマ切替</button>
    </div>
    {legacy ? <DetailView minutes={legacyMinutes} onBack={() => setLegacy(false)} /> : settings ? <div style={{ overflow: "auto", padding: 20 }}><LiveNotesSettings value={config} onChange={setConfig} inheritedProvider="codex" recording={true} codexModels={location.search.includes("empty-models") ? [] : [{ slug: "gpt-6-sol", display_name: "GPT-6 Sol" }, { slug: "gpt-6-luna", display_name: "GPT-6 Luna" }]} claudeModels={[{ slug: "sonnet", display_name: "Sonnet" }, { slug: "haiku", display_name: "Haiku" }]} inheritedModels={{ codex: "gpt-6-sol", claude_code: "sonnet" }} /></div> : <div style={{ flex: 1, minHeight: 0 }}><LiveNotesPanel controller={controller} transcript={<div style={{ padding: 20 }}>確定発話：認証基盤の検討を続けます。音声録音の代わりの合成テキストです。</div>} /></div>}
  </div>;
}
// DetailView's legacy subscription is stubbed too; no actual local WS connection.
window.WebSocket = class extends EventTarget { close() {} } as unknown as typeof WebSocket;
createRoot(document.getElementById("root")!).render(<Fixture />);

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
let offline = false, delay = false, snapshotDelay = false, history: LiveEvent[] = [], previous: LiveSnapshot | null = null;
const requests = new Map<string, LiveSnapshot>();
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const publish = () => window.dispatchEvent(new CustomEvent("recording-ws:live_note_patch", { detail: { session_id: snapshot.session_id, revision: snapshot.note_revision } }));
const commit = (action: string, actor = "human") => {
  snapshot.note_revision++;
  history.unshift({ id: `event-${snapshot.note_revision}`, actor, note_revision: snapshot.note_revision, operation_json: JSON.stringify([{ action }]), undo_json: previous ? JSON.stringify(previous) : undefined, created_at: new Date().toISOString() });
  sessionStorage.setItem("seam:test-snapshot", JSON.stringify(snapshot));
  publish();
};
window.fetch = async (input, init) => {
  const url = new URL(String(input));
  if (url.hostname !== "127.0.0.1" || url.port !== "18900") throw new Error("Test fixture blocks non-local API calls");
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
  if (offline) throw new TypeError("Synthetic network disconnected");
  const method = init?.method || "GET";
  const path = url.pathname.replace("/api/live-notes/test-session", "");
  const body = init?.body ? JSON.parse(String(init.body)) : {};
  if (method !== "GET" && delay) await new Promise((resolve) => setTimeout(resolve, 2000));
  const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  if (url.pathname.endsWith("/preflight")) return json({ ok: true, test_fixture: true });
  if (method === "GET") {
    if (path === "/history") return json({ events: history.slice(Number(url.searchParams.get("offset") || 0), Number(url.searchParams.get("offset") || 0) + 50), total: history.length });
    if (path === "/export.md") return new Response(snapshot.topics.map((topic) => `## ${topic.title}\n${topic.blocks.map((block) => block.content_md).join("\n")}`).join("\n\n"));
    const captured = clone(snapshot);
    if (snapshotDelay) { snapshotDelay = false; await new Promise((resolve) => setTimeout(resolve, 400)); }
    return json(captured);
  }
  if (body.request_id && requests.has(body.request_id)) return json(requests.get(body.request_id));
  previous = clone(snapshot);
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
function aiUpdate(same = false) {
  previous = clone(snapshot);
  const topic = snapshot.topics.find((row) => row.id === "topic-a")!;
  if (same) { topic.blocks[0].content_md = "AIから遅れて届いた更新"; topic.blocks[0].revision++; }
  else { topic.blocks.push({ id: `block-ai-${snapshot.note_revision}`, topic_id: topic.id, kind: "concern", owner: "ai", revision: 0, position: topic.blocks.length, content_md: "既存基盤の移行期間を確認する", source_segment_seqs: [2] }); }
  snapshot.llm_calls++; snapshot.last_processed_segment_seq++;
  commit("add_block", "ai");
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
      <button className="btn" onClick={() => addTopic()}>新話題</button><button className="btn" onClick={() => aiUpdate()}>話題復帰 / 別ブロック更新</button>
      <button className="btn" onClick={() => aiUpdate(true)}>同一ブロック競合</button>
      <button className="btn" onClick={() => { offline = true; publish(); }}>接続断</button>
      <button className="btn" onClick={() => { offline = false; window.dispatchEvent(new CustomEvent("recording-ws:__open")); }}>再接続</button>
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

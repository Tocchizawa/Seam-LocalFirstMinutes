import { useEffect, useState } from "react";
import { listLiveSessions, type LiveSessionListItem } from "../../lib/live-notes";

/** Notes survive independently of recoverable audio/minutes. Keep an explicit route
 * to unlinked sessions rather than hiding them when audio recovery cannot succeed. */
export function LiveNotesRecovery({ projectId, activeSessionId, onOpen }: {
  projectId: string; activeSessionId: string | null; onOpen: (sessionId: string) => void;
}) {
  const [sessions, setSessions] = useState<LiveSessionListItem[]>([]);
  useEffect(() => {
    let stopped = false;
    const refresh = () => { void listLiveSessions().then((rows) => { if (!stopped) setSessions(rows); }).catch(() => {}); };
    refresh();
    window.addEventListener("recording-ws:__open", refresh);
    window.addEventListener("recording-ws:live_note_completed", refresh);
    const timer = window.setInterval(refresh, 30000);
    return () => { stopped = true; clearInterval(timer); window.removeEventListener("recording-ws:__open", refresh); window.removeEventListener("recording-ws:live_note_completed", refresh); };
  }, [projectId]);
  const pending = sessions.filter((session) => session.project_id === projectId && !session.minutes_id && session.session_id !== activeSessionId);
  if (!pending.length) return null;
  return <aside className="px-5 py-3 border-b border-(--border) bg-(--surface-2)" aria-label="保存済みの復旧ノート">
    <p className="text-[11px] text-(--t2) mb-2">復旧した議論ノート · 音声の有無にかかわらず閲覧・編集できます</p>
    <div className="flex gap-2 flex-wrap">{pending.map((session) => <button key={session.session_id} className="btn text-[11px]" onClick={() => onOpen(session.session_id)}>{session.session_id} · rev {session.note_revision}</button>)}</div>
  </aside>;
}

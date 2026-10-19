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
  return <aside className="px-5 py-2 border-b border-(--border) bg-(--surface-2)" aria-label="保存済みの復旧ノート">
    <details key={projectId} className="group">
      <summary className="cursor-pointer list-none text-[11px] text-(--t2) focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--accent)">
        <span className="inline-flex items-center gap-2">
          <span aria-hidden="true" className="transition-transform group-open:rotate-90">▸</span>
          <span>復旧した議論ノート（{pending.length}件）</span>
        </span>
      </summary>
      <div className="pt-2">
        <p className="text-[11px] text-(--t2) mb-2">音声の有無にかかわらず閲覧・編集できます</p>
        <div className="max-h-40 overflow-y-auto overscroll-contain pr-1" aria-label="復旧ノート一覧">
          <div className="flex flex-col gap-1">
            {pending.map((session) => <button key={session.session_id} className="btn w-full text-left text-[11px] whitespace-normal break-all" onClick={() => onOpen(session.session_id)}>{session.session_id} · rev {session.note_revision}</button>)}
          </div>
        </div>
      </div>
    </details>
  </aside>;
}

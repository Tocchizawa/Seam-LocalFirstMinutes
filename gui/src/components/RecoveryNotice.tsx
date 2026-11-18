import { CheckCircle, Info, X } from "@phosphor-icons/react";
import type { RecoveryStatus } from "../lib/api";
import { Spinner } from "./Spinner";

export function RecoveryNotice({ status, onDismiss }: { status: RecoveryStatus; onDismiss: () => void }) {
  const done = status.state === "done";
  const count = done ? status.recovered : status.current;
  const item = status.item;
  const time = item?.hhmm && item.hhmm !== "??:??" ? item.hhmm : "";
  return (
    <div className="recovery-banner" role="status">
      {done ? count === status.total ? <CheckCircle size={16} /> : <Info size={16} /> : <Spinner size={14} />}
      <span>{done ? "録音の復元結果" : "中断した録音を復元中"}</span>
      <span className="num">{count} / {status.total} 件</span>
      {!done && item && <span className="truncate text-(--t3)">{item.date} {time}</span>}
      {done && (
        <button type="button" className="icon-btn ml-auto" aria-label="復元結果を閉じる" onClick={onDismiss}><X size={16} /></button>
      )}
    </div>
  );
}

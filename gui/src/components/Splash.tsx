/* バックエンド (Python + uv) の初回起動を待つスプラッシュ画面。
   Tauri が emit する "backend-status" / "backend-log" を listen して進捗+生ログを表示する。
   初回は uv sync で torch / mlx-whisper など ~3GB をダウンロードするため
   5-10 分かかる。何が起きているかをユーザーに見せる。 */
import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Spinner } from "./Spinner";

interface BackendStatus {
  phase: string;
  message: string;
  progress: number | null;
  detail?: string | null;
}

const DEFAULT_STATUS: BackendStatus = {
  phase: "starting",
  message: "起動中...",
  progress: null,
  detail: null,
};

interface LogLine {
  id: number;
  text: string;
}

interface StartupSnapshot {
  status?: BackendStatus | null;
  logs?: string[];
}

export interface SplashStartupUpdate {
  message: string;
  progress: number | null;
  detail?: string | null;
  isError?: boolean;
}

const MAX_LOG_LINES = 80;

export function Splash({ startupUpdate }: { startupUpdate?: SplashStartupUpdate | null }) {
  const [status, setStatus] = useState<BackendStatus>(DEFAULT_STATUS);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const nextIdRef = useRef(0);
  const logScrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const stop = listen<BackendStatus>("backend-status", (e) => {
      setStatus(e.payload);
    });
    return () => {
      stop.then((un) => un()).catch(() => { /* noop */ });
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    invoke<StartupSnapshot>("get_startup_snapshot")
      .then((snapshot) => {
        if (cancelled) return;
        if (snapshot.status) setStatus(snapshot.status);
        const restored = snapshot.logs || [];
        if (restored.length > 0) {
          setLogs((prev) => {
            const seen = new Set(prev.map((line) => line.text));
            const missing = restored.filter((text) => text && !seen.has(text));
            if (missing.length === 0) return prev;
            const next = [
              ...missing.map((text) => ({ id: nextIdRef.current++, text })),
              ...prev,
            ];
            return next.slice(Math.max(0, next.length - MAX_LOG_LINES));
          });
        }
      })
      .catch(() => { /* older dev shells may not expose the command yet */ });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const stop = listen<string>("backend-log", (e) => {
      const text = (e.payload || "").trim();
      if (!text) return;
      setLogs((prev) => {
        // tqdm 風進捗 ("xxx: 23%|███...") は前の同 prefix 行を置換(端末の \r 上書き相当)
        const isProgress = /%\|/.test(text);
        if (isProgress && prev.length > 0) {
          const last = prev[prev.length - 1];
          const newPrefix = text.split(":")[0];
          const lastPrefix = last.text.split(":")[0];
          if (newPrefix === lastPrefix && /%\|/.test(last.text)) {
            return [...prev.slice(0, -1), { id: nextIdRef.current++, text }];
          }
        }
        const next = [...prev, { id: nextIdRef.current++, text }];
        if (next.length <= MAX_LOG_LINES) return next;
        return next.slice(next.length - MAX_LOG_LINES);
      });
    });
    return () => {
      stop.then((un) => un()).catch(() => { /* noop */ });
    };
  }, []);

  // 新行が追加されたら下端へ自動スクロール
  useEffect(() => {
    const el = logScrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [logs.length]);

  const displayStatus: BackendStatus = startupUpdate
    ? {
      phase: startupUpdate.isError ? "error" : "update",
      message: startupUpdate.message,
      progress: startupUpdate.progress,
      detail: startupUpdate.detail ?? null,
    }
    : status;
  const pct = typeof displayStatus.progress === "number"
    ? Math.round(Math.max(0, Math.min(1, displayStatus.progress)) * 100)
    : null;
  const isReady = !startupUpdate && status.phase === "ready" && (pct ?? 0) >= 100;
  const isError = Boolean(startupUpdate?.isError) || displayStatus.phase === "error";

  return (
    <div className="splash-shell">
      <div data-tauri-drag-region className="fixed top-0 left-0 right-0 h-9 z-50" />
      <div className="splash-card anim-fade-in">
        <img
          src="/icon.png"
          width={88}
          height={88}
          alt="Seam"
          className="splash-icon"
          draggable={false}
          onError={(e) => {
            (e.target as HTMLImageElement).style.display = "none";
          }}
        />
        <h1 className="splash-title">Seam</h1>

        <div className="splash-progress-wrap">
          <div className="splash-progress-track">
            <div
              className={[
                "splash-progress-fill",
                pct === null && !isError ? "is-indeterminate" : "",
                isError ? "is-error" : "",
              ].filter(Boolean).join(" ")}
              style={pct !== null ? { width: `${pct}%` } : undefined}
            />
            {/* 完了前は常時走るシマー(画面に動きを残す) */}
            {!isReady && !isError && <div className="splash-progress-shimmer" />}
          </div>
          {!isReady && !isError && <Spinner size={11} color="var(--accent)" />}
          <span className="splash-progress-label num tabular-nums">
            {pct !== null ? `${pct}%` : "—"}
          </span>
        </div>

        <p
          className={`splash-message anim-fade-in ${isError ? "is-error" : ""}`}
          key={displayStatus.message}
        >
          {displayStatus.message}
        </p>

        {displayStatus.detail && (
          <p className={`splash-detail ${isError ? "is-error" : ""}`}>
            {displayStatus.detail}
          </p>
        )}

        {isError && (
          <div className="splash-note is-error">
            Seam を終了してから再起動してください。port 競合の場合は表示されたプロセスを終了し、
            依存関係の初期化に失敗した場合はネットワークと空き容量を確認してください。
          </div>
        )}

        <div ref={logScrollRef} className="splash-log">
          {startupUpdate ? (
            <div className="splash-log-line anim-log-in">
              起動処理へ進む前にアップデート確認を完了します
            </div>
          ) : logs.length === 0 ? (
            <div className="splash-log-empty">ログ待機中...</div>
          ) : (
            logs.map((line) => (
              <div key={line.id} className="splash-log-line anim-log-in">
                {line.text}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

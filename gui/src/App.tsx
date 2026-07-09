import { useState, useEffect, useCallback, useRef } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "./App.css";
import {
  checkHealth, type Project, type Minutes, getSettings, listProjects,
  getPipelines, getActiveSummarizes, listDevices,
} from "./lib/api";
import { initTheme } from "./lib/theme";
import { Sidebar } from "./components/Sidebar";
import { MainView } from "./pages/MainView";
import { DetailView } from "./pages/DetailView";
import { LiveView } from "./pages/LiveView";
import { SettingsModal } from "./pages/Settings";
import { ProjectSettingsModal } from "./pages/ProjectSettings";
import { ProjectDialog } from "./components/ProjectDialog";
import { Splash } from "./components/Splash";
import { Toaster } from "./components/Toaster";
import { RecordingProvider, useRecording } from "./lib/recording-context";
import { RecordToolbar } from "./components/RecordToolbar";
import { DeviceSettingsModal } from "./components/DeviceSettingsModal";
import { LatestSegmentPreview } from "./components/LatestSegmentPreview";
import {
  checkForAppUpdate,
  closeAppUpdate,
  installAndRelaunchAppUpdate,
  updateErrorMessage,
} from "./lib/updater";
import { showToast } from "./lib/toast";

type Mode =
  | { kind: "main" }
  | { kind: "detail"; minutes: Minutes; query?: string; tab?: "summary" | "transcript" }
  | { kind: "pipeline-detail"; sessionId: string }
  | { kind: "live"; previous: Exclude<Mode, { kind: "live" }> };

function App() {
  return (
    <RecordingProvider>
      <AppInner />
    </RecordingProvider>
  );
}

function AppInner() {
  const [showSettings, setShowSettings] = useState(false);
  const [projectSettingsTarget, setProjectSettingsTarget] = useState<Project | null>(null);
  const [showProjectDialog, setShowProjectDialog] = useState(false);
  const [healthy, setHealthy] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [mode, setMode] = useState<Mode>({ kind: "main" });
  const [showDevicesGlobal, setShowDevicesGlobal] = useState(false);
  const [activeProjectIds, setActiveProjectIds] = useState<Set<string>>(new Set());
  /** 進行中の要約ジョブ: minutes_id → state ("queued" / "running") */
  const [activeSummarizes, setActiveSummarizes] = useState<Map<string, string>>(new Map());
  const {
    recording, micDevice, setMicDevice, captureSystem, setCaptureSystem,
  } = useRecording();
  const startupUpdateCheckedRef = useRef(false);
  const recordingRef = useRef(recording);

  useEffect(() => initTheme(), []);

  useEffect(() => {
    recordingRef.current = recording;
  }, [recording]);

  // WebView のデフォルトコンテキストメニュー (Reload 等) を抑止。
  // 入力欄では OS 標準のメニュー (コピー/ペースト) を残す。
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) {
        return;
      }
      e.preventDefault();
    };
    window.addEventListener("contextmenu", handler);
    return () => window.removeEventListener("contextmenu", handler);
  }, []);

  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        await checkHealth();
        setHealthy(true);
        clearInterval(poll);
      } catch {
        // backend がまだ起動してない
      }
    }, 800);
    return () => clearInterval(poll);
  }, []);

  const refreshProjects = useCallback(async () => {
    const [list, settings] = await Promise.all([
      listProjects(),
      getSettings().catch(() => null),
    ]);
    const preferredProjectId = (settings?.recording as any)?.last_project_id as string | undefined;
    setProjects(list);
    setSelectedProject((prev) => {
      if (prev) return list.find((p) => p.id === prev.id) || list[0] || null;
      if (preferredProjectId) {
        const preferred = list.find((p) => p.id === preferredProjectId);
        if (preferred) return preferred;
      }
      return list[0] || null;
    });
    // 編集モーダルが開いているプロジェクトを最新化
    setProjectSettingsTarget((prev) => {
      if (!prev) return prev;
      return list.find((p) => p.id === prev.id) || null;
    });
  }, []);

  useEffect(() => {
    if (healthy) refreshProjects();
  }, [healthy, refreshProjects]);

  useEffect(() => {
    if (!healthy || startupUpdateCheckedRef.current) return;
    startupUpdateCheckedRef.current = true;
    let cancelled = false;

    (async () => {
      const settings = await getSettings().catch(() => null);
      const updateSettings = ((settings?.app_update as any) || {}) as {
        check_on_startup?: boolean;
        auto_install_on_startup?: boolean;
      };
      const autoInstall = Boolean(updateSettings.auto_install_on_startup);
      const checkOnStartup = autoInstall || updateSettings.check_on_startup !== false;
      if (!checkOnStartup) return;

      let update = null as Awaited<ReturnType<typeof checkForAppUpdate>>;
      try {
        update = await checkForAppUpdate({ timeoutMs: 12_000 });
        if (cancelled || !update) return;

        if (recordingRef.current) {
          showToast({
            kind: "info",
            text: `Seam ${update.version} を利用できます。録音終了後に設定から更新してください`,
            ttl: 7000,
          });
          await closeAppUpdate(update);
          return;
        }

        if (!autoInstall) {
          showToast({
            kind: "info",
            text: `Seam ${update.version} を利用できます`,
            ttl: 8000,
            onClick: () => setShowSettings(true),
            hoverHint: "設定を開く",
          });
          await closeAppUpdate(update);
          return;
        }

        showToast({
          kind: "info",
          text: `Seam ${update.version} にアップデートします`,
          ttl: 5000,
        });
        await installAndRelaunchAppUpdate(update, undefined, {
          shouldContinue: () => !recordingRef.current,
          abortMessage: "録音中のため自動アップデートを中断しました",
        });
      } catch (e) {
        if (autoInstall && !cancelled) {
          const message = updateErrorMessage(e);
          const interrupted = message.includes("中断");
          showToast({
            kind: interrupted ? "info" : "err",
            text: interrupted ? message : `自動アップデート失敗: ${message}`,
            ttl: 7000,
          });
        } else {
          console.warn("[updater] startup check failed", e);
        }
        await closeAppUpdate(update);
      }
    })();

    return () => { cancelled = true; };
  }, [healthy]);

  // 録音デバイス設定は MainView 以外のツールバーからも使うため、アプリ全体で復元する。
  useEffect(() => {
    if (!healthy) return;
    let cancelled = false;
    Promise.all([listDevices({ refresh: true }), getSettings().catch(() => null)]).then(([r, s]) => {
      if (cancelled) return;
      const rec = (s?.recording as any) || {};
      const savedCapture = typeof rec.last_capture_system === "boolean"
        ? rec.last_capture_system : null;
      if (savedCapture !== null) {
        setCaptureSystem(savedCapture && r.screen_capture_available);
      } else {
        setCaptureSystem(Boolean(r.screen_capture_available));
      }

      const savedMic = typeof rec.last_mic_device === "number" ? rec.last_mic_device : null;
      if (savedMic !== null && r.devices.some((d) => d.id === savedMic)) {
        setMicDevice(savedMic);
        return;
      }
      const mic = r.devices.find((d) => d.is_default && !d.is_blackhole);
      if (mic) setMicDevice(mic.id);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [healthy, setCaptureSystem, setMicDevice]);

  // 「いずれかのタスクが進行中」な project_id の集合を維持。
  // pipelines (録音/文字起こし) + active summarize (要約) をマージする。
  const refreshActiveProjects = useCallback(async () => {
    try {
      const [pipes, sums] = await Promise.all([
        getPipelines().catch(() => []),
        getActiveSummarizes().catch(() => []),
      ]);
      const ids = new Set<string>();
      const summaryMap = new Map<string, string>();
      for (const p of pipes) {
        if (
          p.project_id &&
          (p.state === "recording" || p.state === "stopping" || p.state === "transcribing")
        ) {
          ids.add(p.project_id);
        }
      }
      for (const s of sums) {
        if (s.project_id) ids.add(s.project_id);
        if (s.minutes_id) summaryMap.set(s.minutes_id, s.state || "running");
      }
      setActiveProjectIds(ids);
      setActiveSummarizes(summaryMap);
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (!healthy) return;
    refreshActiveProjects();
    // WS イベントで即時更新 (recording-context が再配信)
    const onEvt = () => refreshActiveProjects();
    const evts = [
      "recording-ws:pipeline_progress",
      "recording-ws:pipeline_done",
      "recording-ws:pipeline_error",
      "recording-ws:recording_stopped",
      "recording-ws:summary_stage",
      "recording-ws:summary_done",
      "recording-ws:summary_failed",
      "recording-ws:summary_skipped",
      "recording-ws:summary_cancelled",
    ];
    for (const t of evts) window.addEventListener(t, onEvt);
    // フォールバック: 5秒おきに確認
    const id = window.setInterval(refreshActiveProjects, 5000);
    return () => {
      for (const t of evts) window.removeEventListener(t, onEvt);
      window.clearInterval(id);
    };
  }, [healthy, refreshActiveProjects]);

  // 録音が終わったら live モードからは抜ける
  useEffect(() => {
    if (!recording && mode.kind === "live") {
      setMode(mode.previous);
    }
  }, [recording, mode]);

  // 録音開始時 (false → true) は自動でライブ全画面に遷移
  const prevRecordingRef = useRef(recording);
  useEffect(() => {
    if (!prevRecordingRef.current && recording) {
      setMode((m) => (m.kind === "live" ? m : { kind: "live", previous: m }));
    }
    prevRecordingRef.current = recording;
  }, [recording]);

  const handleDrag = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button, input, select, [role=menu]")) return;
    e.preventDefault();
    getCurrentWindow().startDragging();
  };

  if (!healthy) {
    return <Splash />;
  }

  const openLive = () => {
    setMode((m) => {
      if (m.kind === "live") return m;
      return { kind: "live", previous: m };
    });
  };
  const closeLive = () => {
    setMode((m) => (m.kind === "live" ? m.previous : m));
  };

  // 録音中で main ビュー以外 (= detail / pipeline-detail / live) ならフル RecordToolbar を出す。
  // main ビューには MainView 内に既に RecordToolbar があるのでここでは出さない (重複回避)。
  const showAppLevelToolbar = recording && mode.kind !== "main" && selectedProject;

  return (
    <div className="shell flex flex-col h-screen">
      <div data-tauri-drag-region onMouseDown={handleDrag} className="drag-bar" />

      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          projects={projects}
          selected={selectedProject}
          editingProjectId={projectSettingsTarget?.id ?? null}
          settingsActive={showSettings}
          activeProjectIds={activeProjectIds}
          onSelect={(p) => {
            setSelectedProject(p);
            setMode({ kind: "main" });
          }}
          onNewProject={() => setShowProjectDialog(true)}
          onOpenProjectSettings={(p) => setProjectSettingsTarget(p)}
          onOpenSettings={() => setShowSettings(true)}
        />

        <main className="flex-1 overflow-hidden relative flex flex-col">
          {showAppLevelToolbar && selectedProject && (
            <>
              <RecordToolbar
                project={selectedProject}
                onStopped={() => {
                  // MainView 不在時の停止: 一覧へ戻った時に refresh されるよう通知
                  window.dispatchEvent(new CustomEvent("minutes-updated"));
                }}
                onOpenDeviceSettings={() => setShowDevicesGlobal(true)}
              />
              {mode.kind !== "live" && (
                <LatestSegmentPreview onExpand={openLive} />
              )}
            </>
          )}

          <div className="flex-1 overflow-hidden">
            {!selectedProject ? (
              <EmptyProject onCreateNew={() => setShowProjectDialog(true)} />
            ) : mode.kind === "live" ? (
              <LiveView showStreamStatusBar onClose={closeLive} />
            ) : mode.kind === "detail" ? (
              <DetailView
                minutes={mode.minutes}
                initialQuery={mode.query}
                initialTab={mode.tab}
                onBack={() => setMode({ kind: "main" })}
                onDeleted={() => {
                  /* MainView が監視してるので何もしない */
                }}
              />
            ) : mode.kind === "pipeline-detail" ? (
              <DetailView
                sessionId={mode.sessionId}
                onBack={() => setMode({ kind: "main" })}
              />
            ) : (
              <MainView
                project={selectedProject}
                allProjects={projects}
                activeSummarizes={activeSummarizes}
                onOpenMinutes={(m, opts) =>
                  setMode({ kind: "detail", minutes: m, query: opts?.query, tab: opts?.tab })
                }
                onOpenPipelineSession={(sid) => setMode({ kind: "pipeline-detail", sessionId: sid })}
                onOpenLive={openLive}
              />
            )}
          </div>
        </main>
      </div>

      {showSettings && (
        <SettingsModal onClose={() => setShowSettings(false)} />
      )}
      {projectSettingsTarget && (
        <ProjectSettingsModal
          project={projectSettingsTarget}
          onClose={() => setProjectSettingsTarget(null)}
          onSaved={refreshProjects}
          onDeleted={async () => {
            const deletedId = projectSettingsTarget.id;
            setProjectSettingsTarget(null);
            setSelectedProject((prev) => (prev?.id === deletedId ? null : prev));
            setMode({ kind: "main" });
            await refreshProjects();
          }}
        />
      )}
      {showProjectDialog && (
        <ProjectDialog
          onClose={() => setShowProjectDialog(false)}
          onSave={async () => {
            setShowProjectDialog(false);
            await refreshProjects();
          }}
        />
      )}
      {showDevicesGlobal && (
        <DeviceSettingsModal
          micDevice={micDevice}
          captureSystem={captureSystem}
          refreshOnOpen={!recording}
          onChangeMicDevice={setMicDevice}
          onChangeCaptureSystem={setCaptureSystem}
          onClose={() => setShowDevicesGlobal(false)}
        />
      )}

      <Toaster />
    </div>
  );
}

function EmptyProject({ onCreateNew }: { onCreateNew: () => void }) {
  return (
    <div className="anim-fade-in flex flex-col items-center justify-center h-full gap-3">
      <p className="text-[14px] text-(--t1) font-medium">プロジェクトを作成しましょう</p>
      <p className="text-[12px] text-(--t3)">プロジェクトごとに議事録が整理されます</p>
      <button onClick={onCreateNew} className="btn btn-primary mt-2 h-8 px-4 text-[12px]">
        新規プロジェクト
      </button>
    </div>
  );
}

export default App;

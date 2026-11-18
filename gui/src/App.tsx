import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "./App.css";
import "./design-system.css";
import {
  checkHealth, type Project, type Minutes, getSettings, listProjects,
  listDevices, reorderProjects as saveProjectOrder,
} from "./lib/api";
import { initTheme } from "./lib/theme";
import { Sidebar } from "./components/Sidebar";
import { MainView } from "./pages/MainView";
import { DetailView } from "./pages/DetailView";
import { LiveView } from "./pages/LiveView";
import { SettingsModal } from "./pages/Settings";
import { ProjectSettingsModal } from "./pages/ProjectSettings";
import { ProjectDialog } from "./components/ProjectDialog";
import { Splash, type SplashStartupUpdate } from "./components/Splash";
import { Toaster } from "./components/Toaster";
import { RecordingProvider, useRecording } from "./lib/recording-context";
import { useWorkspaceJobs } from "./lib/workspace-jobs";
import { RecoveryNotice } from "./components/RecoveryNotice";
import { StreamStatusBar } from "./components/StreamStatusBar";
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
import { ensureNotificationPermission } from "./lib/notify";

type Mode =
  | { kind: "main" }
  | { kind: "detail"; minutes: Minutes; query?: string; tab?: "summary" | "transcript" }
  | { kind: "pipeline-detail"; sessionId: string }
  | { kind: "live"; previous: Exclude<Mode, { kind: "live" }> };

interface StartupAppUpdateSettings {
  check_on_startup: boolean;
  auto_install_on_startup: boolean;
}

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
  const projectsRef = useRef<Project[]>(projects);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? projects[0] ?? null;
  const [mode, setMode] = useState<Mode>({ kind: "main" });
  const [showDevicesGlobal, setShowDevicesGlobal] = useState(false);
  const { recording, streamStatus, micDevice, setMicDevice, captureSystem, setCaptureSystem } = useRecording();
  const [dismissedRecovery, setDismissedRecovery] = useState<string | null>(null);
  const [startupUpdatePending, setStartupUpdatePending] = useState(true);
  const [startupUpdate, setStartupUpdate] = useState<SplashStartupUpdate | null>({
    message: "アップデート設定を確認中...",
    progress: null,
  });
  const [startupUpdateNotice, setStartupUpdateNotice] = useState<string | null>(null);
  const startupReady = healthy && !startupUpdatePending;

  const { pipelines, summaries, recovery, refresh: refreshJobs } = useWorkspaceJobs(startupReady);
  const activeProjectIds = useMemo(() => new Set([
    ...pipelines.filter((p) => p.state === "recording" || p.state === "stopping" || p.state === "transcribing").map((p) => p.project_id),
    ...summaries.map((s) => s.project_id),
  ].filter((id): id is string => Boolean(id))), [pipelines, summaries]);
  const activeSummarizes = useMemo(
    () => new Map(summaries.map((s) => [s.minutes_id, s.state || "running"])),
    [summaries],
  );

  useEffect(() => {
    const finishedAt = recovery?.finished_at;
    if (recovery?.state !== "done" || !finishedAt) return;
    const timeout = window.setTimeout(() => setDismissedRecovery(finishedAt), 8000);
    return () => window.clearTimeout(timeout);
  }, [recovery?.state, recovery?.finished_at]);

  useEffect(() => initTheme(), []);

  useEffect(() => {
    let cancelled = false;
    let finishTimer: number | null = null;
    let update: Awaited<ReturnType<typeof checkForAppUpdate>> | null = null;
    let autoInstallRequested = false;
    let backendStartRequested = false;

    const startBackend = async () => {
      if (cancelled || backendStartRequested) return;
      backendStartRequested = true;
      try {
        await invoke("start_backend_after_startup_update");
      } catch (e) {
        console.warn("[startup] backend start command failed", e);
      }
    };

    const finish = (delayMs = 0) => {
      const complete = async () => {
        await startBackend();
        if (cancelled) return;
        setStartupUpdate(null);
        setStartupUpdatePending(false);
      };
      if (delayMs > 0) {
        void startBackend();
        finishTimer = window.setTimeout(() => {
          void complete();
        }, delayMs);
      } else {
        void complete();
      }
    };

    (async () => {
      try {
        const settings = await invoke<StartupAppUpdateSettings>(
          "get_startup_app_update_settings",
        ).catch(() => ({
          check_on_startup: true,
          auto_install_on_startup: false,
        }));
        const autoInstall = Boolean(settings.auto_install_on_startup);
        autoInstallRequested = autoInstall;
        const checkOnStartup = autoInstall || settings.check_on_startup !== false;
        if (!checkOnStartup) {
          finish();
          return;
        }

        setStartupUpdate({
          message: "アップデートを確認中...",
          progress: null,
        });
        update = await checkForAppUpdate({ timeoutMs: 12_000 });
        if (cancelled) return;
        if (!update) {
          finish();
          return;
        }

        if (!autoInstall) {
          setStartupUpdateNotice(`バージョン ${update.version} を利用できます`);
          await closeAppUpdate(update);
          update = null;
          finish();
          return;
        }

        setStartupUpdate({
          message: `バージョン ${update.version} に更新中`,
          progress: null,
        });
        await installAndRelaunchAppUpdate(update, (progress) => {
          if (cancelled) return;
          setStartupUpdate({
            message: progress.message,
            progress: typeof progress.percent === "number" ? progress.percent / 100 : null,
            detail: progress.totalBytes
              ? `${Math.round((progress.downloadedBytes ?? 0) / 1024 / 1024)}MB / ${
                Math.round(progress.totalBytes / 1024 / 1024)
              }MB`
              : null,
          });
        });
      } catch (e) {
        const message = updateErrorMessage(e);
        if (!cancelled && autoInstallRequested) {
          setStartupUpdate({
            message: `自動アップデート失敗: ${message}`,
            progress: null,
            detail: "このまま起動を続行します",
            isError: true,
          });
          setStartupUpdateNotice(`自動アップデート失敗: ${message}`);
          finish(3500);
        } else if (!cancelled) {
          console.warn("[updater] startup check failed", e);
          finish();
        }
        await closeAppUpdate(update);
        update = null;
      }
    })();

    return () => {
      cancelled = true;
      if (finishTimer !== null) window.clearTimeout(finishTimer);
      closeAppUpdate(update).catch(() => {});
    };
  }, []);

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
    projectsRef.current = list;
    setProjects(list);
    setSelectedProjectId((previous) => (
      [previous, preferredProjectId].find((id) => list.some((project) => project.id === id)) ?? list[0]?.id ?? null
    ));
    // 編集モーダルが開いているプロジェクトを最新化
    setProjectSettingsTarget((prev) => {
      if (!prev) return prev;
      return list.find((p) => p.id === prev.id) || null;
    });
  }, []);

  const handleProjectReorder = useCallback(async (projectIds: string[]) => {
    const current = projectsRef.current;
    const byId = new Map(current.map((project) => [project.id, project]));
    if (projectIds.length !== current.length || projectIds.some((id) => !byId.has(id))) {
      await refreshProjects();
      throw new Error("プロジェクトの一覧が変更されました");
    }

    const optimistic = projectIds.map((id) => byId.get(id)!);
    projectsRef.current = optimistic;
    setProjects(optimistic);
    try {
      const saved = await saveProjectOrder(projectIds);
      projectsRef.current = saved;
      setProjects(saved);
    } catch (error) {
      if (projectsRef.current === optimistic) {
        projectsRef.current = current;
        setProjects(current);
      }
      void refreshProjects().catch(() => {});
      showToast({ kind: "err", text: "並び順を保存できませんでした" });
      throw error;
    }
  }, [refreshProjects]);

  useEffect(() => {
    if (startupReady) {
      void refreshProjects().catch(() => showToast({
        kind: "err",
        text: "プロジェクトを読み込めませんでした",
        onClick: () => void refreshProjects().catch(() => showToast({ kind: "err", text: "プロジェクトを読み込めませんでした" })),
        hoverHint: "再試行",
      }));
    }
  }, [startupReady, refreshProjects]);

  // 通知許可を起動時に確認しておく (録音中の音声未検出警告などで使うため、
  // 初回の許可ダイアログが録音の途中に出ないようにする)
  useEffect(() => {
    if (startupReady) void ensureNotificationPermission();
  }, [startupReady]);

  useEffect(() => {
    if (!startupReady || !startupUpdateNotice) return;
    const text = startupUpdateNotice;
    setStartupUpdateNotice(null);
    showToast({
      kind: text.startsWith("自動アップデート失敗") ? "err" : "info",
      text,
      ttl: 8000,
      onClick: text.startsWith("バージョン ") ? () => setShowSettings(true) : undefined,
      hoverHint: text.startsWith("バージョン ") ? "設定を開く" : undefined,
    });
  }, [startupReady, startupUpdateNotice]);

  // 録音デバイス設定は MainView 以外のツールバーからも使うため、アプリ全体で復元する。
  useEffect(() => {
    if (!startupReady) return;
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
  }, [startupReady, setCaptureSystem, setMicDevice]);

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
    void getCurrentWindow().startDragging().catch(() => {});
  };

  if (!startupReady) {
    return <Splash startupUpdate={startupUpdatePending ? startupUpdate : null} />;
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

  return (
    <div className="shell flex flex-col h-screen">
      <div data-tauri-drag-region onMouseDown={handleDrag} className="drag-bar" />

      <div className="flex flex-1 overflow-hidden" inert={showSettings || Boolean(projectSettingsTarget) || showProjectDialog || showDevicesGlobal}>
        <Sidebar
          projects={projects}
          selected={selectedProject}
          editingProjectId={projectSettingsTarget?.id ?? null}
          settingsActive={showSettings}
          activeProjectIds={activeProjectIds}
          onSelect={(p) => {
            setSelectedProjectId(p.id);
            setMode({ kind: "main" });
          }}
          onNewProject={() => setShowProjectDialog(true)}
          onReorder={handleProjectReorder}
          onOpenProjectSettings={(p) => setProjectSettingsTarget(p)}
          onOpenSettings={() => setShowSettings(true)}
        />

        <main className="flex-1 overflow-hidden relative flex flex-col">
          {selectedProject && (
            <RecordToolbar
              project={selectedProject}
              onStopped={refreshJobs}
              onOpenDeviceSettings={() => setShowDevicesGlobal(true)}
            />
          )}
          {recovery && recovery.total > 0 && (
            recovery.state === "running" ||
            recovery.state === "done" && recovery.finished_at !== dismissedRecovery
          ) && (
            <RecoveryNotice status={recovery} onDismiss={() => setDismissedRecovery(recovery.finished_at)} />
          )}
          {recording && mode.kind !== "live" && (
            <>
              <LatestSegmentPreview onExpand={openLive} />
              {streamStatus && <StreamStatusBar status={streamStatus} />}
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
                key={selectedProject.id}
                project={selectedProject}
                allProjects={projects}
                activeSummarizes={activeSummarizes}
                onOpenMinutes={(m, opts) =>
                  setMode({ kind: "detail", minutes: m, query: opts?.query, tab: opts?.tab })
                }
                onOpenPipelineSession={(sid) => setMode({ kind: "pipeline-detail", sessionId: sid })}
                pipelines={pipelines}
                refreshPipelines={refreshJobs}
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
            setProjectSettingsTarget(null);
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
      <button onClick={onCreateNew} className="btn btn-primary mt-2 h-8 px-4 text-[12px]">
        新規プロジェクト
      </button>
    </div>
  );
}

export default App;

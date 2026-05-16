import { useEffect, useState } from "react";
import { open, ask } from "@tauri-apps/plugin-dialog";
import {
  CheckCircle, CircleNotch, FolderOpen, MagicWand, PencilSimple, Plus, Trash, WarningCircle, X,
} from "@phosphor-icons/react";
import {
  autoGenerateGlossary,
  deleteProject,
  updateProject,
  type CorrectionPair,
  type GlossarySuggestion,
  type Project,
} from "../lib/api";
import { showToast } from "../lib/toast";
import { GlossaryManagerModal } from "../components/GlossaryManagerModal";
import { CorrectionsManagerModal } from "../components/CorrectionsManagerModal";

interface Props {
  project: Project;
  onClose: () => void;
  onSaved: () => Promise<void>;
  onDeleted: () => Promise<void>;
}

export function ProjectSettingsModal({ project, onClose, onSaved, onDeleted }: Props) {
  const [closing, setClosing] = useState(false);

  const [name, setName] = useState(project.name);
  const [outputDir, setOutputDir] = useState(project.output_dir);
  const [repoPath, setRepoPath] = useState(project.repo_path || "");
  const [docDirs, setDocDirs] = useState<string[]>(project.doc_dirs || []);
  const [glossary, setGlossary] = useState((project.glossary || []).join("\n"));
  const [corrections, setCorrections] = useState<CorrectionPair[]>(project.corrections || []);
  const [glossaryManagerOpen, setGlossaryManagerOpen] = useState(false);
  const [correctionsManagerOpen, setCorrectionsManagerOpen] = useState(false);

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState(false);

  // Glossary 自動生成
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState("");
  const [aiSuggestions, setAiSuggestions] = useState<GlossarySuggestion[] | null>(null);
  const [aiSelected, setAiSelected] = useState<Set<string>>(new Set());
  const [aiElapsed, setAiElapsed] = useState<number>(0);
  const [aiProvider, setAiProvider] = useState<string>("");
  const [aiIsCli, setAiIsCli] = useState<boolean>(false);
  const [aiActivity, setAiActivity] = useState<string>("");

  useEffect(() => {
    setName(project.name);
    setOutputDir(project.output_dir);
    setRepoPath(project.repo_path || "");
    setDocDirs(project.doc_dirs || []);
    setGlossary((project.glossary || []).join("\n"));
    setCorrections(project.corrections || []);
  }, [project]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") handleClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const handleClose = () => {
    setClosing(true);
    setTimeout(onClose, 180);
  };

  const dirty =
    name !== project.name
    || outputDir !== project.output_dir
    || (repoPath || "") !== (project.repo_path || "")
    || JSON.stringify(docDirs) !== JSON.stringify(project.doc_dirs || [])
    || glossary !== (project.glossary || []).join("\n")
    || JSON.stringify(corrections) !== JSON.stringify(project.corrections || []);

  const canSave = name.trim().length > 0 && outputDir.trim().length > 0 && dirty && !saving;

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      await updateProject(project.id, {
        name: name.trim(),
        output_dir: outputDir.trim(),
        repo_path: repoPath.trim() || null,
        doc_dirs: docDirs.map((d) => d.trim()).filter(Boolean),
        glossary: glossary.split("\n").map((s) => s.trim()).filter(Boolean),
        corrections,
      });
      await onSaved();
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存に失敗しました");
    } finally {
      setSaving(false);
    }
  };

  const hasDocs = docDirs.length > 0 || (repoPath?.trim().length ?? 0) > 0;

  const runAutoGenerate = async () => {
    if (!hasDocs || aiBusy) return;
    setAiBusy(true);
    setAiError("");
    setAiSuggestions(null);
    setAiSelected(new Set());
    setAiElapsed(0);
    setAiProvider("");
    setAiIsCli(false);
    setAiActivity("");
    try {
      const res = await autoGenerateGlossary(project.id, (status) => {
        setAiElapsed(status.elapsed_sec || 0);
        if (status.provider) setAiProvider(status.provider);
        setAiIsCli(Boolean(status.is_cli));
        if (status.current_activity) setAiActivity(status.current_activity);
      });
      if (res.state === "error") {
        setAiError(res.error?.message || "用語集の生成に失敗しました");
        return;
      }
      const suggestions = res.suggestions || [];
      setAiSuggestions(suggestions);
      const existing = new Set(
        glossary.split("\n").map((s) => s.trim()).filter(Boolean),
      );
      const initial = new Set<string>();
      for (const s of suggestions) {
        if (!existing.has(s.term)) initial.add(s.term);
      }
      setAiSelected(initial);
      if (suggestions.length === 0) {
        setAiError("候補が見つかりませんでした。");
      }
    } catch (e) {
      setAiError(e instanceof Error ? e.message : "用語集の生成に失敗しました");
    } finally {
      setAiBusy(false);
    }
  };

  const applySuggestions = () => {
    if (!aiSuggestions) return;
    const existing = glossary.split("\n").map((s) => s.trim()).filter(Boolean);
    // 既存エントリの term 部分だけを抽出して重複チェックに使う
    const existingTerms = new Set(
      existing.map((line) => {
        const a = line.indexOf(":");
        const b = line.indexOf("："); // 全角コロン
        const i = a < 0 ? b : b < 0 ? a : Math.min(a, b);
        return (i < 0 ? line : line.slice(0, i)).trim();
      }),
    );
    const merged = [...existing];
    for (const s of aiSuggestions) {
      if (!aiSelected.has(s.term)) continue;
      const t = s.term.trim();
      if (!t || existingTerms.has(t)) continue;
      existingTerms.add(t);
      // description が AI から来ていれば "term: description" 形式で保存
      const d = (s.description || "").trim();
      merged.push(d ? `${t}: ${d}` : t);
    }
    setGlossary(merged.join("\n"));
    setAiSuggestions(null);
    setAiSelected(new Set());
    showToast({ kind: "ok", text: "用語集に追加しました (未保存)" });
  };

  const toggleSuggestion = (term: string) => {
    setAiSelected((prev) => {
      const next = new Set(prev);
      if (next.has(term)) next.delete(term);
      else next.add(term);
      return next;
    });
  };

  const handleDelete = async () => {
    const ok = await ask(
      `プロジェクト「${project.name}」を削除しますか?\n議事録データは保持されますが、出力フォルダ内のファイルは削除されません。`,
      {
        title: "プロジェクトの削除",
        kind: "warning",
        okLabel: "削除",
        cancelLabel: "キャンセル",
      },
    );
    if (!ok) return;
    setDeleting(true);
    try {
      await deleteProject(project.id, false);
      showToast({ kind: "ok", text: "プロジェクトを削除しました" });
      await onDeleted();
    } catch (e) {
      showToast({
        kind: "err",
        text: `削除失敗: ${e instanceof Error ? e.message : "不明なエラー"}`,
      });
      setDeleting(false);
    }
  };

  const pickDir = async (title: string): Promise<string | null> => {
    const s = await open({ directory: true, multiple: false, title });
    return typeof s === "string" ? s : null;
  };

  const addDocDir = async () => {
    const s = await pickDir("資料フォルダ");
    if (!s) return;
    if (!docDirs.includes(s)) setDocDirs([...docDirs, s]);
  };

  const removeDocDir = (i: number) => {
    setDocDirs(docDirs.filter((_, idx) => idx !== i));
  };

  return (
    <div className={`fixed inset-0 flex items-center justify-center z-50 ${closing ? "anim-modal-overlay-out" : "anim-modal-overlay-in"}`}>
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={handleClose}
        style={{ background: "rgba(0,0,0,0.4)" }}
      />

      <div className={`dialog-shell relative w-[560px] max-w-[95vw] max-h-[86vh] flex flex-col overflow-hidden ${closing ? "anim-modal-out" : "anim-modal-in"}`}>
        <header className="flex items-center justify-between p-4 px-5 border-b border-(--border) shrink-0">
          <div className="min-w-0">
            <h2 className="text-[14px] font-semibold text-(--t1)">プロジェクト設定</h2>
            <p className="text-[11px] text-(--t3) truncate mt-0.5">{project.name}</p>
          </div>
          <button onClick={handleClose} className="icon-btn" title="閉じる">
            <X size={14} weight="bold" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-5 min-h-0">
          <div className="stagger flex flex-col gap-5">
            <Field label="プロジェクト名" required>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="input"
                placeholder="名前を入力"
              />
            </Field>

            <Field label="保存先" required hint="Markdown ファイルが書き出されます">
              <FolderInput
                value={outputDir}
                onChange={setOutputDir}
                placeholder="~/Documents/議事録"
                title="保存先を選択"
              />
            </Field>

            <Field label="リポジトリ" hint="任意。コンテキスト調査の対象">
              <FolderInput
                value={repoPath}
                onChange={setRepoPath}
                placeholder="任意"
                title="リポジトリを選択"
              />
            </Field>

            <Field label="ドキュメントフォルダ" hint="LLM がコンテキストとして参照">
              <div className="flex flex-col gap-1">
                {docDirs.map((dir, i) => (
                  <div key={`${dir}-${i}`} className="flex items-center gap-1 surface px-2 py-1.5">
                    <span className="flex-1 truncate text-[11px] text-(--t2)">{dir}</span>
                    <button
                      type="button"
                      onClick={() => removeDocDir(i)}
                      className="icon-btn !w-6 !h-6 hover:!text-(--danger)"
                      title="削除"
                    >
                      <Trash size={11} weight="regular" />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={addDocDir}
                  className="w-full py-1.5 text-[11px] text-(--t3) bg-transparent border border-dashed border-(--border) rounded-md cursor-pointer hover:text-(--t2) hover:border-(--border-hover) flex items-center justify-center gap-1.5 transition-all"
                >
                  <Plus size={11} weight="bold" />
                  フォルダを追加
                </button>
              </div>
            </Field>

            <Field
              label="用語集"
              hint="略語や固有名詞 (用語: 説明 形式)"
              action={
                <button
                  type="button"
                  onClick={runAutoGenerate}
                  disabled={!hasDocs || aiBusy}
                  className="btn h-6 px-2 text-[10px] shrink-0 gap-1"
                  title={
                    hasDocs
                      ? "リポジトリ / ドキュメントフォルダから用語を抽出します"
                      : "リポジトリかドキュメントフォルダを先に設定してください"
                  }
                >
                  {aiBusy ? (
                    <>
                      <CircleNotch size={11} weight="regular" className="animate-spin" />
                      生成中
                    </>
                  ) : (
                    <>
                      <MagicWand size={11} weight="regular" />
                      ドキュメントから生成
                    </>
                  )}
                </button>
              }
            >
              <GlossarySummaryCard
                value={glossary.split("\n").map((s) => s.trim()).filter(Boolean)}
                onOpen={() => setGlossaryManagerOpen(true)}
              />

              {aiBusy && (
                <div className="anim-fade-in mt-2 rounded-md border border-(--border) bg-(--surface-2) overflow-hidden">
                  <div className="flex items-center gap-2.5 px-3 py-2">
                    <CircleNotch size={14} weight="bold" className="animate-spin text-(--accent) shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-[11px] text-(--t1) font-medium leading-tight">
                        {aiIsCli
                          ? `${aiProvider || "CLI エージェント"} がドキュメントを探索中`
                          : aiProvider
                            ? `${aiProvider} で用語を抽出中`
                            : "用語を抽出中"}
                      </p>
                      <p className="text-[10px] text-(--t3) leading-tight mt-0.5 truncate">
                        {aiActivity || (aiIsCli
                          ? "agent を準備しています..."
                          : "待機中")}
                      </p>
                    </div>
                    <span className="text-[11px] tabular-nums text-(--t2) shrink-0">
                      {aiElapsed.toFixed(0)}s
                    </span>
                  </div>
                  {aiIsCli && (
                    <p className="text-[10px] text-(--t3) leading-snug px-3 py-1.5 border-t border-(--border) bg-(--surface)">
                      ※ CLI エージェント (claude_code / codex) はドキュメントを自分で読み込みます。
                      Ollama / Claude API / OpenAI / Gemini では agent としての自由探索は未対応で、
                      事前抽出した抜粋を参照する高速モードになります。
                    </p>
                  )}
                </div>
              )}

              {!aiBusy && aiError && (
                <div className="anim-fade-in mt-2 flex items-start gap-2 px-3 py-2 rounded-md border border-(--danger) bg-(--surface-2)">
                  <WarningCircle size={14} weight="fill" className="text-(--danger) mt-0.5 shrink-0" />
                  <p className="text-[11px] text-(--danger) leading-snug min-w-0">{aiError}</p>
                </div>
              )}

              {aiSuggestions && aiSuggestions.length > 0 && (
                <div className="anim-fade-in mt-2 rounded-md border border-(--border) bg-(--surface-2) flex flex-col">
                  <div className="flex items-center justify-between px-3 py-2 border-b border-(--border)">
                    <p className="text-[11px] font-medium text-(--t1)">
                      候補 <span className="text-(--t3) font-normal">({aiSelected.size}/{aiSuggestions.length})</span>
                    </p>
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => setAiSelected(new Set(aiSuggestions.map((s) => s.term)))}
                        className="text-[10px] text-(--t3) hover:text-(--t1) px-1.5 py-0.5 rounded"
                      >
                        全選択
                      </button>
                      <button
                        type="button"
                        onClick={() => setAiSelected(new Set())}
                        className="text-[10px] text-(--t3) hover:text-(--t1) px-1.5 py-0.5 rounded"
                      >
                        全解除
                      </button>
                    </div>
                  </div>
                  <ul className="flex flex-col max-h-56 overflow-y-auto px-1">
                    {aiSuggestions.map((s) => (
                      <li key={s.term}>
                        <label className="flex items-start gap-2.5 px-2 py-1.5 cursor-pointer rounded hover:bg-(--surface)">
                          <input
                            type="checkbox"
                            checked={aiSelected.has(s.term)}
                            onChange={() => toggleSuggestion(s.term)}
                            className="mt-0.5"
                          />
                          <div className="min-w-0 flex-1">
                            <p className="text-[11px] text-(--t1) font-medium truncate">{s.term}</p>
                            {s.description && (
                              <p className="text-[10px] text-(--t3) leading-tight mt-0.5 line-clamp-2">
                                {s.description}
                              </p>
                            )}
                          </div>
                        </label>
                      </li>
                    ))}
                  </ul>
                  <div className="flex items-center justify-between gap-2 px-3 py-2 border-t border-(--border)">
                    <span className="text-[10px] text-(--t3)">
                      追加内容は「保存」で確定
                    </span>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => { setAiSuggestions(null); setAiSelected(new Set()); }}
                        className="btn h-7 px-2.5 text-[11px]"
                      >
                        キャンセル
                      </button>
                      <button
                        type="button"
                        onClick={applySuggestions}
                        disabled={aiSelected.size === 0}
                        className="btn btn-primary h-7 px-2.5 text-[11px]"
                      >
                        選択して追加 ({aiSelected.size})
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </Field>

            <Field
              label="誤転写の自動補正"
              hint="要約完了時に LLM が誤転写を学習し、ここに蓄積されます。次回以降の文字起こしと再要約に反映されます。"
            >
              <CorrectionsSummaryCard
                value={corrections}
                onOpen={() => setCorrectionsManagerOpen(true)}
              />
            </Field>

            <div className="border-t border-(--border) pt-5 mt-2">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-[12px] font-medium text-(--danger)">プロジェクトの削除</p>
                  <p className="text-[10px] text-(--t3) mt-0.5 leading-snug">
                    サイドバーから消えます。議事録データと出力フォルダは保持されます。
                  </p>
                </div>
                <button
                  onClick={handleDelete}
                  disabled={deleting}
                  className="btn btn-ghost btn-danger h-8 px-3 text-[11px] shrink-0"
                >
                  <Trash size={12} weight="regular" />
                  {deleting ? "削除中..." : "削除"}
                </button>
              </div>
            </div>
          </div>
        </div>

        <footer className="flex items-center gap-3 p-4 px-5 border-t border-(--border) shrink-0">
          <button
            onClick={handleSave}
            disabled={!canSave}
            className="btn btn-primary h-8 px-4 text-[12px]"
          >
            {saving ? "保存中..." : "保存"}
          </button>
          {saved && (
            <span className="anim-fade-in flex items-center gap-1 text-[11px] text-(--success)">
              <CheckCircle size={12} weight="fill" />
              保存しました
            </span>
          )}
          {error && <span className="text-[11px] text-(--danger)">{error}</span>}
        </footer>
      </div>

      <GlossaryManagerModal
        open={glossaryManagerOpen}
        value={glossary.split("\n").map((s) => s.trim()).filter(Boolean)}
        onChange={(next) => setGlossary(next.join("\n"))}
        onClose={() => setGlossaryManagerOpen(false)}
      />
      <CorrectionsManagerModal
        open={correctionsManagerOpen}
        value={corrections}
        onChange={setCorrections}
        onClose={() => setCorrectionsManagerOpen(false)}
      />
    </div>
  );
}

function GlossarySummaryCard({
  value, onOpen,
}: { value: string[]; onOpen: () => void }) {
  const count = value.length;
  const preview = value.slice(0, 6).map((s) => {
    const a = s.indexOf(":");
    const b = s.indexOf("："); // 全角コロン
    const i = a < 0 ? b : b < 0 ? a : Math.min(a, b);
    return i < 0 ? s : s.slice(0, i).trim();
  });
  return (
    <div className="flex items-center gap-2 min-w-0">
      <div className="flex-1 min-w-0 flex items-baseline gap-2">
        <span className="text-[11px] text-(--t2) shrink-0 tabular-nums">
          {count > 0 ? `${count} 件` : "未登録"}
        </span>
        {preview.length > 0 && (
          <span className="text-[11px] text-(--t3) truncate">
            {preview.join(" · ")}{count > preview.length && " ..."}
          </span>
        )}
      </div>
      <button
        type="button"
        onClick={onOpen}
        className="btn btn-ghost h-7 px-2.5 text-[11px] flex items-center gap-1 shrink-0"
        title="用語集を管理"
      >
        <PencilSimple size={11} weight="regular" />
        管理
      </button>
    </div>
  );
}

function CorrectionsSummaryCard({
  value, onOpen,
}: { value: CorrectionPair[]; onOpen: () => void }) {
  const count = value.length;
  const preview = value.slice(0, 3);
  return (
    <div className="flex items-center gap-2 min-w-0">
      <div className="flex-1 min-w-0 flex items-baseline gap-2">
        <span className="text-[11px] text-(--t2) shrink-0 tabular-nums">
          {count > 0 ? `${count} 件` : "未登録"}
        </span>
        {preview.length > 0 && (
          <span className="text-[11px] text-(--t3) truncate">
            {preview.map((c, i) => (
              <span key={i}>
                {i > 0 && <span className="mx-1.5">·</span>}
                <span className="text-(--danger)">{c.wrong}</span>
                <span className="mx-0.5 text-(--t4)">→</span>
                <span className="text-(--success)">{c.correct}</span>
              </span>
            ))}
            {count > preview.length && " ..."}
          </span>
        )}
      </div>
      <button
        type="button"
        onClick={onOpen}
        className="btn btn-ghost h-7 px-2.5 text-[11px] flex items-center gap-1 shrink-0"
        title="誤転写ルールを管理"
      >
        <PencilSimple size={11} weight="regular" />
        管理
      </button>
    </div>
  );
}

function Field({
  label, required, hint, action, children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  /** ラベル行の右に並ぶ補助アクション (例: 「ドキュメントから生成」ボタン) */
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-1 min-h-[22px]">
        <label className="block text-[11px] font-medium text-(--t2)">
          {label}
          {required && <span className="text-(--danger) ml-0.5">*</span>}
        </label>
        {action}
      </div>
      {hint && <p className="text-[10px] text-(--t3) mb-1.5 leading-snug">{hint}</p>}
      {children}
    </div>
  );
}

function FolderInput({
  value, onChange, placeholder, title,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  title?: string;
}) {
  return (
    <div className="flex gap-1.5">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="input flex-1"
        placeholder={placeholder}
      />
      <button
        type="button"
        onClick={async () => {
          const s = await open({ directory: true, multiple: false, title: title || "フォルダを選択" });
          if (typeof s === "string") onChange(s);
        }}
        className="btn h-auto px-3 text-[11px]"
      >
        <FolderOpen size={12} weight="regular" />
        選択
      </button>
    </div>
  );
}

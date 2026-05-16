import { useState, useEffect, useRef } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { X, FolderOpen, Plus, Trash } from "@phosphor-icons/react";
import { createProject } from "../lib/api";
import { useImeSafeEnter } from "../lib/ime";

interface Props {
  onClose: () => void;
  onSave: () => Promise<void>;
}

function FolderInput({ value, onChange, placeholder, title }: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  title?: string;
}) {
  return (
    <div className="flex gap-1.5">
      <input type="text" value={value} onChange={(e) => onChange(e.target.value)}
        className="input flex-1" placeholder={placeholder} />
      <button type="button"
        onClick={async () => {
          const s = await open({ directory: true, multiple: false, title: title || "フォルダを選択" });
          if (typeof s === "string") onChange(s);
        }}
        className="btn h-auto px-3 text-[11px]">
        <FolderOpen size={12} weight="regular" />
        選択
      </button>
    </div>
  );
}

export function ProjectDialog({ onClose, onSave }: Props) {
  const [name, setName] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [outputDir, setOutputDir] = useState("");
  const [docDirs, setDocDirs] = useState<string[]>([]);
  const [glossary, setGlossary] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [step, setStep] = useState(0);
  const [closing, setClosing] = useState(false);
  const { imeHandlers, isImeEnter } = useImeSafeEnter();
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => { ref.current?.focus(); }, []);
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") handleClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const handleClose = () => {
    setClosing(true);
    setTimeout(onClose, 180);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !outputDir.trim()) {
      setError("名前と保存先は必須です");
      return;
    }
    setSaving(true); setError("");
    try {
      await createProject({
        name: name.trim(),
        repo_path: repoPath.trim() || null,
        output_dir: outputDir.trim(),
        doc_dirs: docDirs.map((s) => s.trim()).filter(Boolean),
        glossary: glossary.split("\n").map((s) => s.trim()).filter(Boolean),
      });
      await onSave();
    } catch (err: any) {
      setError(err.message || "保存に失敗");
    } finally {
      setSaving(false);
    }
  };

  const ok = name.trim().length > 0 && outputDir.trim().length > 0;

  const addDocDir = async () => {
    const s = await open({ directory: true, multiple: false, title: "資料フォルダ" });
    if (typeof s === "string" && !docDirs.includes(s)) setDocDirs([...docDirs, s]);
  };

  const removeDocDir = (i: number) => {
    setDocDirs(docDirs.filter((_, idx) => idx !== i));
  };

  return (
    <div className={`fixed inset-0 flex items-center justify-center z-50 ${closing ? "anim-modal-overlay-out" : "anim-modal-overlay-in"}`}>
      <div className="absolute inset-0 cursor-pointer" onClick={handleClose}
        style={{ background: "rgba(0,0,0,0.45)" }} />

      <div className={`dialog-shell relative w-[460px] max-h-[80vh] overflow-hidden ${closing ? "anim-modal-out" : "anim-modal-in"}`}>
        <form
          onSubmit={submit}
          {...imeHandlers}
          onKeyDown={(e) => {
            // IME 変換確定 (Enter) を誤って submit させない
            if (e.key === "Enter" && isImeEnter(e)) {
              e.preventDefault();
              e.stopPropagation();
            }
          }}
        >
          <header className="flex items-center justify-between p-4 px-5 border-b border-(--border)">
            <div>
              <h2 className="text-[14px] font-semibold text-(--t1)">新規プロジェクト</h2>
              <p className="text-[11px] text-(--t3) mt-0.5">
                議事録を管理するプロジェクトを作成
              </p>
            </div>
            <button type="button" onClick={handleClose} className="icon-btn" title="閉じる">
              <X size={14} weight="bold" />
            </button>
          </header>

          {error && (
            <p className="mx-5 mt-3 text-[11px] text-(--danger)">{error}</p>
          )}

          <div className="p-5 overflow-y-auto" style={{ maxHeight: "calc(80vh - 150px)" }}>
            {step === 0 ? (
              <div className="flex flex-col gap-4">
                <Field label="プロジェクト名" required>
                  <input ref={ref} type="text" value={name} onChange={(e) => setName(e.target.value)}
                    className="input" placeholder="名前を入力" />
                </Field>
                <Field label="保存先" required hint="Markdown ファイルが書き出されます">
                  <FolderInput value={outputDir} onChange={setOutputDir}
                    placeholder="~/Documents/議事録" title="保存先を選択" />
                </Field>
                <Field label="リポジトリ" hint="任意。コンテキスト調査の対象">
                  <FolderInput value={repoPath} onChange={setRepoPath}
                    placeholder="任意" title="リポジトリを選択" />
                </Field>
              </div>
            ) : (
              <div className="flex flex-col gap-4">
                <Field label="ドキュメントフォルダ" hint="LLM がコンテキストとして参照">
                  <div className="flex flex-col gap-1">
                    {docDirs.map((dir, i) => (
                      <div key={`${dir}-${i}`} className="flex items-center gap-1 surface px-2 py-1.5">
                        <span className="flex-1 truncate text-[11px] text-(--t2)">{dir}</span>
                        <button type="button"
                          onClick={() => removeDocDir(i)}
                          className="icon-btn !w-6 !h-6 hover:!text-(--danger)"
                          title="削除">
                          <Trash size={11} weight="regular" />
                        </button>
                      </div>
                    ))}
                    <button type="button"
                      onClick={addDocDir}
                      className="w-full py-1.5 text-[11px] text-(--t3) bg-transparent border border-dashed border-(--border) rounded-md cursor-pointer hover:text-(--t2) hover:border-(--border-hover) flex items-center justify-center gap-1.5 transition-all">
                      <Plus size={11} weight="bold" />
                      フォルダを追加
                    </button>
                  </div>
                </Field>

                <Field label="用語集" hint="略語や固有名詞 (1行に1つ)">
                  <textarea value={glossary} onChange={(e) => setGlossary(e.target.value)} rows={4}
                    className="input"
                    style={{ resize: "vertical", lineHeight: 1.6 }}
                    placeholder="Supabase: BaaS&#10;OG画像: SNSプレビュー" />
                </Field>
              </div>
            )}
          </div>

          <footer className="flex items-center justify-between p-4 px-5 border-t border-(--border)">
            <div className="flex gap-1.5">
              {[0, 1].map((s) => (
                <button key={s} type="button" onClick={() => setStep(s)}
                  className="cursor-pointer rounded-full transition-all"
                  style={{
                    width: step === s ? 14 : 5,
                    height: 5,
                    background: step === s ? "var(--t2)" : "var(--t4)",
                    border: "none",
                    padding: 0,
                  }} />
              ))}
            </div>
            <div className="flex gap-1.5">
              <button type="button" onClick={handleClose} className="btn h-7 px-3 text-[11px]">
                キャンセル
              </button>
              {step === 0 ? (
                <button type="button" onClick={() => setStep(1)} disabled={!ok}
                  className="btn btn-primary h-7 px-3 text-[11px]">
                  次へ
                </button>
              ) : (
                <button type="submit" disabled={saving || !ok}
                  className="btn btn-primary h-7 px-3 text-[11px]">
                  {saving ? "保存中..." : "作成"}
                </button>
              )}
            </div>
          </footer>
        </form>
      </div>
    </div>
  );
}

function Field({ label, required, hint, children }: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-[11px] font-medium text-(--t2) mb-1">
        {label}
        {required && <span className="text-(--danger) ml-0.5">*</span>}
      </label>
      {children}
      {hint && <p className="text-[10px] text-(--t3) mt-1">{hint}</p>}
    </div>
  );
}

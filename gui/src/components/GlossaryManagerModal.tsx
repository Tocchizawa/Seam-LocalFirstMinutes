/* 用語集管理モーダル。
   保存形式は string[] のままだが、UI 上では {term, description} に parse して扱う。
   形式: "term" または "term: description" / "term：description"。
   - 検索 / 五十音ソート / インライン編集 / 重複検出 / チェックボックス選択での一括削除 */
import { useEffect, useMemo, useState } from "react";
import { X, MagnifyingGlass, Plus, Trash, WarningCircle } from "@phosphor-icons/react";
import { Select } from "./Select";
import { useImeSafeEnter } from "../lib/ime";

export interface GlossaryEntry {
  term: string;
  description: string;
}

interface Props {
  open: boolean;
  value: string[];
  onChange: (next: string[]) => void;
  onClose: () => void;
}

type SortKey = "added" | "alpha";

function parse(strings: string[]): GlossaryEntry[] {
  return strings.map((raw) => {
    const s = String(raw ?? "").trim();
    const a = s.indexOf(":");
    const b = s.indexOf("："); // 全角コロン
    const i = a < 0 ? b : b < 0 ? a : Math.min(a, b);
    if (i < 0) return { term: s, description: "" };
    return {
      term: s.slice(0, i).trim(),
      description: s.slice(i + 1).trim(),
    };
  });
}

function serialize(entries: GlossaryEntry[]): string[] {
  return entries
    .map((e) => {
      const t = e.term.trim();
      const d = e.description.trim();
      if (!t) return "";
      return d ? `${t}: ${d}` : t;
    })
    .filter(Boolean);
}

export function GlossaryManagerModal({ open, value, onChange, onClose }: Props) {
  const [closing, setClosing] = useState(false);
  const [entries, setEntries] = useState<GlossaryEntry[]>(() => parse(value));
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("added");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [newTerm, setNewTerm] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const { imeHandlers, isImeEnter } = useImeSafeEnter();

  // value が外部から変わったら反映 (例: AI 抽出で追加された後)
  useEffect(() => {
    if (open) setEntries(parse(value));
  }, [open, value]);

  // Esc で閉じる
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleClose = () => {
    setClosing(true);
    window.setTimeout(() => {
      setClosing(false);
      setQuery("");
      setSelected(new Set());
      setNewTerm("");
      setNewDesc("");
      onClose();
    }, 140);
  };

  // 重複検出 (term だけで判定)
  const duplicates = useMemo(() => {
    const count = new Map<string, number>();
    entries.forEach((e) => {
      const k = e.term.trim().toLowerCase();
      if (!k) return;
      count.set(k, (count.get(k) || 0) + 1);
    });
    return new Set(
      Array.from(count.entries()).filter(([, n]) => n >= 2).map(([k]) => k),
    );
  }, [entries]);

  // 検索 + ソート (元の index も保持)
  const view = useMemo(() => {
    const q = query.trim().toLowerCase();
    let indexed = entries.map((e, i) => ({ entry: e, originalIndex: i }));
    if (q) {
      indexed = indexed.filter(
        ({ entry }) =>
          entry.term.toLowerCase().includes(q) ||
          entry.description.toLowerCase().includes(q),
      );
    }
    if (sortKey === "alpha") {
      indexed = [...indexed].sort((a, b) =>
        a.entry.term.localeCompare(b.entry.term, "ja"),
      );
    }
    return indexed;
  }, [entries, query, sortKey]);

  const commit = (next: GlossaryEntry[]) => {
    setEntries(next);
    onChange(serialize(next));
  };

  const addEntry = () => {
    const t = newTerm.trim();
    if (!t) return;
    const next = [...entries, { term: t, description: newDesc.trim() }];
    commit(next);
    setNewTerm("");
    setNewDesc("");
  };

  const updateEntry = (idx: number, patch: Partial<GlossaryEntry>) => {
    const next = entries.map((e, i) => (i === idx ? { ...e, ...patch } : e));
    commit(next);
  };

  const removeEntry = (idx: number) => {
    const next = entries.filter((_, i) => i !== idx);
    commit(next);
    setSelected((prev) => {
      const ns = new Set<number>();
      prev.forEach((p) => {
        if (p < idx) ns.add(p);
        else if (p > idx) ns.add(p - 1);
      });
      return ns;
    });
  };

  const toggleSelect = (idx: number) => {
    setSelected((prev) => {
      const ns = new Set(prev);
      if (ns.has(idx)) ns.delete(idx);
      else ns.add(idx);
      return ns;
    });
  };

  const selectAllVisible = () => {
    setSelected(new Set(view.map((v) => v.originalIndex)));
  };
  const clearSelection = () => setSelected(new Set());

  const bulkDelete = () => {
    if (selected.size === 0) return;
    const next = entries.filter((_, i) => !selected.has(i));
    commit(next);
    setSelected(new Set());
  };

  if (!open) return null;

  return (
    <div className={`fixed inset-0 flex items-center justify-center z-50 ${closing ? "anim-modal-overlay-out" : "anim-modal-overlay-in"}`}>
      <div className="absolute inset-0 cursor-pointer" onClick={handleClose}
        style={{ background: "rgba(0,0,0,0.45)" }} />

      <div
        {...imeHandlers}
        className={`dialog-shell relative w-[640px] max-w-[92vw] max-h-[88vh] flex flex-col ${closing ? "anim-modal-out" : "anim-modal-in"}`}
      >
        <header className="flex items-center justify-between p-4 px-5 border-b border-(--border) shrink-0">
          <div>
            <h2 className="text-[14px] font-semibold text-(--t1)">用語集の管理</h2>
            <p className="text-[11px] text-(--t3) mt-0.5">
              {entries.length} 件{duplicates.size > 0 && (
                <span className="text-(--danger) ml-2">· 重複 {duplicates.size}</span>
              )}
            </p>
          </div>
          <button type="button" onClick={handleClose} className="icon-btn" title="閉じる">
            <X size={14} weight="bold" />
          </button>
        </header>

        {/* 検索 / ソート */}
        <div className="px-5 pt-3 pb-2 flex items-center gap-2 border-b border-(--border) shrink-0">
          <div className="flex items-center gap-1.5 flex-1 px-2.5 h-8 rounded-md border border-(--border) bg-(--surface)">
            <MagnifyingGlass size={12} weight="regular" className="text-(--t3) shrink-0" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="用語または説明で検索"
              className="flex-1 bg-transparent border-none outline-none text-[12px] text-(--t1) placeholder:text-(--t4)"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="text-(--t3) hover:text-(--t1)"
                title="クリア"
              >
                <X size={10} weight="bold" />
              </button>
            )}
          </div>
          <Select
            value={sortKey}
            onChange={(v) => setSortKey(v as SortKey)}
            options={[
              { value: "added", label: "追加順" },
              { value: "alpha", label: "五十音順" },
            ]}
            size="md"
            ariaLabel="並び順"
          />
        </div>

        {/* 新規追加 */}
        <div className="px-5 py-2.5 flex items-stretch gap-2 border-b border-(--border) shrink-0">
          <div className="flex-1 min-w-0 flex flex-col gap-1.5">
            <input
              type="text"
              value={newTerm}
              onChange={(e) => setNewTerm(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !isImeEnter(e)) {
                  e.preventDefault();
                  addEntry();
                }
              }}
              placeholder="用語"
              className="input h-8 text-[12px] font-medium"
            />
            <input
              type="text"
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !isImeEnter(e)) {
                  e.preventDefault();
                  addEntry();
                }
              }}
              placeholder="説明 (任意)"
              className="input h-8 text-[11px]"
            />
          </div>
          <button
            type="button"
            onClick={addEntry}
            disabled={!newTerm.trim()}
            className="btn btn-primary px-3 text-[11px] flex items-center gap-1 shrink-0 self-stretch"
          >
            <Plus size={11} weight="bold" />
            追加
          </button>
        </div>

        {/* 一括操作 (選択中のみ) */}
        {selected.size > 0 && (
          <div className="px-5 py-2 flex items-center gap-2 border-b border-(--border) shrink-0 bg-(--surface-2)">
            <span className="text-[11px] text-(--t2)">
              {selected.size} 件選択中
            </span>
            <button
              type="button"
              onClick={clearSelection}
              className="text-[11px] text-(--t3) hover:text-(--t1) px-2 py-0.5"
            >
              選択解除
            </button>
            <div className="ml-auto">
              <button
                type="button"
                onClick={bulkDelete}
                className="btn btn-ghost btn-danger h-7 px-2.5 text-[11px] flex items-center gap-1"
              >
                <Trash size={11} weight="regular" />
                一括削除
              </button>
            </div>
          </div>
        )}

        {/* リスト */}
        <div className="flex-1 overflow-y-auto px-5 py-2 min-h-0">
          {view.length === 0 ? (
            <div className="h-full flex items-center justify-center py-8">
              <p className="text-[11px] text-(--t3)">
                {query ? "該当する用語がありません" : "用語がまだ登録されていません"}
              </p>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 pb-1.5">
                <button
                  type="button"
                  onClick={selectAllVisible}
                  className="text-[10px] text-(--t3) hover:text-(--t1) px-1.5"
                >
                  表示中を全選択 ({view.length})
                </button>
              </div>
              <ul className="flex flex-col gap-1">
                {view.map(({ entry, originalIndex }) => {
                  const isDup = duplicates.has(entry.term.trim().toLowerCase());
                  const isSel = selected.has(originalIndex);
                  return (
                    <li
                      key={originalIndex}
                      className={`flex items-start gap-2 px-2.5 py-1.5 rounded-md border ${
                        isSel ? "border-(--accent) bg-(--surface-2)" : "border-(--border)"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isSel}
                        onChange={() => toggleSelect(originalIndex)}
                        className="mt-1.5"
                      />
                      <div className="flex-1 min-w-0 flex flex-col gap-1">
                        <div className="flex items-center gap-1.5">
                          <input
                            type="text"
                            value={entry.term}
                            onChange={(e) =>
                              updateEntry(originalIndex, { term: e.target.value })
                            }
                            className="input h-7 w-40 text-[12px] font-medium"
                            placeholder="用語"
                          />
                          {isDup && (
                            <span
                              className="text-(--danger) text-[10px] flex items-center gap-0.5"
                              title="この用語が複数登録されています"
                            >
                              <WarningCircle size={11} weight="fill" />
                              重複
                            </span>
                          )}
                        </div>
                        <input
                          type="text"
                          value={entry.description}
                          onChange={(e) =>
                            updateEntry(originalIndex, { description: e.target.value })
                          }
                          className="input h-7 text-[11px]"
                          placeholder="説明 (任意)"
                        />
                      </div>
                      <button
                        type="button"
                        onClick={() => removeEntry(originalIndex)}
                        className="icon-btn !w-7 !h-7 hover:!text-(--danger) shrink-0 mt-0.5"
                        title="削除"
                      >
                        <Trash size={11} weight="regular" />
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>

        <footer className="px-5 py-3 border-t border-(--border) flex items-center justify-end shrink-0">
          <button
            type="button"
            onClick={handleClose}
            className="btn h-8 px-4 text-[11px]"
          >
            閉じる
          </button>
        </footer>
      </div>
    </div>
  );
}

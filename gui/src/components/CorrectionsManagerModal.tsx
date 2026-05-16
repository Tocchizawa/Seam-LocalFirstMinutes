/* 誤転写 (wrong → correct) 管理モーダル。
   検索 / ソート / インライン編集 / チェックボックス選択での一括削除。 */
import { useEffect, useMemo, useState } from "react";
import { X, MagnifyingGlass, Plus, Trash, ArrowRight } from "@phosphor-icons/react";
import { Select } from "./Select";
import { useImeSafeEnter } from "../lib/ime";

export interface CorrectionPair {
  wrong: string;
  correct: string;
}

interface Props {
  open: boolean;
  value: CorrectionPair[];
  onChange: (next: CorrectionPair[]) => void;
  onClose: () => void;
}

type SortKey = "added" | "wrong" | "correct";

export function CorrectionsManagerModal({ open, value, onChange, onClose }: Props) {
  const [closing, setClosing] = useState(false);
  const [items, setItems] = useState<CorrectionPair[]>(value);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("added");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [newWrong, setNewWrong] = useState("");
  const [newCorrect, setNewCorrect] = useState("");
  const { imeHandlers, isImeEnter } = useImeSafeEnter();

  useEffect(() => {
    if (open) setItems(value);
  }, [open, value]);

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
      setNewWrong("");
      setNewCorrect("");
      onClose();
    }, 140);
  };

  const view = useMemo(() => {
    const q = query.trim().toLowerCase();
    let indexed = items.map((c, i) => ({ pair: c, originalIndex: i }));
    if (q) {
      indexed = indexed.filter(
        ({ pair }) =>
          pair.wrong.toLowerCase().includes(q) ||
          pair.correct.toLowerCase().includes(q),
      );
    }
    if (sortKey === "wrong") {
      indexed = [...indexed].sort((a, b) =>
        a.pair.wrong.localeCompare(b.pair.wrong, "ja"),
      );
    } else if (sortKey === "correct") {
      indexed = [...indexed].sort((a, b) =>
        a.pair.correct.localeCompare(b.pair.correct, "ja"),
      );
    }
    return indexed;
  }, [items, query, sortKey]);

  const commit = (next: CorrectionPair[]) => {
    setItems(next);
    onChange(next);
  };

  const addItem = () => {
    const w = newWrong.trim();
    const c = newCorrect.trim();
    if (!w || !c || w === c) return;
    commit([...items, { wrong: w, correct: c }]);
    setNewWrong("");
    setNewCorrect("");
  };

  const updateItem = (idx: number, patch: Partial<CorrectionPair>) => {
    commit(items.map((c, i) => (i === idx ? { ...c, ...patch } : c)));
  };

  const removeItem = (idx: number) => {
    commit(items.filter((_, i) => i !== idx));
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

  const selectAllVisible = () =>
    setSelected(new Set(view.map((v) => v.originalIndex)));
  const clearSelection = () => setSelected(new Set());

  const bulkDelete = () => {
    if (selected.size === 0) return;
    commit(items.filter((_, i) => !selected.has(i)));
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
            <h2 className="text-[14px] font-semibold text-(--t1)">誤転写ルールの管理</h2>
            <p className="text-[11px] text-(--t3) mt-0.5">
              {items.length} 件
            </p>
          </div>
          <button type="button" onClick={handleClose} className="icon-btn" title="閉じる">
            <X size={14} weight="bold" />
          </button>
        </header>

        <div className="px-5 pt-3 pb-2 flex items-center gap-2 border-b border-(--border) shrink-0">
          <div className="flex items-center gap-1.5 flex-1 px-2.5 h-8 rounded-md border border-(--border) bg-(--surface)">
            <MagnifyingGlass size={12} weight="regular" className="text-(--t3) shrink-0" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="誤転写または正式表記で検索"
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
              { value: "wrong", label: "誤転写順" },
              { value: "correct", label: "正式表記順" },
            ]}
            size="md"
            ariaLabel="並び順"
          />
        </div>

        <div className="px-5 py-2.5 flex items-center gap-2 border-b border-(--border) shrink-0">
          <input
            type="text"
            value={newWrong}
            onChange={(e) => setNewWrong(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !isImeEnter(e)) {
                e.preventDefault();
                addItem();
              }
            }}
            placeholder="誤った表記"
            className="input h-8 flex-1 text-[12px]"
          />
          <ArrowRight size={12} weight="bold" className="text-(--t3) shrink-0" />
          <input
            type="text"
            value={newCorrect}
            onChange={(e) => setNewCorrect(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !isImeEnter(e)) {
                e.preventDefault();
                addItem();
              }
            }}
            placeholder="正式表記"
            className="input h-8 flex-1 text-[12px]"
          />
          <button
            type="button"
            onClick={addItem}
            disabled={!newWrong.trim() || !newCorrect.trim() || newWrong.trim() === newCorrect.trim()}
            className="btn btn-primary h-8 px-3 text-[11px] flex items-center gap-1"
          >
            <Plus size={11} weight="bold" />
            追加
          </button>
        </div>

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

        <div className="flex-1 overflow-y-auto px-5 py-2 min-h-0">
          {view.length === 0 ? (
            <div className="h-full flex items-center justify-center py-8">
              <p className="text-[11px] text-(--t3)">
                {query ? "該当するルールがありません" : "ルールがまだ登録されていません"}
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
                {view.map(({ pair, originalIndex }) => {
                  const isSel = selected.has(originalIndex);
                  return (
                    <li
                      key={originalIndex}
                      className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border ${
                        isSel ? "border-(--accent) bg-(--surface-2)" : "border-(--border)"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isSel}
                        onChange={() => toggleSelect(originalIndex)}
                      />
                      <input
                        type="text"
                        value={pair.wrong}
                        onChange={(e) =>
                          updateItem(originalIndex, { wrong: e.target.value })
                        }
                        className="input h-7 flex-1 text-[12px]"
                        style={{ color: "var(--danger)" }}
                        placeholder="誤った表記"
                      />
                      <ArrowRight size={11} weight="bold" className="text-(--t3) shrink-0" />
                      <input
                        type="text"
                        value={pair.correct}
                        onChange={(e) =>
                          updateItem(originalIndex, { correct: e.target.value })
                        }
                        className="input h-7 flex-1 text-[12px]"
                        style={{ color: "var(--success)" }}
                        placeholder="正式表記"
                      />
                      <button
                        type="button"
                        onClick={() => removeItem(originalIndex)}
                        className="icon-btn !w-7 !h-7 hover:!text-(--danger) shrink-0"
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

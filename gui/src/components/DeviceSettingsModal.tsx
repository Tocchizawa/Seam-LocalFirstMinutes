import { useState, useEffect } from "react";
import { X, Microphone, SpeakerHigh } from "@phosphor-icons/react";
import type { AudioDevice } from "../lib/api";
import { listDevices } from "../lib/api";
import { Spinner } from "./Spinner";
import { Select } from "./Select";

interface Props {
  micDevice: number | null;
  captureSystem: boolean;
  onChangeMicDevice: (id: number | null) => void;
  onChangeCaptureSystem: (v: boolean) => void;
  onClose: () => void;
}

export function DeviceSettingsModal({
  micDevice, captureSystem, onChangeMicDevice, onChangeCaptureSystem, onClose,
}: Props) {
  const [closing, setClosing] = useState(false);
  const [devices, setDevices] = useState<AudioDevice[] | null>(null);
  const [sckAvailable, setSckAvailable] = useState(false);

  useEffect(() => {
    listDevices()
      .then((r) => { setDevices(r.devices); setSckAvailable(r.screen_capture_available); })
      .catch(() => setDevices([]));
  }, []);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") handleClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const handleClose = () => {
    setClosing(true);
    setTimeout(onClose, 180);
  };

  const mics = (devices ?? []).filter((d) => !d.is_blackhole);

  return (
    <div className={`fixed inset-0 flex items-center justify-center z-50 ${
      closing ? "anim-modal-overlay-out" : "anim-modal-overlay-in"
    }`}>
      <div className="absolute inset-0 cursor-pointer"
        onClick={handleClose}
        style={{ background: "rgba(0,0,0,0.45)" }} />

      <div className={`dialog-shell relative w-[420px] overflow-hidden ${
        closing ? "anim-modal-out" : "anim-modal-in"
      }`}>
        <header className="flex items-center justify-between p-4 px-5 border-b border-(--border)">
          <h2 className="text-[14px] font-semibold text-(--t1)">デバイス設定</h2>
          <button onClick={handleClose} className="icon-btn" title="閉じる">
            <X size={14} weight="bold" />
          </button>
        </header>

        <div className="p-5 flex flex-col gap-5">
          {!devices ? (
            <div className="flex justify-center py-8"><Spinner size={20} /></div>
          ) : (
            <>
              <div>
                <label className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-(--t3) mb-2">
                  <Microphone size={11} weight="regular" />
                  マイク
                </label>
                <Select
                  value={micDevice == null ? "" : String(micDevice)}
                  onChange={(v) => onChangeMicDevice(v ? Number(v) : null)}
                  options={mics.map((d) => ({
                    value: String(d.id),
                    label: `${d.name}${d.is_default ? "  (既定)" : ""}`,
                  }))}
                  className="w-full"
                />
              </div>

              {sckAvailable && (
                <div>
                  <label className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-(--t3) mb-2">
                    <SpeakerHigh size={11} weight="regular" />
                    内部音声
                  </label>
                  <Toggle
                    label="内部音声も録音"
                    hint="Zoom 等の相手の声も含める"
                    checked={captureSystem}
                    onChange={onChangeCaptureSystem}
                  />
                </div>
              )}
            </>
          )}
        </div>

        <footer className="flex items-center justify-end p-4 px-5 border-t border-(--border)">
          <button onClick={handleClose} className="btn h-7 px-3 text-[11px]">
            完了
          </button>
        </footer>
      </div>
    </div>
  );
}

function Toggle({ label, hint, checked, onChange }: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-3 cursor-pointer select-none">
      <span
        className="relative inline-flex items-center shrink-0 rounded-full mt-0.5 transition-colors duration-200"
        style={{
          width: 36,
          height: 20,
          background: checked ? "var(--accent)" : "var(--surface-3)",
        }}>
        <input type="checkbox" checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="absolute opacity-0 inset-0 cursor-pointer m-0" />
        <span
          className="block rounded-full bg-white shadow transition-transform duration-200"
          style={{
            width: 16,
            height: 16,
            transform: checked ? "translateX(18px)" : "translateX(2px)",
          }}
        />
      </span>
      <div>
        <p className="text-[12px] text-(--t1)">{label}</p>
        {hint && <p className="text-[10px] text-(--t3) mt-0.5">{hint}</p>}
      </div>
    </label>
  );
}

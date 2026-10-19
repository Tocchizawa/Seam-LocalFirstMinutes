import { useState } from "react";
import { liveRequest, type LiveNotesConfig } from "../../lib/live-notes";
import type { CliModelOption } from "../../lib/api";
import "./live-notes.css";

export function LiveNotesSettings({ value, onChange, inheritedProvider, recording, codexModels = [], claudeModels = [], inheritedModels = {} }: {
  value: LiveNotesConfig; onChange: (next: LiveNotesConfig) => void; inheritedProvider: string; recording: boolean;
  codexModels?: CliModelOption[]; claudeModels?: CliModelOption[];
  inheritedModels?: Partial<Record<"codex" | "claude_code", string>>;
}) {
  const [preflight, setPreflight] = useState("");
  const [checking, setChecking] = useState(false);
  const [customProvider, setCustomProvider] = useState<string | null>(null);
  const provider = value.provider === "inherit" ? inheritedProvider : value.provider;
  const valid = provider === "codex" || provider === "claude_code";
  const choices = provider === "codex" ? codexModels : claudeModels;
  const model = valid ? value.models[provider] : "";
  const inheritedModel = valid ? inheritedModels[provider] || "CLIの既定モデル" : "";
  const customModel = customProvider === provider || !!model && !choices.some((choice) => choice.slug === model);
  const update = (patch: Partial<LiveNotesConfig>) => onChange({ ...value, ...patch });
  const check = async () => {
    setChecking(true); setPreflight("");
    try {
      const result = await liveRequest<Record<string, unknown>>(`/preflight?provider=${encodeURIComponent(provider)}`);
      setPreflight(result.ok ? `制限されたCLI実行機能を確認しました${result.version ? `（${String(result.version)}）` : ""}。認証状態と利用枠は推論を開始するときに確認します。` : `CLIを利用できません: ${String(result.error || "必要な実行制限に対応していません")}`);
    } catch (cause) { setPreflight(cause instanceof Error ? cause.message : "CLIの確認に失敗しました"); }
    finally { setChecking(false); }
  };
  return <section className="live-note-settings" aria-labelledby="live-notes-settings-title">
    <h3 id="live-notes-settings-title">リアルタイム議論ノート</h3>
    <p>会議中の確定発話を論点ごとに蓄積します。手動編集した本文・見出しはAIの上書きから保護されます。設定は保存後、次の会議から反映されます。</p>
    {recording && <p>録音中の方式・provider・modelは会議開始時の設定を維持します。</p>}
    <label>次の録音でライブノートを使用<input type="checkbox" checked={value.enabled} onChange={(event) => update({ enabled: event.target.checked })} /></label>
    <label>ライブノートのCLI<select value={value.provider} onChange={(event) => { update({ provider: event.target.value as LiveNotesConfig["provider"] }); setPreflight(""); }}>
      <option value="inherit">要約設定から継承</option><option value="codex">Codex CLI</option><option value="claude_code">Claude Code CLI</option>
    </select></label>
    {valid && <>
      <label>ライブノートのモデル<select value={customModel ? "__custom__" : model} onChange={(event) => {
        if (event.target.value === "__custom__") { setCustomProvider(provider); return; }
        setCustomProvider(null);
        update({ models: { ...value.models, [provider]: event.target.value } }); setPreflight("");
      }}>
        <option value="">要約設定から継承（{inheritedModel}）</option>
        {choices.map((choice) => <option key={choice.slug} value={choice.slug}>{choice.display_name}{choice.slug === "gpt-6-luna" ? "（軽量・ライブ推奨）" : ""}</option>)}
        <option value="__custom__">モデルIDを指定…</option>
      </select></label>
      {customModel && <label>ライブノートのモデルID<input type="text" maxLength={160} value={model} placeholder={provider === "codex" ? "gpt-6-luna" : "haiku"} onChange={(event) => { update({ models: { ...value.models, [provider]: event.target.value.trim() } }); setPreflight(""); }} /></label>}
      <p>次の会議で使用：{model || inheritedModel}。録音後要約とは別のモデルを選べます。</p>
      <p>{provider === "codex" ? "頻繁なノート更新には軽量なGPT-6 Luna（gpt-6-luna）を推奨します。候補にない場合はモデルIDを指定できます。候補はCodexのローカルキャッシュに基づき、利用可否はアカウント・CLIの版によって異なります。" : "頻繁な更新では軽量なHaikuから試せます。精度が足りない場合はSonnetなどを選択してください。"}重要な決定・アクションは内容を確認してください。</p>
    </>}
    <p>既存のCLIログイン・ランチャー設定を使用します。APIキーの追加は不要です。CLIの契約・利用枠・サービス側のデータ取り扱いが適用され、無料・無制限を保証しません。</p>
    {!valid ? <p role="status">継承先はCodex CLI / Claude Code CLIではありません。CLIを選択するまでAIは動作せず、録音と手動編集は継続できます。</p> : <label className="live-note-consent">
      <input type="checkbox" checked={value.consent[provider]} onChange={(event) => update({ consent: { ...value.consent, [provider]: event.target.checked } })} />
      <span>音声認識は端末上で実行されます。確定発話と関連ノートの文脈を{provider === "codex" ? "OpenAI（Codex）" : "Anthropic（Claude Code）"}へ送信することに同意します。ローカルのコード・資料は読み取らせません。同意前はAIへ送信しません。</span>
    </label>}
    <label>目標更新間隔（秒）<input type="number" min={value.min_interval_sec} max={value.max_wait_sec} value={value.target_interval_sec} onChange={(event) => update({ target_interval_sec: Math.max(value.min_interval_sec, Math.min(value.max_wait_sec, Number(event.target.value) || 35)) })} /></label>
    <label>1時間の呼び出し上限<input type="number" min={1} max={180} value={value.max_calls_per_hour} onChange={(event) => update({ max_calls_per_hour: Math.max(1, Math.min(180, Number(event.target.value) || 90)) })} /></label>
    <p>最短{value.min_interval_sec}秒・最大待機{value.max_wait_sec}秒を目安に適応バッチ化し、同時に1件だけ実行します。修復・再試行も上限に含みます。CLIエラー時も発話を保存し、録音を継続します。</p>
    <button type="button" className="btn" disabled={!valid || checking} onClick={() => { void check(); }}>{checking ? "確認中…" : "CLIの利用可否を確認（推論なし）"}</button>
    <p>確認には現在保存されているCLI設定を使用します。モデル・ランチャーを変更した場合は先に保存してください。</p>
    {preflight && <pre className="live-note-settings-output" role="status">{preflight}</pre>}
  </section>;
}

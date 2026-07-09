# 要約機能 — 設計ドキュメント (v2 確定版)

## 1. 目的

会議の文字起こしから議事録要約をAIで自動生成する。プロバイダ切替式 (ローカル/クラウド) でユーザーの環境とプライバシー要件に応じて柔軟に対応する。

## 2. 要件

| 項目 | 内容 |
|---|---|
| **既定動作** | 録音停止 → 自動的に要約生成 (provider未設定時はskip) |
| **手動再生成** | 既存議事録に対して再要約ボタン (provider切替も可能) |
| **v1 プロバイダ** | Ollama (Qwen3 4B/8B/14B/32B) / Claude API / OpenAI / Gemini |
| **CLI プロバイダ** | Claude Code CLI / Codex CLI (subprocess + 起動前 preflight + 出力パース) |
| **出力形式** | Markdown (構造化セクション) |
| **プライバシー** | クラウド利用はprovider初使用時に1度だけ同意モーダル。Ollamaは送信なし |
| **UX** | 生成中はストリーミング表示、完了時に保存 |
| **初回起動** | 利用可能provider自動検出 → 推奨提示 |

## 3. アーキテクチャ

### 3.1 モジュール構成 (v1)

```
src/summarize/
├── __init__.py             # registry/runner/types を re-export
├── base.py                 # SummarizerProvider Protocol + 共通型 + エラーコード
├── prompts.py              # 要約プロンプトテンプレート + token見積り
├── registry.py             # provider 名 → factory 関数 + auto-detect
├── runner.py               # 非同期ジョブキュー + 実行制御 + cancel + draft保存
├── ollama.py               # Ollama HTTP API
├── claude_api.py           # Anthropic Messages API + prompt caching
├── openai_api.py           # OpenAI Chat Completions API
└── gemini_api.py           # Google Generative AI API
```

CLI provider: `claude_code.py` / `codex.py` (CLI subprocess)

### 3.2 主要型

```python
# base.py

class SummaryErrorCode(str, Enum):
    AUTH_FAILED = "AUTH_FAILED"            # 401/403
    RATE_LIMIT = "RATE_LIMIT"              # 429
    PROVIDER_DOWN = "PROVIDER_DOWN"        # 5xx
    TIMEOUT = "TIMEOUT"
    OFFLINE = "OFFLINE"                    # connection refused
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE" # ollama: pull required
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"   # 事前検証で送信前に弾く
    OUTPUT_FORMAT = "OUTPUT_FORMAT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

class SummaryError(Exception):
    def __init__(self, code: SummaryErrorCode, message: str, *,
                 provider: str | None = None):
        self.code = code
        self.message = message
        self.provider = provider

@dataclass
class ProjectContext:
    name: str
    members: list[dict]      # [{name, role}]
    glossary: list[str]      # ["Term: definition", ...]

@dataclass
class SummaryResult:
    text: str
    provider: str            # "ollama"
    model: str               # "qwen3:8b"
    input_chars: int
    output_chars: int
    duration_sec: float

@dataclass
class ProviderHealth:
    ok: bool
    code: str                # "READY" | "NO_API_KEY" | "MODEL_NOT_LOADED" | "OFFLINE"
    message: str

class SummarizerProvider(Protocol):
    name: str
    
    async def health_check(self) -> ProviderHealth: ...
    
    async def generate(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        on_token: Callable[[str], None] | None = None,
        timeout_sec: float = 300,
    ) -> SummaryResult: ...  # raises SummaryError on failure
    
    async def cancel(self, *, reason: str = "user_cancelled") -> None: ...
```

### 3.3 ジョブランナー (runner.py)

```python
class SummaryRunner:
    """
    - asyncio.Queue でジョブをシリアル消費 (concurrency=1)
    - in-memory に partial text を保持 (status API用)
    - 1ジョブ = 1議事録の要約生成
    - 結果をDBに書き戻し + WS通知
    - 連打debounce: 同一minutes_idがin-flightなら既存をcancelして新ジョブ採用
    - draft保存: 生成成功後DB更新前に ~/.seam/summary_drafts/{id}.md に保存し、
      DB成功後に削除。失敗時はdraftが残るので次回起動時にリカバリ可
    """
    def enqueue(self, minutes_id: str, *, provider_name: str | None = None) -> None: ...
    def get_status(self, minutes_id: str) -> JobStatus: ...
    async def cancel(self, minutes_id: str) -> None: ...

@dataclass
class JobStatus:
    minutes_id: str
    state: Literal["queued", "running", "done", "failed", "cancelled"]
    provider: str | None
    partial_text: str = ""    # 生成中の部分テキスト (UI再接続時に取得)
    error_code: str | None = None
    error_message: str | None = None
```

### 3.4 Provider auto-detect (registry.py)

初回起動 / Settings画面初訪問時:

```python
async def auto_detect_recommended() -> str | None:
    """利用可能なproviderを優先順で検出して推奨を返す。
    
    優先順位:
      1. Ollama reachable + qwen3 系モデルpull済み → "ollama"
      2. Claude API key keyring保存済み → "claude_api"
      3. OpenAI API key keyring保存済み → "openai"
      4. Gemini API key keyring保存済み → "gemini"
      5. Ollama reachable (モデル無し)→ "ollama" (要pull案内)
      6. None → 設定促す
    """
```

## 4. データフロー

### 4.1 自動要約フロー

```
[録音停止]
   ↓
[pipeline 実行 (mix/whisper/diarize)]
   ↓
[db.insert_minutes(summary="")]   ← 既存
   ↓
[ws.broadcast pipeline_done]      ← 既存
   ↓
[summary_runner.enqueue(minutes_id)]   ← 新規
   ↓ (バックグラウンドworker)
   ↓
[runner ワーカー]
   ├─ minutes_id → project_id を引いて projects.yaml から ProjectContext 構築
   ├─ provider解決 (config or 引数)
   ├─ provider.health_check() で事前検証
   ├─ prompts.build_prompt(transcript, project) でprompt構築
   ├─ tokens.estimate() で context長検証
   │   └─ 超過時 → SummaryError(CONTEXT_OVERFLOW) → ws.broadcast summary_failed
   ├─ provider.generate(transcript, on_token=ws_stream)
   │   └─ on_token ごとに ws.broadcast summary_chunk + partial_text 蓄積
   ├─ draft保存 (~/.seam/summary_drafts/{id}.md)
   ├─ db.update_minutes(id, summary=text, llm_model=f"{provider}:{model}")
   ├─ draft削除
   └─ ws.broadcast summary_done
```

### 4.2 手動再要約フロー

```
[UI: "再要約" ボタン]
   ↓ (provider選択ポップオーバー)
[POST /api/minutes/{id}/summarize?provider=claude_api]
   ↓
[runner.enqueue(id, provider_name=claude_api)]
   ├─ in-flightジョブがあればcancel
   └─ 新ジョブで上書き (draft保存 + DB上書き)
```

### 4.3 起動時リカバリ

```
[アプリ起動]
   ↓
[~/.seam/summary_drafts/ をスキャン]
   ↓
[各 {id}.md について]
   ├─ DBの該当minutes.summary が空 → draftの内容で埋める
   └─ DB summary既に値あり → draft破棄 (古い失敗ジョブの残骸)
```

## 5. 設定スキーマ

### 5.1 config.yaml (非機密)

```yaml
minutes_ai:
  provider: "ollama"            # ollama | claude_api | openai | gemini
  
  auto_generate: true           # 録音停止時に自動要約 (推奨ON)
  timeout_sec: 300
  
  consent:                      # 初回利用同意フラグ (provider別)
    claude_api: false
    openai: false
    gemini: false
  
  ollama:
    base_url: "http://localhost:11434"
    model: "qwen3:8b"           # qwen3:4b | qwen3:8b | qwen3:14b | qwen3:32b
    num_ctx: 8192
    keep_alive_sec: 0
    num_thread: 1
    num_batch: 8
    num_gpu: 0
    low_vram: true
    dynamic_budget_enabled: true
    spare_usage_ratio: 0.6
  
  claude_api:
    model: "claude-sonnet-4-6"
    max_tokens: 4096
    use_prompt_caching: true
  
  openai:
    model: "gpt-4o-mini"
    max_tokens: 4096
  
  gemini:
    model: "gemini-2.0-flash"
    max_tokens: 4096

  claude_code:
    binary_path: "claude"
    model: "sonnet"
    launcher_command: ""       # zsh function 等が必要な場合だけ指定
    launcher_shell: "/bin/zsh"
    launcher_interactive: true
    connect_timeout_sec: 12
    extra_args: []

  codex:
    binary_path: "codex"
    model: ""                  # 空なら CLI 側の既定モデル
    launcher_command: ""
    launcher_shell: "/bin/zsh"
    launcher_interactive: true
    connect_timeout_sec: 12
    extra_args: []

# 旧 ollama.minutes_* 設定は migration で minutes_ai.ollama.* へ移動
ollama:
  base_url: "http://localhost:11434"
  context_model: "qwen3:8b"     # agent用 (別feature) — そのまま残す
  auto_start: true
  auto_install: true
```

### 5.2 Config migration (config.py の _normalize 拡張)

```python
def _migrate_minutes_ai(self) -> None:
    """v1 → v2 設定移行。
    旧: ollama.minutes_model, minutes_num_ctx, minutes_num_*
    新: minutes_ai.ollama.model, num_ctx, num_*
    """
    old = self._data.get("ollama", {})
    new_ai = self._data.setdefault("minutes_ai", {})
    new_ollama = new_ai.setdefault("ollama", {})
    
    # フィールド名のマッピング
    mapping = {
        "minutes_model": "model",
        "minutes_num_ctx": "num_ctx",
        "minutes_num_thread": "num_thread",
        "minutes_num_batch": "num_batch",
        "minutes_num_gpu": "num_gpu",
        "minutes_low_vram": "low_vram",
        "minutes_dynamic_budget_enabled": "dynamic_budget_enabled",
        "minutes_spare_usage_ratio": "spare_usage_ratio",
        "minutes_keep_alive_sec": "keep_alive_sec",
    }
    for old_key, new_key in mapping.items():
        if old_key in old and new_key not in new_ollama:
            new_ollama[new_key] = old.pop(old_key)
```

### 5.3 keyring (機密)

| サービス | キー | 値 |
|---|---|---|
| `seam-app` | `claude_api_key` | sk-ant-... |
| `seam-app` | `openai_api_key` | sk-... |
| `seam-app` | `gemini_api_key` | AIza... |
| `seam-app` | `hf_token` | (既存) |

## 6. プロンプト設計

### 6.1 共通システムプロンプト

```
あなたは日本語の会議議事録作成アシスタントです。
以下の文字起こしから、構造化された議事録を作成してください。

# 出力形式 (Markdown 厳守)
## 概要
1-2文で会議の目的と結論を記述。

## 決定事項
- 各決定を箇条書きで。決定者・期限が明確なら付記。

## TODO
- [担当者] タスク内容 (期限: YYYY-MM-DD)
- 担当者不明なら [未定]、期限不明なら期限部分を省略。

## 議論ハイライト
- 重要な議論や反対意見を3-5項目で。

# 制約
- 文字起こしに無い情報を捏造しない。
- 推測には「(議論より)」等を付記。
- 文字起こし内の話者ラベル (例: 田中, 話者2) はそのまま使用する。
- 専門用語は glossary に従って表記を統一。
```

(変更点: §11 issue #1 反映 — "実名置換"の記述を削除、"そのまま使用"に変更)

### 6.2 ユーザープロンプトに注入

```
# プロジェクト: {project.name}

# 参加者
- 田中 太郎 (リード)
- 佐藤 花子 (PM)

# 用語集
- Supabase: BaaS
- OG画像: SNSプレビュー

# 文字起こし
[00:00] (田中) ...
[00:12] (佐藤) ...
```

`members` は「会議の登場人物」のヒントとしてのみ提示。LLMが文字起こし内ラベルとmembersの対応を文脈推論で判断する。

### 6.3 Token見積り (prompts.py)

```python
# 日本語混在テキストの大まかな目安: 1 char ≒ 0.7 token
# Whisper transcript は割と密で 1 char ≒ 0.8 token
def estimate_tokens_jp(text: str) -> int:
    return int(len(text) / 1.3)   # 安全側に小さめ

def validate_context_budget(
    transcript: str, system_prompt: str, max_output: int, ctx_window: int,
) -> None:
    needed = (
        estimate_tokens_jp(transcript)
        + estimate_tokens_jp(system_prompt)
        + max_output
        + 256  # safety margin
    )
    if needed > ctx_window:
        raise SummaryError(
            SummaryErrorCode.CONTEXT_OVERFLOW,
            f"context長 {ctx_window} に対し {needed} tokens必要。"
            f"より大きいnum_ctx, またはClaude/Gemini等の長contextモデルを使用してください。"
        )
```

### 6.4 長尺対応 (v2 で対応)

3時間超: map-reduce
- map: 30分ごとにチャンク要約
- reduce: チャンク要約を統合
- v1 では `CONTEXT_OVERFLOW` エラーで通知のみ

## 7. API 設計

### 7.1 新規エンドポイント

```
POST /api/minutes/{id}/summarize
  Query: provider? (省略時は config の provider)
  Response: 202 { job_id: minutes_id, status: "queued" }

GET /api/minutes/{id}/summarize/status
  Response: {
    state: "queued" | "running" | "done" | "failed" | "cancelled",
    provider, partial_text, error_code, error_message
  }

POST /api/minutes/{id}/summarize/cancel
  Response: { state: "cancelled" }

PUT /api/summarize/api-key
  Body: { provider: "claude_api" | "openai" | "gemini", token: "..." }

DELETE /api/summarize/api-key/{provider}

POST /api/summarize/test
  Body: { provider }
  Response: { ok, code, message }

POST /api/summarize/consent/{provider}    # 初回同意マーク
  Response: { ok }

GET /api/summarize/recommended
  Response: { provider: "ollama" | ..., reason }
```

### 7.2 WS イベント

```typescript
{type: "summary_chunk", data: {minutes_id, text, total_chars}}
{type: "summary_done",  data: {minutes_id, model, duration_sec}}
{type: "summary_failed", data: {minutes_id, error_code, message}}
{type: "summary_cancelled", data: {minutes_id}}
```

## 8. UI 設計

### 8.1 設定画面 (新カテゴリ "要約 AI")

初回:
- Provider auto-detect 結果に基づき推奨を highlight
- "Ollama が利用可能です" / "API キー未設定です" 等のhint

通常表示:

```
┌ 要約 AI ─────────────────────────────────┐
│ プロバイダ                                  │
│  ◉ Ollama (ローカル)         ✓ Ready      │
│  ○ Claude API                ⚠ APIキー要  │
│  ○ OpenAI                                │
│  ○ Gemini                                │
│  (Claude Code / Codex は v2)              │
│                                           │
│ ─ Ollama 設定 ─                           │
│ モデル:  [qwen3:8b ▾]                     │
│   ├─ qwen3:4b  (~2.5GB, 軽量)             │
│   ├─ qwen3:8b  (~5GB, 既定)               │
│   ├─ qwen3:14b (~9GB, 推奨32GB+ Mac)      │
│   └─ qwen3:32b (~20GB, 推奨64GB+ Mac)     │
│ コンテキスト長:  [8192]                    │
│                                           │
│ [接続テスト]                               │
│                                           │
│ □ 録音停止時に自動要約 (推奨)              │
│ タイムアウト: [300] 秒                     │
└──────────────────────────────────────────┘
```

クラウド系を選んで未同意:

```
┌ Claude API 同意 ────────────────────────┐
│ ⚠️ 文字起こしテキストが Anthropic API に   │
│    送信されます。                          │
│                                           │
│ 概算コスト: 60分会議1件 ≈ $0.05            │
│ (Sonnet 4.6 / 入力10K + 出力1K tokens)   │
│                                           │
│         [理解した上で利用する] [戻る]      │
└──────────────────────────────────────────┘
```

同意後にAPI key登録UI出現。consent flagはconfig.yaml永続化、provider別1回のみ。

### 8.2 詳細画面 (DetailView) 要約セクション

| 状態 | 表示 |
|---|---|
| 生成中 (partial有) | spinner + "要約生成中... (Claude API)" + ストリーミングテキスト |
| 生成中 (partial無) | spinner + "要約生成を開始しています..." (model loading等) |
| 完了 | Markdownレンダー + footer "Claude Sonnet 4.6 / 12秒で生成 [再要約 ▾]" |
| 失敗 | エラーコード + メッセージ + [再試行] ボタン |
| 未生成 (provider未設定) | "要約プロバイダが未設定です [設定を開く]" |
| 未生成 (有効) | "要約はまだ生成されていません [要約を生成]" |

ヘッダー右に **再要約ボタン** (▾でprovider切替可能なpopover)。

### 8.3 再接続時の状態復元

UIマウント / WS再接続時の処理:
1. `GET /api/minutes/{id}/summarize/status` でin-flight確認
2. state==running なら partial_text を初期表示 + WS subscribe
3. state==done なら DB の summary を表示
4. state==failed なら error表示

## 9. エッジケース

| ケース | 挙動 |
|---|---|
| 文字起こしが空/極小 (< 50文字) | 要約スキップ、`summary` は "" のまま、UI は "発話が短すぎて要約できません" |
| Ollama 未起動 | health_check失敗 → SummaryError(OFFLINE) → UI 起動方法を案内 |
| Ollama モデル未pull | SummaryError(MODEL_UNAVAILABLE, "qwen3:8b が未pull")→ UI に `ollama pull qwen3:8b` 案内 (自動pullはしない) |
| API キー無効 (401/403) | SummaryError(AUTH_FAILED) → トークンは消さずUI再入力促す |
| Rate limit (429) | 30秒後に1回リトライ。再失敗で SummaryError(RATE_LIMIT) |
| context長超過 | 送信前のtoken見積りで SummaryError(CONTEXT_OVERFLOW) |
| タイムアウト | SummaryError(TIMEOUT) → 再試行ボタン |
| CLI provider の未ログイン/接続不可 | 要約ジョブ投入前の短い preflight で AUTH_FAILED/OFFLINE/TIMEOUT を返す |
| CLI binary が shell 起動直後の PATH に無い | 実行前に login shell の PATH を再取得し、shell の command hash を clear してから起動 |
| 連打で再要約 | runner で in-flightチェック → 既存ジョブ cancel → 新ジョブ採用 |
| 生成中に対象議事録削除 | runner cancel + WS notify、draft削除 |
| アプリ強制終了 | 起動時リカバリで draft → DB復元 (§4.3) |
| WS切断 → 再接続 | UI が status APIで partial_text取得 → ストリーミング再開 |
| 要約中に新規録音停止 | 新ジョブをqueue末尾投入、シリアル処理 |
| provider未設定 + auto_generate=true | skipしログ出力、UI に設定促すバナー |
| consent未取得provider利用試行 | 同意モーダル提示、未同意ならジョブ作らない |

## 10. テスト戦略

### 10.1 ユニットテスト (`tests/summarize/`)

```
test_prompts.py        # token見積り精度、prompt構築の正しさ
test_ollama.py         # HTTPモック (httpx_mock)、stream parsing、cancel
test_claude_api.py     # SDK モック (anthropic.AsyncAnthropic)、cache_control確認
test_openai.py         # SDK モック (openai.AsyncOpenAI)
test_gemini.py         # SDK モック
test_runner.py         # キュー直列処理、cancel、debounce、in-flight
test_registry.py       # auto-detect 各分岐
test_recovery.py       # draft → DB リカバリ
```

### 10.2 結合テスト

```
test_pipeline_integration.py
  - pipeline_done イベント → runner.enqueue → DB update まで
  - WS broadcast 順序検証
  - Ollama実プロセス起動時のend-to-end
```

### 10.3 E2Eテスト (手動 + 自動)

- 録音停止 → 自動要約 → DetailView表示まで
- 再要約 → provider切替動作
- API key 入力 → consent → 要約生成
- error scenarios (Ollama停止、API key無効) のUI確認

## 11. 実装フェーズ (確定)

| # | フェーズ | スコープ | テスト |
|---|---|---|---|
| **1** | 基盤 | base.py / registry / runner / config migration / draft recovery / token見積り | unit: prompts/runner/registry/recovery |
| **2** | Ollama provider | ollama.py + Qwen3 model selection (4B/8B/14B/32B) + health_check + cancel | unit: ollama / 結合: pipeline E2E |
| **3** | UI 基盤 | Settings要約AIカテゴリ + DetailView要約セクション + WS subscribe + 自動トリガー + 状態復元 | E2E: 録音停止→自動要約→UI |
| **4** | Claude API | claude_api.py + APIキー管理 + 同意モーダル + prompt caching | unit: claude_api / E2E: 同意→生成 |
| **5** | OpenAI / Gemini | openai_api.py + gemini_api.py + 同様UI拡張 | unit / E2E |
| **6** | 再要約 + auto-detect | API endpoint + UI再要約popover + provider auto-detect on first run | E2E: 既存議事録の再要約 |
| **7** | エッジケース統合 | §9 全ケース、エラーUI整備 | エラーシナリオ網羅 |

**後続実装フロー**:
- Phase next.1: 長尺対応 map-reduce chunking
- Phase next.2: cost tracking / usage metrics

## 12. 確定済み決定事項

| # | 項目 | 決定 |
|---|---|---|
| 1 | 自動要約デフォルト | **ON** (provider未設定時はskip) |
| 2 | バックフィル | **手動のみ** (起動時自動再生成はしない) |
| 3 | クラウド同意モーダル | **provider初使用時の1回のみ** (config永続化) |
| 4 | 失敗時summary | **空のまま** (FTSノイズ回避) |
| 5 | llm_model列形式 | `provider:model` (例 `claude_api:claude-sonnet-4-6`) |
| 6 | Ollama自動pull | **しない**、エラーでpullコマンド案内 |
| 7 | context overflow | v1 はエラー通知のみ、chunkingはv2 |
| 8 | APIキー保存 | 既存 `seam-app` keyring 共用 |
| 9 | CLI agents | **v2 へ後送り** |
| 10 | provider auto-detect | **初回起動時に自動実行** |
| 11 | 同時実行 | concurrency=1 シリアル (cloud並列化はv2検討) |
| 12 | 話者→実名マッピング | **しない**、文字起こし内ラベルそのまま使用 |

---

承認後、フェーズ1 から実装に入ります。

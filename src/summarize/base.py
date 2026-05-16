"""要約provider層の共通型・エラー定義。

各providerは ``SummarizerProvider`` Protocol を実装する。
ジョブランナーは provider 名で registry から factory を引き、
``health_check`` → ``generate`` → 失敗時 ``cancel`` の順に呼ぶ。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


class SummaryErrorCode(str, Enum):
    AUTH_FAILED = "AUTH_FAILED"               # API key invalid (401/403)
    RATE_LIMIT = "RATE_LIMIT"                 # 429
    PROVIDER_DOWN = "PROVIDER_DOWN"           # 5xx
    TIMEOUT = "TIMEOUT"
    OFFLINE = "OFFLINE"                       # connection refused / network down
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"   # ollama: pull required
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"     # 事前検証で送信前に弾く
    OUTPUT_FORMAT = "OUTPUT_FORMAT"           # Markdown構造違反
    CANCELLED = "CANCELLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"         # provider未設定 / consent未取得
    UNKNOWN = "UNKNOWN"


class SummaryError(Exception):
    """provider層から runner / API へ伝搬する正規化エラー。"""

    def __init__(
        self,
        code: SummaryErrorCode,
        message: str,
        *,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "provider": self.provider,
        }


@dataclass
class ProjectContext:
    """要約プロンプトに注入するプロジェクトコンテキスト。

    Note:
        members → 「会議の登場人物」のヒントとしてのみ提示。
        speaker_id ↔ member_name の対応は LLM の文脈推論に委ねる。

    repo_path / doc_dirs は CLI agent (claude_code / codex) では
    --add-dir flag で渡し、agent が必要に応じて自分で Read/Grep する。
    API providers (claude_api / openai / gemini) は subprocess を持たないため、
    現状は prompt に paths を「参照可能なドキュメント」として案内するのみで、
    実際にファイルを読む処理は behaviour の対象外 (案B / 案C 実装で対応)。
    """

    name: str = ""
    members: list[dict[str, str]] = field(default_factory=list)
    glossary: list[str] = field(default_factory=list)
    repo_path: str | None = None
    doc_dirs: list[str] = field(default_factory=list)


@dataclass
class SummaryResult:
    text: str
    provider: str       # "ollama" | "claude_api" | ...
    model: str          # "qwen3:8b" | "claude-sonnet-4-6" | ...
    input_chars: int
    output_chars: int
    duration_sec: float


@dataclass
class ProviderHealth:
    """provider が要約可能な状態か事前確認するための結果。

    code はUI側で error_code として共通扱いするため SummaryErrorCode に揃える。
    ``READY`` は ok=True 時のみ使う特殊値。
    """

    ok: bool
    code: str           # "READY" | SummaryErrorCode.* の値
    message: str
    model: str | None = None


# 1ストリーム1tokenごとに呼ばれるコールバック。
# WebSocket 経由でUIへ部分テキストを broadcast する用途。
TokenCallback = Callable[[str], None] | Callable[[str], Awaitable[None]]

# 生成中の活動 (例: "Read: KNOWLEDGE.md", "Thinking...") を UI に伝えるためのコールバック。
# 主に CLI provider (claude_code / codex) の tool 使用や進捗ログを surface する用途。
ActivityCallback = Callable[[str], None] | Callable[[str], Awaitable[None]]


@runtime_checkable
class SummarizerProvider(Protocol):
    """要約provider共通インターフェース。

    実装は async generator ではなく ``on_token`` コールバック方式を採用。
    呼び出し側 (runner) で WS broadcast / partial_text 蓄積を一元化するため。
    """

    name: str

    async def health_check(self) -> ProviderHealth:
        """provider が要約可能な状態か確認する。"""
        ...

    async def generate(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        on_token: TokenCallback | None = None,
        on_activity: ActivityCallback | None = None,
        timeout_sec: float = 300,
    ) -> SummaryResult:
        """要約を生成する。失敗時は SummaryError を raise する。

        on_token は token (or 細切れの部分テキスト) ごとに呼ばれる。
        on_activity は生成中の活動 (Read / Tool 使用 / Thinking 等) を UI に
        伝える。サポート外の provider は呼ばなくてよい。
        どちらも sync / async どちらでも受け付けられる。
        """
        ...

    async def generate_title(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        timeout_sec: float = 60,
    ) -> str:
        """会議タイトルを1行 (30文字以内) 生成して返す。

        失敗時 (provider 未対応・通信エラー等) は SummaryError を raise する。
        on_token は使わない (短いので一括取得で良い)。
        """
        ...

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        """進行中の generate をキャンセルする。

        通信を中断すること、subprocess を terminate すること、
        いずれの場合も await 完了時には generate がエラーで返って良い状態にする。
        """
        ...


# provider 識別子の定数 (typo 防止)
PROVIDER_OLLAMA = "ollama"
PROVIDER_CLAUDE_API = "claude_api"
PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"
PROVIDER_CLAUDE_CODE = "claude_code"
PROVIDER_CODEX = "codex"

V1_PROVIDERS: tuple[str, ...] = (
    PROVIDER_OLLAMA,
    PROVIDER_CLAUDE_API,
    PROVIDER_OPENAI,
    PROVIDER_GEMINI,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_CODEX,
)

# CLI subprocess を起動する provider (APIキー不要)
CLI_PROVIDERS: tuple[str, ...] = (
    PROVIDER_CLAUDE_CODE,
    PROVIDER_CODEX,
)

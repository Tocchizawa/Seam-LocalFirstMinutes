"""要約 (議事録AI) サブシステム。

Public API:
    SummarizerProvider  — provider 共通 Protocol
    SummaryError        — 正規化されたエラー型
    SummaryErrorCode    — エラーコード列挙
    ProjectContext      — プロンプトに注入するプロジェクトコンテキスト
    SummaryResult       — 生成結果
    ProviderHealth      — health_check の戻り
    get_provider        — provider name → factory
    auto_detect_recommended — 推奨provider検出
    SummaryRunner       — ジョブキュー
    get_runner          — singleton runner
    recover_drafts      — 起動時リカバリ
"""
from .base import (
    CLI_PROVIDERS,
    PROVIDER_CLAUDE_API,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_CODEX,
    PROVIDER_GEMINI,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    V1_PROVIDERS,
    ProjectContext,
    ProviderHealth,
    SummarizerProvider,
    SummaryError,
    SummaryErrorCode,
    SummaryResult,
)
from .registry import (
    CLOUD_PROVIDERS,
    auto_detect_recommended,
    get_provider,
    is_cloud_provider,
    is_known_provider,
)
from .runner import (
    JobStatus,
    SummaryRunner,
    get_runner,
    recover_drafts,
    set_broadcaster,
)

__all__ = [
    "PROVIDER_OLLAMA",
    "PROVIDER_CLAUDE_API",
    "PROVIDER_OPENAI",
    "PROVIDER_GEMINI",
    "PROVIDER_CLAUDE_CODE",
    "PROVIDER_CODEX",
    "V1_PROVIDERS",
    "CLOUD_PROVIDERS",
    "CLI_PROVIDERS",
    "ProjectContext",
    "ProviderHealth",
    "SummarizerProvider",
    "SummaryError",
    "SummaryErrorCode",
    "SummaryResult",
    "JobStatus",
    "SummaryRunner",
    "auto_detect_recommended",
    "get_provider",
    "get_runner",
    "is_cloud_provider",
    "is_known_provider",
    "recover_drafts",
    "set_broadcaster",
]

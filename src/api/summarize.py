"""要約 (summarize) 関連の API endpoints。

提供するエンドポイント:
  POST /api/minutes/{id}/summarize         — 要約ジョブ起動
  GET  /api/minutes/{id}/summarize/status  — ジョブ状態
  POST /api/minutes/{id}/summarize/cancel  — ジョブキャンセル

  PUT    /api/summarize/api-key            — APIキー保存
  DELETE /api/summarize/api-key/{provider} — APIキー削除
  GET    /api/summarize/api-keys           — keyの登録有無一覧 (中身は返さない)
  POST   /api/summarize/test               — provider接続テスト
  POST   /api/summarize/consent/{provider} — クラウド利用同意マーク
  GET    /api/summarize/recommended        — 推奨provider auto-detect
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.api.errors import bad_request, not_found
from src.config import config
from src.storage.db import db
from src.summarize.base import SummaryError
from src.summarize.registry import (
    CLOUD_PROVIDERS,
    auto_detect_recommended,
    get_provider,
    is_cloud_provider,
    is_known_provider,
)
from src.summarize.runner import get_runner

router = APIRouter(tags=["summarize"])


# ───────── job control: per-minutes ─────────

@router.post("/api/minutes/{minutes_id}/summarize", status_code=202)
async def trigger_summarize(
    minutes_id: str,
    provider: str | None = None,
) -> dict:
    """要約ジョブを enqueue する。

    Query:
        provider: 省略時は config の minutes_ai.provider を使う
    """
    if db.get_minutes(minutes_id) is None:
        raise not_found("MINUTES_NOT_FOUND", f"minutes '{minutes_id}' が見つかりません")
    if provider and not is_known_provider(provider):
        raise bad_request("UNKNOWN_PROVIDER", f"未知のproviderです: {provider}")
    # cloud provider なら consent チェック
    target_provider = provider or str(config.get("minutes_ai", "provider", default="ollama"))
    if is_cloud_provider(target_provider):
        consent_map = config.get("minutes_ai", "consent") or {}
        if not bool(consent_map.get(target_provider)):
            raise bad_request(
                "CONSENT_REQUIRED",
                f"{target_provider} の利用同意が未取得です。POST /api/summarize/consent/{target_provider} で同意してください。",
            )
    ai_cfg = config.get("minutes_ai") or {}
    try:
        provider_impl = get_provider(target_provider, ai_cfg)
        health = await provider_impl.health_check()
    except SummaryError as e:
        raise bad_request(e.code.value, e.message)
    except Exception as e:
        raise bad_request("PROVIDER_HEALTH_CHECK_FAILED", str(e))
    if not health.ok:
        raise bad_request(health.code, health.message)
    get_runner().enqueue(minutes_id, provider_name=provider)
    return {"job_id": minutes_id, "status": "queued"}


@router.get("/api/minutes/{minutes_id}/summarize/status")
async def summarize_status(minutes_id: str) -> dict:
    status = get_runner().get_status(minutes_id)
    if status is None:
        return {"state": "none"}
    return status.to_dict()


@router.post("/api/minutes/{minutes_id}/summarize/cancel")
async def summarize_cancel(minutes_id: str) -> dict:
    ok = await get_runner().cancel(minutes_id)
    return {"state": "cancelled" if ok else "none"}


@router.get("/api/summarize/active")
async def summarize_active() -> list[dict]:
    """進行中の要約ジョブ一覧 (project_id 付き)。

    サイドバーの各プロジェクトにローダー表示するため、最低限の情報のみ返す。
    """
    runner = get_runner()
    out: list[dict] = []
    for mid in runner.list_active():
        m = db.get_minutes(mid)
        if m is None:
            continue
        status = runner.get_status(mid)
        out.append({
            "minutes_id": mid,
            "project_id": m.get("project_id"),
            "state": status.state if status is not None else None,
        })
    return out


# ───────── provider config ─────────

class ApiKeyRequest(BaseModel):
    provider: str = Field(min_length=1)
    token: str = Field(min_length=4, max_length=400)


@router.put("/api/summarize/api-key")
async def put_api_key(body: ApiKeyRequest) -> dict:
    """APIキーを保存。provider別に keyring に格納する。"""
    if not is_cloud_provider(body.provider):
        raise bad_request(
            "INVALID_PROVIDER",
            f"{body.provider} はAPIキー登録対象外です (cloud: {', '.join(CLOUD_PROVIDERS)})",
        )
    token = body.token.strip()
    if not token:
        raise bad_request("EMPTY_TOKEN", "トークンが空です")
    try:
        if body.provider == "claude_api":
            from src.summarize.claude_api import set_api_key as _set
            _set(token)
        elif body.provider == "openai":
            from src.summarize.openai_api import set_api_key as _set
            _set(token)
        elif body.provider == "gemini":
            raise bad_request("NOT_IMPLEMENTED", "Gemini provider は v1 後続フェーズで対応")
        else:
            raise bad_request("UNKNOWN_PROVIDER", f"未知の provider: {body.provider}")
    except SummaryError as e:
        raise bad_request(e.code.value, e.message)
    return {"status": "saved", "provider": body.provider}


@router.delete("/api/summarize/api-key/{provider}")
async def delete_api_key_endpoint(provider: str) -> dict:
    if not is_cloud_provider(provider):
        raise bad_request(
            "INVALID_PROVIDER",
            f"{provider} はAPIキー登録対象外です",
        )
    if provider == "claude_api":
        from src.summarize.claude_api import delete_api_key as _del
        _del()
    elif provider == "openai":
        from src.summarize.openai_api import delete_api_key as _del
        _del()
    elif provider == "gemini":
        try:
            import keyring
            keyring.delete_password("seam-app", "gemini_api_key")
        except Exception:
            pass
    return {"status": "deleted", "provider": provider}


@router.get("/api/summarize/api-keys")
async def list_api_keys() -> dict:
    """各providerのAPIキー登録有無 (内容は返さない)。"""
    out: dict[str, bool] = {}
    for p in CLOUD_PROVIDERS:
        if p == "claude_api":
            from src.summarize.claude_api import has_api_key
            out[p] = has_api_key()
        elif p == "openai":
            from src.summarize.openai_api import has_api_key
            out[p] = has_api_key()
        else:
            try:
                import keyring
                out[p] = bool(keyring.get_password("seam-app", f"{p}_api_key"))
            except Exception:
                out[p] = False
    return {"providers": out}


# ───────── consent ─────────

@router.post("/api/summarize/consent/{provider}")
async def consent_provider(provider: str) -> dict:
    if not is_cloud_provider(provider):
        raise bad_request(
            "INVALID_PROVIDER",
            f"{provider} は同意不要のproviderです",
        )
    consent_map = config.get("minutes_ai", "consent") or {}
    consent_map = dict(consent_map) if isinstance(consent_map, dict) else {}
    consent_map[provider] = True
    config.update({"minutes_ai": {"consent": consent_map}})
    return {"status": "consented", "provider": provider}


# ───────── connection test ─────────

class TestProviderRequest(BaseModel):
    provider: str = Field(min_length=1)


@router.post("/api/summarize/test")
async def test_provider(body: TestProviderRequest) -> dict:
    if not is_known_provider(body.provider):
        raise bad_request("UNKNOWN_PROVIDER", f"未知のproviderです: {body.provider}")
    ai_cfg = config.get("minutes_ai") or {}
    try:
        provider = get_provider(body.provider, ai_cfg)
    except SummaryError as e:
        return {"ok": False, "code": e.code.value, "message": e.message}
    health = await provider.health_check()
    return {
        "ok": health.ok,
        "code": health.code,
        "message": health.message,
        "model": health.model,
    }


# ───────── auto-detect ─────────

@router.get("/api/summarize/recommended")
async def get_recommended() -> dict:
    ai_cfg = config.get("minutes_ai") or {}
    res = await auto_detect_recommended(ai_cfg)
    return res

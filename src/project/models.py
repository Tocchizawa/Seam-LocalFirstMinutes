from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Member(BaseModel):
    name: str
    role: str = ""


class CorrectionPair(BaseModel):
    """Whisper 誤転写 → 正式表記 のマッピング。

    要約完了後の LLM 補正で発見した wrong→correct ペアをここに蓄積し、
    将来の Whisper 出力には post-process 置換で適用する。
    """
    wrong: str       # Whisper が出力しがちな誤転写
    correct: str     # 正式表記


class ProjectCreate(BaseModel):
    name: str
    repo_path: str | None = None
    doc_dirs: list[str] = Field(default_factory=list)
    output_dir: str
    members: list[Member] = Field(default_factory=list)
    glossary: list[str] = Field(default_factory=list)
    corrections: list[CorrectionPair] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError("name is required")
        return name

    @field_validator("output_dir")
    @classmethod
    def _validate_output_dir(cls, value: str) -> str:
        return _normalize_abs_path(value, field_name="output_dir")

    @field_validator("repo_path")
    @classmethod
    def _validate_repo_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return _normalize_abs_path(text, field_name="repo_path")

    @field_validator("doc_dirs")
    @classmethod
    def _validate_doc_dirs(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = _normalize_abs_path(item, field_name="doc_dirs")
            if normalized not in seen:
                out.append(normalized)
                seen.add(normalized)
        return out


class ProjectUpdate(BaseModel):
    name: str | None = None
    repo_path: str | None = None
    doc_dirs: list[str] | None = None
    output_dir: str | None = None
    members: list[Member] | None = None
    glossary: list[str] | None = None
    corrections: list[CorrectionPair] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = str(value).strip()
        if not name:
            raise ValueError("name must not be empty")
        return name

    @field_validator("output_dir")
    @classmethod
    def _validate_output_dir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_abs_path(value, field_name="output_dir")

    @field_validator("repo_path")
    @classmethod
    def _validate_repo_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return _normalize_abs_path(text, field_name="repo_path")

    @field_validator("doc_dirs")
    @classmethod
    def _validate_doc_dirs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = _normalize_abs_path(item, field_name="doc_dirs")
            if normalized not in seen:
                out.append(normalized)
                seen.add(normalized)
        return out


class Project(BaseModel):
    id: str
    name: str
    repo_path: str | None = None
    doc_dirs: list[str] = Field(default_factory=list)
    output_dir: str
    members: list[Member] = Field(default_factory=list)
    glossary: list[str] = Field(default_factory=list)
    corrections: list[CorrectionPair] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


def _normalize_abs_path(value: str, *, field_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    if any(ch in raw for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{field_name} contains invalid characters")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute path")
    return str(path)

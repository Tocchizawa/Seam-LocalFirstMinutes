from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.config import APP_DIR
from src.project.models import Project, ProjectCreate, ProjectUpdate

logger = logging.getLogger(__name__)

PROJECTS_PATH = APP_DIR / "projects.yaml"
OUTPUT_DIR_SENTINEL = ".seam_output_dir"


class ProjectManager:
    def __init__(self) -> None:
        self._path = PROJECTS_PATH
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not self._path.exists():
            APP_DIR.mkdir(parents=True, exist_ok=True)
            self._save_raw({"schema_version": 1, "projects": []})

    def _load_raw(self) -> dict:
        with open(self._path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {"schema_version": 1, "projects": []}

    def _save_raw(self, data: dict) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def _mark_output_dir(self, output_dir: Path) -> None:
        try:
            (output_dir / OUTPUT_DIR_SENTINEL).touch(exist_ok=True)
        except Exception as e:
            logger.warning("Failed to mark output dir %s: %s", output_dir, e)

    def _ensure_output_dir(self, output_dir: str) -> Path:
        output = Path(output_dir).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        self._mark_output_dir(output)
        return output

    def _can_delete_output_dir(self, output_dir: Path) -> tuple[bool, str]:
        if output_dir.expanduser().is_symlink():
            return False, "symlink is not allowed"
        try:
            resolved = output_dir.expanduser().resolve()
        except Exception as e:
            return False, f"resolve failed: {e}"
        if not resolved.is_dir():
            return False, "not a directory"
        if len(resolved.parts) < 4:
            return False, "path is too shallow"
        protected_paths = {
            Path("/").resolve(),
            Path.home().resolve(),
            APP_DIR.resolve(),
            (APP_DIR / "sessions").resolve(),
            (APP_DIR / "logs").resolve(),
        }
        if resolved in protected_paths:
            return False, "protected path"
        if not (resolved / OUTPUT_DIR_SENTINEL).exists():
            return False, f"missing sentinel: {OUTPUT_DIR_SENTINEL}"
        return True, ""

    def list(self) -> list[Project]:
        raw = self._load_raw()
        return [Project(**p) for p in raw.get("projects", [])]

    def get(self, project_id: str) -> Project | None:
        for p in self.list():
            if p.id == project_id:
                return p
        return None

    def create(self, data: ProjectCreate) -> Project:
        raw = self._load_raw()
        now = datetime.now(timezone.utc).isoformat()
        project = Project(
            id=uuid.uuid4().hex[:12],
            name=data.name,
            repo_path=data.repo_path,
            doc_dirs=data.doc_dirs,
            output_dir=data.output_dir,
            members=data.members,
            glossary=data.glossary,
            created_at=now,
            updated_at=now,
        )
        # output_dir の自動作成
        self._ensure_output_dir(project.output_dir)

        raw.setdefault("projects", []).append(project.model_dump())
        self._save_raw(raw)
        logger.info("Created project: %s (%s)", project.name, project.id)
        return project

    def update(self, project_id: str, data: ProjectUpdate) -> Project | None:
        raw = self._load_raw()
        projects = raw.get("projects", [])
        for i, p in enumerate(projects):
            if p["id"] == project_id:
                update_dict = data.model_dump(exclude_none=True)
                p.update(update_dict)
                p["updated_at"] = datetime.now(timezone.utc).isoformat()
                # output_dir が変更されたら自動作成
                if "output_dir" in update_dict:
                    self._ensure_output_dir(p["output_dir"])
                projects[i] = p
                raw["projects"] = projects
                self._save_raw(raw)
                logger.info("Updated project: %s", project_id)
                return Project(**p)
        return None

    def delete(self, project_id: str, delete_output: bool = False) -> bool:
        raw = self._load_raw()
        projects = raw.get("projects", [])
        for i, p in enumerate(projects):
            if p["id"] == project_id:
                if delete_output and p.get("output_dir"):
                    import shutil
                    output = Path(p["output_dir"]).expanduser()
                    if output.exists():
                        can_delete, reason = self._can_delete_output_dir(output)
                        if can_delete:
                            shutil.rmtree(output)
                            logger.info("Deleted output dir: %s", output)
                        else:
                            logger.warning(
                                "Skip deleting output dir '%s': %s",
                                output,
                                reason,
                            )
                projects.pop(i)
                raw["projects"] = projects
                self._save_raw(raw)
                logger.info("Deleted project: %s", project_id)
                return True
        return False


project_manager = ProjectManager()

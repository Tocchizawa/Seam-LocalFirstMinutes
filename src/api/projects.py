from __future__ import annotations

from fastapi import APIRouter, Query

from src.api.errors import not_found
from src.project.manager import project_manager
from src.project.models import Project, ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[Project])
async def list_projects() -> list[Project]:
    return project_manager.list()


@router.post("", response_model=Project, status_code=201)
async def create_project(data: ProjectCreate) -> Project:
    return project_manager.create(data)


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str) -> Project:
    p = project_manager.get(project_id)
    if p is None:
        raise not_found("PROJECT_NOT_FOUND", f"プロジェクト '{project_id}' が見つかりません")
    return p


@router.put("/{project_id}", response_model=Project)
async def update_project(project_id: str, data: ProjectUpdate) -> Project:
    p = project_manager.update(project_id, data)
    if p is None:
        raise not_found("PROJECT_NOT_FOUND", f"プロジェクト '{project_id}' が見つかりません")
    return p


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    delete_output: bool = Query(False, description="output_dir も削除するか"),
) -> dict:
    ok = project_manager.delete(project_id, delete_output=delete_output)
    if not ok:
        raise not_found("PROJECT_NOT_FOUND", f"プロジェクト '{project_id}' が見つかりません")
    return {"status": "deleted"}

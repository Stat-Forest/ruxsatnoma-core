"""Infrastructure file router (design/01's file-router exception; lives at the app
level, not in core, so core keeps importing zero modules — auth deps and audit are
module code)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files
from app.core.deps import get_db
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import get_current_user
from app.modules.auth.models import User

router = APIRouter(prefix="/files", tags=["files"])


class FileOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


def _sanitize_filename(filename: str) -> str:
    """Strip characters that would break the Content-Disposition header (quotes end
    the filename="..." value early, newlines inject headers) — applied once here so
    the stored value is already safe wherever it is later reflected back."""
    return filename.replace('"', "").replace("\n", " ")


@router.post("", status_code=201)
async def upload_file(
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> FileOut:
    data = await file.read()
    filename = _sanitize_filename(file.filename or "file")
    saved = await files.save_upload(
        db,
        data=data,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        actor=user,
    )
    await audit.log(
        db,
        action="file.upload",
        user_id=user.id,
        object_type="media_file",
        object_id=saved.id,
        new_value={
            "filename": saved.filename,
            "content_type": saved.content_type,
            "size": saved.size_bytes,
        },
    )
    return FileOut.model_validate(saved, from_attributes=True)


@router.get("/{file_id}")
async def download_file(
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Response:
    role = await auth_repo.role_code(db, user)
    file, data = await files.get_readable(db, file_id, user, role)
    disposition = "inline" if file.content_type in files.INLINE_TYPES else "attachment"
    return Response(
        content=data,
        media_type=file.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{file.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )

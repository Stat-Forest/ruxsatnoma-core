"""Infrastructure file router (design/01's file-router exception; lives at the app
level, not in core, so core keeps importing zero modules — auth deps and audit are
module code)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import files, settings_store
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


@router.post("", status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> FileOut:
    cap_bytes = await settings_store.get_int(db, "max_upload_mb") * 1024 * 1024
    data = await files.read_capped(file, cap_bytes, files.declared_length(request.headers))
    filename = files.sanitize_filename(file.filename or "file")
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
            "Content-Disposition": files.content_disposition(disposition, file.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )

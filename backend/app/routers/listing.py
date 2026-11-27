from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.dependencies import get_current_admin, get_current_user
from app.openapi import COMMON_RESPONSES
from app.schemas.user import UserRead
from app.services import audit_service
from app.services.template_service import ImageUploadConflict, TemplateError, TemplateService

router = APIRouter(prefix="/api/list", tags=["list"], responses=COMMON_RESPONSES)


@router.get("/icons/{filename}", response_class=FileResponse)
def get_node_icon(filename: str, current_user: UserRead = Depends(get_current_user)):
    asset = TemplateService().icon_asset(filename)
    if asset is None:
        raise HTTPException(status_code=404, detail="Node icon does not exist.")
    path, media_type = asset
    return FileResponse(
        path,
        media_type=media_type,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private"},
    )


@router.get("/templates/{template_type}")
async def list_templates(
    template_type: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = TemplateService().list_templates(template_type)
    except TemplateError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Templates listed successfully.",
        "data": data,
    }


@router.get("/images/{template_type}/{template_key}")
async def list_images(
    template_type: str,
    template_key: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = TemplateService().list_images(template_type, template_key)
    except TemplateError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Images listed successfully.",
        "data": data,
    }


@router.post("/images/{template_type}/{template_key}")
async def upload_image(
    template_type: str,
    template_key: str,
    image: UploadFile = File(...),
    image_name: str | None = Form(default=None),
    current_user: UserRead = Depends(get_current_admin),
):
    try:
        data, size = await TemplateService().upload_image(
            template_type=template_type,
            template_key=template_key,
            filename=image.filename or "",
            source=image.file,
            image_name=image_name,
        )
    except ImageUploadConflict as exc:
        return {"code": 409, "status": "fail", "message": str(exc)}
    except TemplateError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }

    # #300: an admin put a boot image on the host.
    uploaded = data.get("image") if isinstance(data, dict) else None
    await audit_service.record(
        "image.upload",
        actor=current_user.username,
        target_type="image",
        target_id=str(uploaded or image_name or image.filename or ""),
        target_name=str(uploaded or image_name or image.filename or ""),
        detail={
            "image_type": template_type,
            "template": template_key,
            "bytes": size,
        },
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Image uploaded successfully.",
        "data": data,
    }

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.dependencies import get_current_admin, get_current_user
from app.schemas.user import UserRead
from app.services.template_service import TemplateError, TemplateService

router = APIRouter(prefix="/api/list", tags=["list"])


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
        data = await TemplateService().upload_image(
            template_type=template_type,
            template_key=template_key,
            filename=image.filename or "",
            content=await image.read(),
            image_name=image_name,
        )
    except TemplateError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Image uploaded successfully.",
        "data": data,
    }

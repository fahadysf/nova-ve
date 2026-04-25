from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
from typing import Any

import yaml

from app.config import get_settings


SUPPORTED_TEMPLATE_TYPES = {"qemu", "docker", "iol", "dynamips"}


class TemplateError(Exception):
    pass


@dataclass
class TemplateDefinition:
    key: str
    type: str
    name: str
    description: str
    icon: str
    cpu: int
    ram: int
    ethernet: int
    console: str
    cpulimit: int
    raw: dict[str, Any]

    def as_response(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "template": self.key,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "cpu": self.cpu,
            "ram": self.ram,
            "ethernet": self.ethernet,
            "console": self.console,
            "cpulimit": self.cpulimit,
        }


class TemplateService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.templates_dir = self.settings.TEMPLATES_DIR
        self.images_dir = self.settings.IMAGES_DIR

    def list_templates(self, template_type: str) -> dict[str, dict[str, Any]]:
        template_type = self._normalize_type(template_type)
        templates: dict[str, dict[str, Any]] = {}
        for template in self._load_templates():
            if template.type == template_type:
                templates[template.key] = template.as_response()
        return dict(sorted(templates.items()))

    def get_template(self, template_type: str, key: str) -> TemplateDefinition:
        template_type = self._normalize_type(template_type)
        for template in self._load_templates():
            if template.type == template_type and template.key == key:
                return template
        raise TemplateError(f"Template {key} does not exist for type {template_type}.")

    def list_images(self, template_type: str, template_key: str) -> dict[str, dict[str, Any]]:
        self.get_template(template_type, template_key)
        images: dict[str, dict[str, Any]] = {}

        if template_type == "docker":
            images.update(self._docker_image_catalog())

        image_root = self.images_dir / template_type
        if image_root.exists():
            for child in sorted(image_root.iterdir()):
                image_info = self._image_info(child)
                if image_info:
                    images[image_info["image"]] = image_info

        return images

    def validate_node_request(self, template_type: str, template_key: str, image_name: str) -> TemplateDefinition:
        template = self.get_template(template_type, template_key)
        images = self.list_images(template_type, template_key)
        if image_name not in images:
            raise TemplateError(
                f"Image {image_name} is not available for template {template_key} ({template_type})."
            )
        return template

    def list_icon_options(self) -> list[str]:
        icons = {
            "Router.png",
            "Server.png",
        }
        for template in self._load_templates():
            icon = str(template.icon).strip()
            if icon:
                icons.add(icon)
        return sorted(icons)

    def build_node_catalog(self) -> dict[str, Any]:
        templates: list[dict[str, Any]] = []
        icon_options = self.list_icon_options()
        for template in self._load_templates():
            images = list(self.list_images(template.type, template.key).values())
            default_image = images[0]["image"] if images else ""
            templates.append(
                {
                    "key": template.key,
                    "type": template.type,
                    "name": template.name,
                    "description": template.description,
                    "defaults": {
                        "type": template.type,
                        "template": template.key,
                        "image": default_image,
                        "icon": template.icon,
                        "cpu": template.cpu,
                        "ram": template.ram,
                        "ethernet": template.ethernet,
                        "console": template.console,
                        "delay": 0,
                        "cpulimit": template.cpulimit,
                    },
                    "images": images,
                    "icon_options": icon_options,
                }
            )
        return {
            "templates": templates,
            "icon_options": icon_options,
        }

    async def upload_image(
        self,
        template_type: str,
        template_key: str,
        filename: str,
        content: bytes,
        image_name: str | None = None,
    ) -> dict[str, Any]:
        self.get_template(template_type, template_key)
        safe_filename = Path(filename).name
        if not safe_filename:
            raise TemplateError("Uploaded image filename is empty.")

        target_image_name = (image_name or Path(safe_filename).stem).strip()
        if not target_image_name:
            raise TemplateError("Image name is invalid.")

        target_dir = self.images_dir / template_type / target_image_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        target_path.write_bytes(content)
        return self._image_info(target_dir) or {
            "image": target_image_name,
            "filename": safe_filename,
            "path": str(target_path),
        }

    def _load_templates(self) -> list[TemplateDefinition]:
        if not self.templates_dir.exists():
            return []

        templates: list[TemplateDefinition] = []
        for template_path in sorted(self.templates_dir.rglob("*.yml")):
            payload = yaml.safe_load(template_path.read_text()) or {}
            template_type = str(payload.get("type") or template_path.parent.name).strip().lower()
            if template_type not in SUPPORTED_TEMPLATE_TYPES:
                continue

            key = template_path.stem
            templates.append(
                TemplateDefinition(
                    key=key,
                    type=template_type,
                    name=str(payload.get("name") or key),
                    description=str(payload.get("description") or ""),
                    icon=str(payload.get("icon") or self._default_icon(template_type)),
                    cpu=int(payload.get("cpu", 1)),
                    ram=int(payload.get("ram", 1024)),
                    ethernet=int(payload.get("ethernet", 1)),
                    console=str(payload.get("console") or self._default_console(template_type)),
                    cpulimit=int(payload.get("cpulimit", 1)),
                    raw=payload,
                )
            )
        return templates

    def _normalize_type(self, template_type: str) -> str:
        normalized = template_type.strip().lower()
        if normalized not in SUPPORTED_TEMPLATE_TYPES:
            raise TemplateError(f"Unsupported template type: {template_type}")
        return normalized

    def _image_info(self, path: Path) -> dict[str, Any] | None:
        if path.is_dir():
            files = sorted(child.name for child in path.iterdir() if child.is_file())
            if not files:
                return None
            return {
                "image": path.name,
                "files": files,
                "path": str(path),
            }

        if path.is_file():
            return {
                "image": path.stem,
                "files": [path.name],
                "path": str(path),
            }
        return None

    def _docker_image_catalog(self) -> dict[str, dict[str, Any]]:
        docker_binary = shutil.which("docker")
        if not docker_binary:
            return {}

        env = os.environ.copy()
        if getattr(self.settings, "DOCKER_HOST", ""):
            env["DOCKER_HOST"] = self.settings.DOCKER_HOST

        command = [
            docker_binary,
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
        if result.returncode != 0:
            return {}

        images: dict[str, dict[str, Any]] = {}
        for line in result.stdout.splitlines():
            image_name = line.strip()
            if not image_name or image_name.endswith(":<none>") or image_name.startswith("<none>:"):
                continue
            images[image_name] = {
                "image": image_name,
                "files": [],
                "path": image_name,
                "source": "docker",
            }
        return images

    @staticmethod
    def _default_icon(template_type: str) -> str:
        if template_type == "docker":
            return "Server.png"
        return "Router.png"

    @staticmethod
    def _default_console(template_type: str) -> str:
        if template_type == "docker":
            return "rdp"
        return "telnet"

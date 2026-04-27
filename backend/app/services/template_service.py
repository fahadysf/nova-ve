from dataclasses import dataclass, field
from pathlib import Path
import os
import shutil
import subprocess
from typing import Any

import yaml

from app.config import get_settings


SUPPORTED_TEMPLATE_TYPES = {"qemu", "docker", "iol", "dynamips"}


ICON_TYPE_TO_FILENAME = {
    "router": "Router.png",
    "server": "Server.png",
    "switch": "Switch.png",
    "firewall": "Firewall.png",
    "host": "Server.png",
}


def _icon_filename_for(icon_type: str) -> str:
    return ICON_TYPE_TO_FILENAME.get(icon_type.strip().lower(), "Router.png")


QEMU_NIC_OPTIONS = [
    {"value": "virtio-net-pci", "label": "virtio-net-pci"},
    {"value": "e1000", "label": "e1000"},
    {"value": "e1000e", "label": "e1000e"},
    {"value": "rtl8139", "label": "rtl8139"},
    {"value": "vmxnet3", "label": "vmxnet3"},
    {"value": "pcnet", "label": "pcnet"},
]

QEMU_ARCH_OPTIONS = [
    {"value": "x86_64", "label": "x86_64"},
    {"value": "aarch64", "label": "aarch64"},
    {"value": "i386", "label": "i386"},
]

DOCKER_RESTART_OPTIONS = [
    {"value": "no", "label": "no"},
    {"value": "on-failure", "label": "on-failure"},
    {"value": "unless-stopped", "label": "unless-stopped"},
    {"value": "always", "label": "always"},
]

IOL_CONFIG_OPTIONS = [
    {"value": "Unconfigured", "label": "Unconfigured"},
    {"value": "Saved", "label": "Saved"},
    {"value": "Exported", "label": "Exported"},
]

DYNAMIPS_NPE_OPTIONS = [
    {"value": "npe-100", "label": "npe-100"},
    {"value": "npe-150", "label": "npe-150"},
    {"value": "npe-175", "label": "npe-175"},
    {"value": "npe-200", "label": "npe-200"},
    {"value": "npe-225", "label": "npe-225"},
    {"value": "npe-300", "label": "npe-300"},
    {"value": "npe-400", "label": "npe-400"},
    {"value": "npe-g1", "label": "npe-g1"},
    {"value": "npe-g2", "label": "npe-g2"},
]

DYNAMIPS_MIDPLANE_OPTIONS = [
    {"value": "std", "label": "std"},
    {"value": "vxr", "label": "vxr"},
]

DYNAMIPS_C7200_SLOT_OPTIONS = [
    {"value": "", "label": "(empty)"},
    {"value": "C7200-IO-FE", "label": "C7200-IO-FE"},
    {"value": "C7200-IO-2FE", "label": "C7200-IO-2FE"},
    {"value": "C7200-IO-GE-E", "label": "C7200-IO-GE-E"},
    {"value": "PA-FE-TX", "label": "PA-FE-TX"},
    {"value": "PA-2FE-TX", "label": "PA-2FE-TX"},
    {"value": "PA-GE", "label": "PA-GE"},
    {"value": "PA-4E", "label": "PA-4E"},
    {"value": "PA-8E", "label": "PA-8E"},
    {"value": "PA-4T+", "label": "PA-4T+"},
    {"value": "PA-8T", "label": "PA-8T"},
    {"value": "PA-A1", "label": "PA-A1"},
    {"value": "PA-POS-OC3", "label": "PA-POS-OC3"},
]


def _qemu_extras_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "architecture",
            "label": "Architecture",
            "type": "select",
            "options": QEMU_ARCH_OPTIONS,
            "default": "x86_64",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_nic",
            "label": "NIC model",
            "type": "select",
            "options": QEMU_NIC_OPTIONS,
            "default": "e1000",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_version",
            "label": "QEMU version",
            "type": "text",
            "default": "",
            "placeholder": "(auto)",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_options",
            "label": "Extra qemu args",
            "type": "textarea",
            "default": "",
            "placeholder": "-nographic -enable-kvm",
            "description": "Appended to the qemu command line.",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "uuid",
            "label": "UUID",
            "type": "text",
            "default": "",
            "placeholder": "(auto-generated)",
            "stoppedOnly": True,
        },
        {
            "key": "firstmac",
            "label": "First MAC",
            "type": "text",
            "default": "",
            "placeholder": "(auto-generated)",
            "stoppedOnly": True,
        },
        {
            "key": "cpulimit",
            "label": "CPU limit",
            "type": "number",
            "default": 1,
            "stoppedOnly": True,
        },
    ]


def _docker_extras_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "cpulimit",
            "label": "CPU limit",
            "type": "number",
            "default": 1,
            "stoppedOnly": True,
        },
        {
            "key": "restart_policy",
            "label": "Restart policy",
            "type": "select",
            "options": DOCKER_RESTART_OPTIONS,
            "default": "no",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "environment",
            "label": "Environment variables",
            "type": "env",
            "default": [],
            "description": "Passed as -e KEY=value to docker run.",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "extra_args",
            "label": "Extra docker args",
            "type": "textarea",
            "default": "",
            "placeholder": "--privileged --cap-add=NET_ADMIN",
            "stoppedOnly": True,
            "runtime": True,
        },
    ]


def _iol_extras_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "serial",
            "label": "Serial groups",
            "type": "number",
            "default": 0,
            "description": "Each group adds 4 serial interfaces.",
            "stoppedOnly": True,
        },
        {
            "key": "nvram",
            "label": "NVRAM (KB)",
            "type": "number",
            "default": 1024,
            "stoppedOnly": True,
        },
        {
            "key": "config",
            "label": "Config",
            "type": "select",
            "options": IOL_CONFIG_OPTIONS,
            "default": "Unconfigured",
            "stoppedOnly": True,
        },
        {
            "key": "idle_pc",
            "label": "Idle PC",
            "type": "text",
            "default": "",
            "placeholder": "0x...",
            "stoppedOnly": True,
        },
    ]


def _dynamips_c7200_extras_schema() -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    for index in range(7):
        schema.append(
            {
                "key": f"slot{index}",
                "label": f"Slot {index}",
                "type": "select",
                "options": DYNAMIPS_C7200_SLOT_OPTIONS,
                "default": "C7200-IO-FE" if index == 0 else "",
                "stoppedOnly": True,
                "runtime": True,
            }
        )
    schema += [
        {
            "key": "nvram",
            "label": "NVRAM (KB)",
            "type": "number",
            "default": 128,
            "stoppedOnly": True,
        },
        {
            "key": "npe",
            "label": "NPE",
            "type": "select",
            "options": DYNAMIPS_NPE_OPTIONS,
            "default": "npe-400",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "midplane",
            "label": "Midplane",
            "type": "select",
            "options": DYNAMIPS_MIDPLANE_OPTIONS,
            "default": "vxr",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "idlepc",
            "label": "Idle PC",
            "type": "text",
            "default": "",
            "placeholder": "0x...",
            "stoppedOnly": True,
            "runtime": True,
        },
    ]
    return schema


def _extras_schema_for(template_type: str) -> list[dict[str, Any]]:
    if template_type == "qemu":
        return _qemu_extras_schema()
    if template_type == "docker":
        return _docker_extras_schema()
    if template_type == "iol":
        return _iol_extras_schema()
    if template_type == "dynamips":
        return _dynamips_c7200_extras_schema()
    return []


class TemplateError(Exception):
    pass


@dataclass
class TemplateDefinition:
    key: str
    type: str
    name: str
    description: str
    icon_type: str
    cpu: int
    ram: int
    ethernet: int
    console_type: str
    cpulimit: int
    extras: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_response(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "template": self.key,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "icon_type": self.icon_type,
            "icon": _icon_filename_for(self.icon_type),
            "cpu": self.cpu,
            "ram": self.ram,
            "ethernet": self.ethernet,
            "console_type": self.console_type,
            "cpulimit": self.cpulimit,
            "extras": dict(self.extras),
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
        template = self.get_template(template_type, template_key)
        images: dict[str, dict[str, Any]] = {}

        if template_type == "docker":
            images.update(self._docker_image_catalog())

        image_root = self.images_dir / template_type
        if image_root.exists():
            apply_name_filter = template_type != "docker"
            template_match = template.name.strip().lower()
            for child in sorted(image_root.iterdir()):
                image_info = self._image_info(child)
                if not image_info:
                    continue
                if apply_name_filter and template_match:
                    folder_match = str(image_info["image"]).strip().lower()
                    if (
                        folder_match
                        and template_match not in folder_match
                        and folder_match not in template_match
                    ):
                        continue
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
            icon = _icon_filename_for(template.icon_type)
            if icon:
                icons.add(icon)
        return sorted(icons)

    def build_node_catalog(self) -> dict[str, Any]:
        templates: list[dict[str, Any]] = []
        icon_options = self.list_icon_options()
        for template in self._load_templates():
            images = list(self.list_images(template.type, template.key).values())
            default_image = images[0]["image"] if images else ""
            extras_schema = _extras_schema_for(template.type)
            defaults_extras = self._compose_extras(extras_schema, template.extras)
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
                        "icon_type": template.icon_type,
                        "icon": _icon_filename_for(template.icon_type),
                        "cpu": template.cpu,
                        "ram": template.ram,
                        "ethernet": template.ethernet,
                        "console_type": template.console_type,
                        "delay": 0,
                        "cpulimit": template.cpulimit,
                        "extras": defaults_extras,
                    },
                    "images": images,
                    "icon_options": icon_options,
                    "extras_schema": extras_schema,
                }
            )
        return {
            "templates": templates,
            "icon_options": icon_options,
        }

    def template_extras(self, template_type: str, template_key: str) -> dict[str, Any]:
        """Return the merged default extras (schema defaults + YAML overrides) for a template."""
        template = self.get_template(template_type, template_key)
        return self._compose_extras(_extras_schema_for(template_type), template.extras)

    @staticmethod
    def _compose_extras(schema: list[dict[str, Any]], yaml_overrides: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field in schema:
            result[field["key"]] = field.get("default")
        for key, value in (yaml_overrides or {}).items():
            result[key] = value
        return result

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
            yaml_extras = payload.get("extras") or {}
            if not isinstance(yaml_extras, dict):
                yaml_extras = {}
            yaml_extras = dict(yaml_extras)
            for schema_field in _extras_schema_for(template_type):
                schema_key = schema_field["key"]
                if schema_key in payload and schema_key not in yaml_extras:
                    yaml_extras[schema_key] = payload[schema_key]
            templates.append(
                TemplateDefinition(
                    key=key,
                    type=template_type,
                    name=str(payload.get("name") or key),
                    description=str(payload.get("description") or ""),
                    icon_type=str(payload.get("icon_type") or self._default_icon_type(template_type)),
                    cpu=int(payload.get("cpu", 1)),
                    ram=int(payload.get("ram", 1024)),
                    ethernet=int(payload.get("ethernet", 1)),
                    console_type=str(payload.get("console_type") or self._default_console_type(template_type)),
                    cpulimit=int(payload.get("cpulimit", 1)),
                    extras=yaml_extras,
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
    def _default_icon_type(template_type: str) -> str:
        if template_type == "docker":
            return "server"
        return "router"

    @staticmethod
    def _default_console_type(template_type: str) -> str:
        if template_type == "docker":
            return "rdp"
        return "telnet"

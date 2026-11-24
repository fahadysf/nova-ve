"""Resolve existing templates using the node picker's image matcher."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.services.template_service import (
    SUPPORTED_TEMPLATE_TYPES,
    TemplateError,
    _matches_template_image_name,
    _validate_image_aliases,
)


DEFAULT_BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
_logger = logging.getLogger("nova_ve.import_eveng")


@dataclass(frozen=True)
class TemplateCandidate:
    key: str
    type: str
    name: str
    image_aliases: list[str]
    user: bool
    declared_image: str


def _read_candidates(
    paths: list[Path], *, user: bool, inherited: dict[tuple[str, str], TemplateCandidate]
) -> dict[tuple[str, str], TemplateCandidate]:
    candidates: dict[tuple[str, str], TemplateCandidate] = {}
    for path in paths:
        try:
            payload = yaml.safe_load(path.read_text()) or {}
            if not isinstance(payload, dict) or payload.get("kind") == "paired":
                continue
            kind = str(payload.get("type") or path.parent.name).strip().lower()
            if kind not in SUPPORTED_TEMPLATE_TYPES:
                continue
            key = (kind, path.stem)
            builtin = inherited.get(key)
            if "image_aliases" in payload:
                aliases = _validate_image_aliases(payload["image_aliases"], str(path))
            else:
                aliases = builtin.image_aliases if builtin else []
        except (OSError, UnicodeError, yaml.YAMLError, TemplateError) as exc:
            _logger.warning("Cannot read image aliases from %s: %s", path, exc)
            continue
        candidates[key] = TemplateCandidate(
            key=path.stem,
            type=kind,
            name=str(payload.get("name") or path.stem),
            image_aliases=aliases,
            user=user,
            declared_image=str(payload.get("image") or "").strip() if kind == "docker" else "",
        )
    return candidates


def load_template_candidates(
    builtin_dir: Path, user_dir: Path
) -> list[TemplateCandidate]:
    builtins = _read_candidates(sorted(builtin_dir.rglob("*.yml")), user=False, inherited={})
    # Runtime precedence is builtins, then user YAML, then user JSON. A
    # user override without image_aliases inherits the builtin's aliases.
    users = _read_candidates(
        [*sorted(user_dir.rglob("*.yml")), *sorted(user_dir.rglob("*.json"))],
        user=True,
        inherited=builtins,
    )
    return sorted((builtins | users).values(), key=lambda template: template.key)


def _rank(candidate: TemplateCandidate, image: str) -> tuple[bool, int, bool, bool]:
    key = candidate.key.lower()
    name = candidate.name.strip().lower()
    matching_aliases = [
        alias
        for alias in candidate.image_aliases
        if _matches_template_image_name(SimpleNamespace(name="", image_aliases=[alias]), image)
    ]
    specificity = max(
        (len(alias.replace("*", "")) for alias in matching_aliases if "*" in alias or alias in image),
        default=0,
    )
    if name in image:
        specificity = max(specificity, len(name))
    # Prefer an exact named template, then the most specific matcher.
    # For equal families, an operator template wins before the key tiebreaker.
    return (
        bool(candidate.declared_image) or image in (key, name, *matching_aliases),
        specificity,
        candidate.user,
        image.startswith(f"{key}-"),
    )


def select_existing_template(
    candidates: list[TemplateCandidate], kind: str, image: str, *, docker_tag: str = ""
) -> TemplateCandidate | None:
    normalized_image = image.strip().lower()
    best = None
    best_rank = (False, 0, False, False)
    for template in candidates:
        if template.type != kind:
            continue
        matches = (
            template.declared_image == docker_tag
            if template.declared_image
            else _matches_template_image_name(template, normalized_image)
        )
        if not matches:
            continue
        rank = _rank(template, normalized_image)
        # The picker also accepts a substring of a display name. A fragment
        # such as "server" alone is insufficient to select a VM to reuse.
        if (rank[0] or rank[1] > 0) and rank > best_rank:
            best, best_rank = template, rank
    return best

# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the generated OpenAPI / Swagger spec.

FastAPI guarantees structurally valid OpenAPI 3 output by construction;
these tests defend against accidental regressions in the *content* —
missing tag descriptions, schemas leaking private names, doc URLs
moving back out from under the Caddy /api/* proxy, etc.
"""

from __future__ import annotations

import pytest

from app.main import app


@pytest.fixture(scope="module")
def spec() -> dict:
    return app.openapi()


def test_app_metadata_populated(spec: dict) -> None:
    info = spec["info"]
    assert info["title"] == "nova-ve API"
    assert info["version"]
    assert info.get("description"), "info.description must be set"
    assert info.get("contact"), "info.contact must be set"
    assert info.get("license"), "info.license must be set"


def test_openapi_tags_have_descriptions(spec: dict) -> None:
    tags = spec.get("tags") or []
    assert tags, "openapi_tags must be configured on the FastAPI app"
    for entry in tags:
        assert entry.get("name"), f"tag entry missing name: {entry!r}"
        assert entry.get("description"), f"tag {entry['name']!r} missing description"

    # Every operation tag should resolve to a named entry in the tags
    # metadata so Swagger UI can render the description.
    declared = {entry["name"] for entry in tags}
    used: set[str] = set()
    for methods in spec["paths"].values():
        for verb, op in methods.items():
            if verb in {"parameters", "summary", "description"}:
                continue
            for t in op.get("tags") or []:
                used.add(t)
    missing = sorted(used - declared)
    assert not missing, f"tags used by operations but not in OPENAPI_TAGS: {missing}"


def test_docs_urls_under_api_prefix() -> None:
    # The Caddy front-end has a single reverse_proxy rule for /api/*,
    # so the doc URLs must stay under that prefix to be publicly reachable.
    assert app.docs_url == "/api/docs"
    assert app.redoc_url == "/api/redoc"
    assert app.openapi_url == "/api/openapi.json"


def test_no_underscore_schemas_leak(spec: dict) -> None:
    schemas = (spec.get("components") or {}).get("schemas") or {}
    leaked = sorted(name for name in schemas if name.startswith("_"))
    assert not leaked, f"private (underscore-prefixed) schemas leaked into public spec: {leaked}"


def test_common_error_responses_documented(spec: dict) -> None:
    # Sample endpoint that should pick up the COMMON_RESPONSES spread:
    # /api/auth/me requires authentication, so 401/403 must be documented.
    op = spec["paths"]["/api/auth/me"]["get"]
    codes = set(op.get("responses", {}).keys())
    for required in ("200", "401", "403", "422", "503"):
        assert required in codes, (
            f"/api/auth/me GET missing documented response {required}; got {sorted(codes)}"
        )


def test_every_operation_has_summary_or_description(spec: dict) -> None:
    untyped = []
    for path, methods in spec["paths"].items():
        for verb, op in methods.items():
            if verb in {"parameters", "summary", "description"}:
                continue
            if not (op.get("summary") or op.get("description")):
                untyped.append(f"{verb.upper()} {path}")
    assert not untyped, f"endpoints lacking both summary and description: {untyped}"

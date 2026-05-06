"""Tests for the VendorAdapter ABC + registry + REQUIRED_FIELDS contract (#186)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from scripts.import_eveng import adapters as adapter_registry
from scripts.import_eveng.adapters import (
    GenericLinuxAdapter,
    NeedsManualReview,
    VendorAdapter,
    iter_adapters,
    register,
    reset_registry_for_tests,
    select_adapter,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "required_fields"


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot/restore the registry around each test so registrations don't leak.

    Resets to a minimal baseline (generic_linux only) for framework-level tests
    that exercise sort order / tiebreaker / dispatch invariants without the
    noise of every shipped vendor adapter. Vendor-specific tests (e.g.
    test_adapters_cisco) call ``reset_registry_for_tests()`` themselves to get
    the full production registry.
    """
    saved = list(adapter_registry.ADAPTERS)
    adapter_registry.ADAPTERS.clear()
    register(GenericLinuxAdapter())
    yield
    adapter_registry.ADAPTERS.clear()
    adapter_registry.ADAPTERS.extend(saved)


# ---- ABC enforcement ---------------------------------------------------


def test_abc_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        VendorAdapter()  # type: ignore[abstract]


def test_subclass_must_implement_match_and_convert() -> None:
    class _Incomplete(VendorAdapter):
        name: ClassVar[str] = "incomplete"
        priority: ClassVar[int] = 1

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


# ---- REQUIRED_FIELDS presence-only contract ----------------------------


class _StubAdapter(VendorAdapter):
    """Test stub: claims any dict, returns it back."""

    name: ClassVar[str] = "stub"
    priority: ClassVar[int] = 99
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image", "ram", "console"}

    def match(self, raw: dict[str, Any]) -> bool:
        return True

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        return dict(raw)


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text())


def test_all_present_passes_validate() -> None:
    """all_present.json: every required field set; validate() does not raise."""
    raw = _load_fixture("all_present.json")
    # Stub requires {image, ram, console}; all_present has them all.
    _StubAdapter().validate(raw)  # no raise


def test_required_missing_raises_needs_manual_review() -> None:
    """required_missing.json: lacks 'image' AND 'console'; raises NeedsManualReview."""
    raw = _load_fixture("required_missing.json")
    with pytest.raises(NeedsManualReview) as exc_info:
        _StubAdapter().validate(raw)
    msg = str(exc_info.value)
    # The exception names which fields were missing so the manifest entry can record them.
    assert "image" in msg or "console" in msg


def test_optional_missing_warns_and_continues() -> None:
    """optional_missing.json: only 'image' is present; required-only adapter still passes."""
    raw = _load_fixture("optional_missing.json")
    # generic_linux REQUIRED_FIELDS = {"image"} only; optional fields are absent.
    GenericLinuxAdapter().validate(raw)  # no raise


def test_required_fields_is_presence_only_not_semantic() -> None:
    """A field with a semantically-invalid value (e.g. cpu=0) still passes presence check.

    Semantic validation belongs in convert()/validate() override, NOT in REQUIRED_FIELDS.
    """
    raw = {"image": "alpine", "ram": 0, "console": ""}  # ram=0 + console="" are semantically wrong
    _StubAdapter().validate(raw)  # presence check passes despite semantic nonsense


# ---- registry order ----------------------------------------------------


def test_registry_default_state_has_generic_linux_last() -> None:
    """After reset_registry_for_tests, generic_linux must be the LAST entry.

    The exact set of preceding adapters depends on which vendor PRs have landed
    (Cisco from #187, Juniper from #188, etc.) — what's invariant is that
    generic_linux remains the structural fallback.
    """
    adapters = iter_adapters()
    assert len(adapters) >= 1
    assert isinstance(adapters[-1], GenericLinuxAdapter)


def test_registry_with_only_generic_linux_when_cleared() -> None:
    """Manually clearing the registry and registering only generic_linux yields a 1-element registry."""
    adapter_registry.ADAPTERS.clear()
    register(GenericLinuxAdapter())
    adapters = iter_adapters()
    assert len(adapters) == 1
    assert isinstance(adapters[0], GenericLinuxAdapter)


def test_registry_sort_by_descending_priority() -> None:
    class _High(VendorAdapter):
        name: ClassVar[str] = "high"
        priority: ClassVar[int] = 100

        def match(self, raw):
            return False

        def convert(self, raw, image_dir):
            return {}

    class _Mid(VendorAdapter):
        name: ClassVar[str] = "mid"
        priority: ClassVar[int] = 50

        def match(self, raw):
            return False

        def convert(self, raw, image_dir):
            return {}

    register(_Mid())
    register(_High())

    names = [a.name for a in iter_adapters()]
    assert names == ["high", "mid", "generic_linux"]


def test_same_priority_ties_resolved_by_insertion_order() -> None:
    class _A(VendorAdapter):
        name: ClassVar[str] = "alpha"
        priority: ClassVar[int] = 50

        def match(self, raw):
            return False

        def convert(self, raw, image_dir):
            return {}

    class _B(VendorAdapter):
        name: ClassVar[str] = "bravo"
        priority: ClassVar[int] = 50

        def match(self, raw):
            return False

        def convert(self, raw, image_dir):
            return {}

    # Register B before A; both at priority 50.
    register(_B())
    register(_A())

    names = [a.name for a in iter_adapters()]
    # B was registered first, so on the priority-50 tier it must dispatch first.
    assert names == ["bravo", "alpha", "generic_linux"]


def test_generic_linux_is_structurally_last() -> None:
    """Even with vendor adapters added at higher priorities, generic_linux must be last."""
    class _Vendor(VendorAdapter):
        name: ClassVar[str] = "vendor"
        priority: ClassVar[int] = 80

        def match(self, raw):
            return False

        def convert(self, raw, image_dir):
            return {}

    register(_Vendor())
    assert iter_adapters()[-1].name == "generic_linux"


# ---- dispatch ----------------------------------------------------------


def test_select_adapter_picks_highest_priority_match() -> None:
    class _OnlyMatchesAlpine(VendorAdapter):
        name: ClassVar[str] = "alpine_only"
        priority: ClassVar[int] = 80

        def match(self, raw):
            return raw.get("image") == "alpine:latest"

        def convert(self, raw, image_dir):
            return {"name": "alpine"}

    register(_OnlyMatchesAlpine())

    matched = select_adapter({"image": "alpine:latest"})
    assert matched is not None and matched.name == "alpine_only"

    # Anything else falls through to generic_linux.
    fallback = select_adapter({"image": "ubuntu:22.04"})
    assert fallback is not None and fallback.name == "generic_linux"


def test_generic_linux_match_always_truthy() -> None:
    assert GenericLinuxAdapter().match({}) is True
    assert GenericLinuxAdapter().match({"image": "any"}) is True


def test_generic_linux_convert_emits_valid_template_with_eveng_raw() -> None:
    raw = {
        "image": "alpine:latest",
        "cpu": 2,
        "ram": 512,
        "ethernet": 3,
        "console": "telnet",
        "qemu_nic": "e1000",
        "_eveng_raw": {"yaml": {"image": "alpine:latest"}},
    }
    template = GenericLinuxAdapter().convert(raw, Path("/var/lib/nova-ve/images/qemu/alpine"))
    assert template["schema"] == 1
    assert template["kind"] == "qemu"
    assert template["image"] == "alpine:latest"
    assert template["cpu"] == 2
    assert template["ram"] == 512
    assert template["ethernet"] == 3
    assert template["extras"]["qemu_nic"] == "e1000"
    assert template["extras"]["_eveng_raw"] == {"yaml": {"image": "alpine:latest"}}

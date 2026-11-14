"""Registry of the molecular models exposed by the Blablador shim.

Each model is a ModelSpec: the alias to send, the form a user fills in, and a
builder that turns cleaned form data into the shim's `parameters` object.
Adding a model means one module under jobs/specs/.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from django import forms


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    slug: str
    # The exact API model ID. Never guessed; see GET /v1/models.
    alias: str
    name: str
    blurb: str
    form_class: type[forms.Form]
    # cleaned_data -> the shim's `parameters` object.
    params_builder: Callable[[dict[str, Any]], dict[str, Any]]
    result_template: str = "jobs/results/_generic.html"
    # What the result ZIP is expected to contain, for the UI's description.
    result_note: str = ""
    # Caveats from the deployment handoff, shown on the submit page.
    caveats: tuple[str, ...] = ()


_REGISTRY: dict[str, ModelSpec] = {}


def register(spec: ModelSpec) -> ModelSpec:
    if spec.slug in _REGISTRY:
        raise ValueError(f"duplicate model slug: {spec.slug}")
    _REGISTRY[spec.slug] = spec
    return spec


def get(slug: str) -> ModelSpec:
    return _REGISTRY[slug]


def all_specs() -> list[ModelSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def by_alias(alias: str) -> ModelSpec | None:
    return next((s for s in _REGISTRY.values() if s.alias == alias), None)


def load_specs() -> None:
    from importlib import import_module

    for mod in (
        "esmfold2",
        "bioemu2",
        "rfdiffusion3",
        "bindcraft",
        "proteina_complexa",
    ):
        import_module(f"jobs.specs.{mod}")

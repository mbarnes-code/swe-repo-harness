"""Inventory, coordinates, manifests (SPEC §5.2)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, computed_field, model_validator

from fleet.models.base import FleetModel, utcnow
from fleet.models.enums import Ecosystem

RepoId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")]
Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class Coordinate(FleetModel):
    """The single normalized dependency address (ADR-0005). Nothing downstream of an
    adapter ever sees an ecosystem-specific identifier again. `key` format is ADR-0017."""

    model_config = FleetModel.model_config | {"frozen": True}

    ecosystem: Ecosystem
    group: str = Field(
        default="", description="Maven groupId / npm @scope / go host+org; '' if N/A"
    )
    name: str = Field(min_length=1)
    version_spec: str | None = Field(default=None, description="Raw, un-resolved range as written")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        """Canonical, case-folded join key. THE index key for all cross-repo queries."""
        return f"{self.ecosystem.value}:{self.group.lower()}:{self.name.lower()}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def group_path(self) -> str:
        """Filesystem-safe group segment used by bazel/layout.py."""
        return self.group.lower().replace(".", "/").replace("@", "").replace(":", "/")


class RawDependency(FleetModel):
    """Adapter output before normalization. The ONLY model that may carry ecosystem-native text."""

    raw_id: str = Field(min_length=1, description="e.g. 'com.acme:commons:1.4.0' or '@acme/ui'")
    version_spec: str | None = None
    scope: str | None = Field(default=None, description="compile/test/dev/optional, as written")
    optional: bool = False
    source_line: int | None = Field(default=None, ge=1)


class ManifestRef(FleetModel):
    """One parsed manifest file. Persisted to the `manifests` table."""

    manifest_id: int | None = Field(default=None, description="SQLite rowid; None before insert")
    repo_id: RepoId
    path: str = Field(description="Repo-relative POSIX path")
    ecosystem: Ecosystem
    adapter: str = Field(description="ManifestAdapter.name that claimed this file")
    adapter_version: int = Field(ge=1, description="Bump to force re-parse on re-scan")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publishes: Coordinate | None = Field(
        default=None, description="Coordinate this manifest publishes"
    )
    dependency_count: int = Field(default=0, ge=0)
    low_confidence: bool = Field(
        default=False, description="Set → eligible for the LLM extract slot"
    )
    parse_error: str | None = None
    parsed_at: datetime = Field(default_factory=utcnow)


class RepoRecord(FleetModel):
    """One source repository in the fleet. Persisted to `repos`."""

    repo_id: RepoId
    name: str = Field(min_length=1)
    url: HttpUrl | str = Field(
        description="Credential-free. Any userinfo is stripped at preflight and never persisted."
    )
    default_branch: str = "main"
    default_branch_source: Literal["symbolic-ref", "fallback", "config"] = "symbolic-ref"
    head_sha: Sha1 | None = None
    ecosystems: list[Ecosystem] = Field(default_factory=list)
    primary_coordinate: Coordinate | None = Field(
        default=None, description="The coordinate this repo publishes; drives monorepo dest path"
    )
    dest_path: str | None = Field(default=None, description="Monorepo destination; None → computed")
    kind: Literal["service", "library", "monolith", "tool", "unknown"] = "unknown"
    framework: str | None = None
    owner_hint: str | None = None
    size_bytes: int = Field(default=0, ge=0)
    commit_count: int = Field(default=0, ge=0)
    last_commit_at: datetime | None = None
    cloned_at: datetime | None = None
    # ---- git preflight (§3.1 step 1); all set before any other phase touches the repo ----
    is_shallow: bool = False
    submodule_count: int = Field(default=0, ge=0)
    has_lfs: bool = False
    lfs_object_bytes: int = Field(default=0, ge=0)
    largest_blob_bytes: int = Field(default=0, ge=0)
    preflight_ok: bool | None = Field(
        default=None, description="None = not yet run; False = gated, see `findings`"
    )
    # ---- graph-derived (§3.5), written by Phase 1 step 7 ----
    blast_radius: int = Field(
        default=0, ge=0, description="|transitive dependents| over the ordering subgraph"
    )
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _dest_requires_coordinate(self) -> Self:
        if self.dest_path is None and self.primary_coordinate is None and self.head_sha is not None:
            raise ValueError(f"{self.repo_id}: cloned repo needs dest_path or primary_coordinate")
        return self

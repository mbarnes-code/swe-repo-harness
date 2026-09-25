"""§9 configuration layer: the loader's five startup rules, precedence, and per-section digests.

Every test here answers "why does this matter", not "does this line run":

* the **defaults** are the numbers §9 documents, and a run that silently gets a different
  `run_max_cost_usd` than the file says is a run whose ceiling is fiction;
* an **unpriced target** is the guard that makes the cost ceiling real (§9 rule 5): with no price
  the ledger reserves $0.00 for every call, `run_max_cost_usd` never trips, and a 250-repo run
  bills unbounded dollars while §12.24 still passes;
* **precedence** is what makes `--set` and `FLEET_*` usable at all — an override that loses to the
  file is an operator who thinks they lowered a budget and did not;
* **per-section digests** are what `fleet resume --accept-drift SECTION` accepts one section of;
  a digest that moves for an unrelated edit forces the operator to accept everything to accept one,
  and a digest that is not stable across processes fails every resume;
* a **secret** that reaches a digest, a `repr()` or a `model_dump()` is a secret in the log the
  redactor exists to keep it out of (§9 rule 4, §11.4).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from fleet.models.enums import ContextPolicy, ModelTier, TransformTier
from fleet.models.tasks import BackendTarget, Price
from fleet.settings import (
    _BACKEND_EXTRAS,
    CONFIG_SECTIONS,
    MODELS_SECTION,
    SHIPPED_BACKENDS,
    BuildSection,
    ConfigFileError,
    ConfigValidationError,
    FleetSettings,
    LadderRung,
    RedactionSection,
    SecretInConfigError,
    TransformSection,
    UnpricedTargetError,
    UnresolvedReferenceError,
    canonical_json,
    target_price_usd,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")

# The §9 `default` profile, split into named pieces so a test can swap ONE target without a
# hundred-column string literal. The shape is exactly §9's: `roles` (role → tier), then
# `profiles` (tier → ordered targets).
MODELS_HEADER = """\
version: 2
roles:
  escalation: HEAVY
  transform_repair: WORKHORSE
  repo_classify: CHEAP
default_profile: default
profiles:
  default:
"""

HEAVY_TARGET = """\
    HEAVY:
      - { backend: anthropic, model_id: claude-opus-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: { in_per_mtok: 5.0, out_per_mtok: 25.0 } }
"""

WORKHORSE_TARGET = """\
    WORKHORSE:
      - { backend: anthropic, model_id: claude-sonnet-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: { in_per_mtok: 3.0, out_per_mtok: 15.0 } }
"""

CHEAP_TARGET = """\
    CHEAP:
      - { backend: anthropic, model_id: claude-haiku-4-5,
          api_key_env: ANTHROPIC_API_KEY,
          price: { in_per_mtok: 1.0, out_per_mtok: 5.0 } }
"""

MODELS_YAML = MODELS_HEADER + HEAVY_TARGET + WORKHORSE_TARGET + CHEAP_TARGET

REPOS_YAML = """\
version: 1
defaults:
  ref: main
repos:
  - name: acme-commons
    url: https://github.com/acme/acme-commons
  - name: legacy-batch
    url: https://github.com/acme/legacy-batch
    skip: true
"""


LOCAL_PROFILE_YAML = """\
  local:
    HEAVY:
      - { backend: openai_compatible, model_id: local-heavy, effort: high, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY,
          capabilities_override: { max_context: 131072 } }
    WORKHORSE:
      - { backend: openai_compatible, model_id: local-workhorse, effort: medium, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY }
    CHEAP:
      - { backend: openai_compatible, model_id: local-cheap, effort: low, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY }
"""


# The §15.2 pilot profile (config/models.yaml `profiles.pilot`): one dedicated Spark host serving
# one model across all three tiers, no `capabilities_override` (unverified real server), no
# `api_key_env` (unknown whether the eventual server checks one) — see that file's own comment.
PILOT_PROFILE_YAML = """\
  pilot:
    HEAVY:
      - { backend: openai_compatible, model_id: nvidia/nemotron-3-super-120b-a12b, effort: high,
          price: free,
          base_url: 'http://pilot-spark.internal:8000/v1' }
    WORKHORSE:
      - { backend: openai_compatible, model_id: nvidia/nemotron-3-super-120b-a12b, effort: medium,
          price: free,
          base_url: 'http://pilot-spark.internal:8000/v1' }
    CHEAP:
      - { backend: openai_compatible, model_id: nvidia/nemotron-3-super-120b-a12b, effort: low,
          price: free,
          base_url: 'http://pilot-spark.internal:8000/v1' }
"""


def write_config(
    tmp_path: Path,
    *,
    fleet: str = "run:\n  monorepo_path: ../acme-monorepo\n",
    models: str = MODELS_YAML,
    repos: str = REPOS_YAML,
) -> Path:
    """A three-file `config/` directory, the shape §9 documents. Returns the config dir."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "fleet.yaml").write_text(textwrap.dedent(fleet), encoding="utf-8")
    (config_dir / "models.yaml").write_text(models, encoding="utf-8")
    (config_dir / "repos.yaml").write_text(repos, encoding="utf-8")
    return config_dir


def load(config_dir: Path, **kwargs: object) -> FleetSettings:
    kwargs.setdefault("env", {})
    return FleetSettings.load(config_dir, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------------------


def test_minimal_config_lands_every_documented_default(tmp_path: Path) -> None:
    """A `fleet.yaml` that sets one key must still produce §9's numbers everywhere else.

    Each assertion quotes §9 verbatim, because a default that drifts from the spec is invisible:
    nothing fails, the run just enforces a ceiling nobody wrote down.
    """
    settings = load(write_config(tmp_path))
    cfg = settings.config

    assert cfg.run.monorepo_path == "../acme-monorepo"      # the one key the file set
    assert cfg.run.monorepo_branch == "integration"          # §9 `monorepo_branch: integration`
    assert cfg.run.stale_after_s == 300                      # "THE authoritative liveness TTL"
    assert cfg.run.reaper_interval_s == 30                   # §6 reaper scan period
    assert cfg.concurrency.docker == 4                       # §11 semaphore classes
    assert cfg.concurrency.llm.workhorse == 8                # one semaphore per ModelTier
    assert cfg.budgets.run_max_cost_usd == 400.0             # "hard stop; halted=1 is sticky"
    assert cfg.budgets.wave_max_cost_usd_per_repo == 8.0     # the wave ceiling SCALES
    assert cfg.budgets.task_max_wallclock_s.transform == 1800
    assert cfg.preflight.branch_fallbacks == ("main", "master", "trunk", "develop")
    assert cfg.preflight.baseline_build.enabled is True      # "the gate §14.1 claims to have"
    assert cfg.redaction.enabled is True                     # "not configurable off"
    assert cfg.scan.max_symbols_per_repo == 500_000
    assert cfg.scan.contracts.min_consumers == 2
    assert cfg.graph.min_confidence == 0.5
    assert cfg.graph.break_cycles == "auto"
    assert cfg.transform.max_attempts == 3                   # ADR-0014's default ladder length
    assert len(cfg.transform.ladder) == 3                    # "Length must equal max_attempts"
    assert cfg.transform.engines["ast-grep"] == "fleet.rewrite.astgrep"
    assert cfg.transform.anchoring.on_exhausted == "advance"
    assert cfg.build.ruleset_versions["rules_jvm_external"] == "6.7"
    assert cfg.stubs.revalidation == "batched"               # DEFAULT, per §9's own comment
    assert cfg.stubs.on_budget_exhausted == "hold"           # "the only value that is not a lie"
    assert cfg.verify.container_memory == "8g"
    assert cfg.verify.network == "none"
    assert cfg.llm.profile == "default"
    assert cfg.llm.cache_mode == "read-write"
    assert cfg.llm.max_schema_repairs == 1
    assert cfg.llm.rate_limit.honor_retry_after is True      # "OBEYED, never merely logged"
    assert cfg.llm.rate_limit.defaults.rpm == 0              # 0 = unlimited
    assert cfg.llm.failover.on_tier_exhausted == "halt"      # fail closed, exit 8
    assert cfg.llm.require_capabilities[ModelTier.HEAVY].min_context == 100_000
    assert cfg.llm.harmony_vocab_dir is None                 # ADR-0148/§12.50, B1: opt-in only
    assert cfg.pr.merge_wait_timeout_s == 172_800            # 48 h
    assert cfg.gc.cache_max_age == "30d"
    assert cfg.gc.cache_max_age_s() == 30 * 86_400
    assert settings.repos.ref_for(settings.repos.repos[0]) == "main"


# `ruleset: (lowest version measured to work under Bazel 9.2.0, the verbatim Bazel error the
# version below it gives)`. Every entry was produced by running the real binary — a `load()` of
# the ruleset's own `defs.bzl` for four of them, and a full `ts_project` + `js_binary` analysis for
# `aspect_rules_js`, which LOADS at 2.1.3 and only fails when a target is configured. The two
# real-Bazel tests in `test_bazel.py` re-measure both claims live; this offline copy exists so the
# fast suite still catches a pin walked backwards on a host with no network.
_LOWEST_WORKING: dict[str, tuple[tuple[int, ...], str]] = {
    "aspect_rules_js": ((3, 0, 0), "rules_nodejs 6.3.x: rule() got unexpected keyword argument "
                                   "'incompatible_use_toolchain_transition'"),
    "rules_rust": ((0, 65, 0), "The CcInfo symbol has been removed (rust/private/rustdoc_test)"),
    "rules_go": ((0, 61, 1), "The CcInfo symbol has been removed (go/private/rules/cross.bzl)"),
    "gazelle": ((0, 52, 2), "The CcInfo symbol has been removed, through rules_go"),
    "aspect_rules_ts": ((3, 10, 0), "No repository visible as '@local_config_platform'"),
}


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def test_no_ruleset_pin_regresses_below_a_version_known_to_fail_under_real_bazel() -> None:
    """**Why (D8):** ADR-0041 made `build.ruleset_versions` a true pin
    (`single_version_override`), so the configured version is the version Bazel runs — MVS can no
    longer float quietly past one that a newer Bazel rejects. Five of the eight shipped pins were
    on the wrong side of Bazel 9's removal of the `CcInfo` global, the `@local_config_platform`
    repository and `rule(incompatible_use_toolchain_transition)`, and the failure is not a
    warning: the `MODULE.bazel` does not load, for every repo in the fleet, so Phase 3 produces
    zero green builds and the error names the ruleset rather than this file.

    The guard is a floor per ruleset rather than an equality on the current strings, because the
    thing that must never happen again is a pin moving *down* past a measured-broken version;
    moving one up is the fix, and a change-detector test that fired on the fix would be noise.
    """
    pinned = BuildSection().ruleset_versions
    for ruleset, (floor, why) in _LOWEST_WORKING.items():
        assert ruleset in pinned, f"{ruleset} is no longer pinned; the floor below is unverified"
        assert _version_tuple(pinned[ruleset]) >= floor, (
            f"build.ruleset_versions pins {ruleset}=={pinned[ruleset]}, below the lowest version "
            f"measured to work ({'.'.join(str(p) for p in floor)}); anything lower gives: {why}"
        )


# --------------------------------------------------------------------------------------
# §9 rule 5 — the price rule is what makes the cost ceiling real
# --------------------------------------------------------------------------------------


def test_unpriced_target_is_refused_naming_profile_tier_and_index(tmp_path: Path) -> None:
    """A target with no `price` and no `price: free` must be exit 2 at startup.

    §9 rule 5: with no declared price `price(...)` misses for every call, `budget_ledger.spent_usd`
    stays `0.00` and `run_max_cost_usd` never trips. The operator has to be told WHICH target, so
    the message names the profile, the tier and the index rather than "a target".
    """
    unpriced = """\
    HEAVY:
      - { backend: anthropic, model_id: claude-opus-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY }
"""
    models = MODELS_YAML.replace(HEAVY_TARGET, unpriced)
    with pytest.raises(UnpricedTargetError) as excinfo:
        load(write_config(tmp_path, models=models))

    message = str(excinfo.value)
    assert "models.yaml" in message
    assert "profiles.default.HEAVY[0]" in message
    assert "anthropic:claude-opus-5" in message
    assert "price: free" in message  # tells the operator the other legal answer


def test_price_free_is_accepted_and_yields_zero_cost(tmp_path: Path) -> None:
    """`free` is a positive assertion about a locally-served target, not the absence of a key —
    which is what distinguishes "this model costs nothing" from "nobody told the ledger" (§9)."""
    free_cheap = """\
    CHEAP:
      - { backend: openai_compatible, model_id: local-cheap, effort: low, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY }
"""
    models = MODELS_YAML.replace(CHEAP_TARGET, free_cheap)
    settings = load(write_config(tmp_path, models=models))

    cheap = settings.targets_for_tier(ModelTier.CHEAP)[0]
    assert cheap.price == "free"
    assert target_price_usd(cheap, in_tokens=1_000_000, out_tokens=1_000_000) == 0.0

    heavy = settings.targets_for_tier(ModelTier.HEAVY)[0]
    assert target_price_usd(heavy, in_tokens=1_000_000, out_tokens=0) == 5.0


def test_a_zero_rate_price_must_be_written_as_free(tmp_path: Path) -> None:
    """`{in: 0, out: 0}` and `free` mean the same to arithmetic and different things to a reader;
    §9 rule 5 wants the assertion, so the ambiguous spelling is refused rather than accepted."""
    models = MODELS_YAML.replace(
        "price: { in_per_mtok: 1.0, out_per_mtok: 5.0 }",
        "price: { in_per_mtok: 0.0, out_per_mtok: 0.0 }",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, models=models))
    assert "free" in str(excinfo.value)


def test_pilot_profile_loads_and_lands_one_model_free_with_no_override(tmp_path: Path) -> None:
    """§15.2's prerequisite checklist item: `profiles.pilot` must load/validate cleanly through
    the same §9 startup path `profiles.local` does, ahead of the real Spark being provisioned.

    Unlike `local` (a different model per tier), `pilot` routes all three tiers to the SAME
    customer-specified model on one dedicated (still-placeholder) host, so all three targets must
    resolve to that one `model_id`. No `capabilities_override` is declared (nobody has verified
    the real server's capabilities yet), so `negotiate()` would land on the backend's honest
    PROMPTED floor rather than an unverified claim — checked here as an empty override dict,
    `BackendTarget.capabilities_override`'s documented default for "the operator did not say"."""
    models = MODELS_YAML + PILOT_PROFILE_YAML
    settings = load(write_config(tmp_path, models=models), cli_overrides={"llm.profile": "pilot"})

    for tier in (ModelTier.HEAVY, ModelTier.WORKHORSE, ModelTier.CHEAP):
        target = settings.targets_for_tier(tier)[0]
        assert target.backend == "openai_compatible"
        assert target.model_id == "nvidia/nemotron-3-super-120b-a12b"
        assert target.price == "free"
        assert target_price_usd(target, in_tokens=1_000_000, out_tokens=1_000_000) == 0.0
        assert target.capabilities_override == {}


# --------------------------------------------------------------------------------------
# precedence: CLI flags → FLEET_* env → config/fleet.yaml → defaults
# --------------------------------------------------------------------------------------


def test_file_beats_default(tmp_path: Path) -> None:
    """The lowest-priority pair. A file value that loses to a default is a config nobody can use."""
    config = write_config(tmp_path, fleet="budgets:\n  run_max_cost_usd: 100.0\n")
    assert load(config).config.budgets.run_max_cost_usd == 100.0


def test_env_beats_file(tmp_path: Path) -> None:
    """`FLEET_BUDGETS__RUN_MAX_COST_USD` is how CI narrows a ceiling without editing the repo."""
    config = write_config(tmp_path, fleet="budgets:\n  run_max_cost_usd: 100.0\n")
    settings = load(config, env={"FLEET_BUDGETS__RUN_MAX_COST_USD": "200"})
    assert settings.config.budgets.run_max_cost_usd == 200.0


def test_cli_beats_env(tmp_path: Path) -> None:
    """The operator at the keyboard wins over the exported variable they forgot about."""
    config = write_config(tmp_path, fleet="budgets:\n  run_max_cost_usd: 100.0\n")
    settings = load(
        config,
        env={"FLEET_BUDGETS__RUN_MAX_COST_USD": "200"},
        cli_overrides={"budgets.run_max_cost_usd": 300.0},
    )
    assert settings.config.budgets.run_max_cost_usd == 300.0


def test_cli_beats_file_and_unset_keys_keep_their_neighbours(tmp_path: Path) -> None:
    """Merging is deep: overriding one key in a section must not blank the rest of the section."""
    config = write_config(
        tmp_path, fleet="budgets:\n  run_max_cost_usd: 100.0\n  repo_max_cost_usd: 2.0\n"
    )
    settings = load(config, cli_overrides={"budgets.run_max_cost_usd": 300.0})
    assert settings.config.budgets.run_max_cost_usd == 300.0
    assert settings.config.budgets.repo_max_cost_usd == 2.0   # from the file
    assert settings.config.budgets.max_disk_gb == 400          # from the default


def test_env_reaches_a_nested_section(tmp_path: Path) -> None:
    """`__` addresses depth, so `concurrency.llm.heavy` — the key a single-GPU host must lower —
    is reachable without a file edit."""
    settings = load(write_config(tmp_path), env={"FLEET_CONCURRENCY__LLM__HEAVY": "1"})
    assert settings.config.concurrency.llm.heavy == 1


# --------------------------------------------------------------------------------------
# per-section digests (§6 runs.config_digests, §10 --accept-drift)
# --------------------------------------------------------------------------------------


def _digests_in_subprocess(config_dir: Path, hashseed: str) -> dict[str, str]:
    """Compute the digests in a fresh interpreter under an explicit `PYTHONHASHSEED`.

    In-process comparison cannot catch the bug this guards: `hash()` and dict iteration order are
    stable *within* one process, so a digest built on either would only diverge on the resume — the
    one moment the run has to trust it.
    """
    script = (
        "import json,sys;"
        "from fleet.settings import FleetSettings;"
        "print(json.dumps(FleetSettings.load(sys.argv[1], env={}).section_digests))"
    )
    proc = subprocess.run(  # noqa: S603 — argv is this file's own literal script plus tmp_path
        [sys.executable, "-c", script, str(config_dir)],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": hashseed, "PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"},
    )
    return dict(json.loads(proc.stdout))


def test_section_digests_are_stable_across_processes(tmp_path: Path) -> None:
    """Two processes with different `PYTHONHASHSEED` must agree, or `fleet resume` reports drift
    on a config nobody touched and refuses to continue a three-day run."""
    config = write_config(tmp_path)
    in_process = dict(load(config).section_digests)

    assert _digests_in_subprocess(config, "0") == in_process
    assert _digests_in_subprocess(config, "12345") == in_process


def test_every_drift_section_has_a_digest_and_the_sha_is_derived_from_them(tmp_path: Path) -> None:
    """§6: `config_sha256` is "DERIVED as the hash of the per-section digests ... so the two can
    never disagree about what the config was"."""
    settings = load(write_config(tmp_path))
    assert set(settings.section_digests) == {*CONFIG_SECTIONS, MODELS_SECTION}
    assert all(len(d) == 64 for d in settings.section_digests.values())

    import hashlib

    expected = hashlib.sha256(
        canonical_json(dict(settings.section_digests)).encode("utf-8")
    ).hexdigest()
    assert settings.config_sha256() == expected


def test_canonical_json_refuses_nan_rather_than_emitting_a_non_standard_literal() -> None:
    """`canonical_json` added `allow_nan=False` (round VIII fix, matching `state/digest.py`'s own
    `canonical_json`) so a NaN/Infinity float never round-trips into a non-standard JSON literal
    inside what is supposed to be a canonical, portable digest input (Rule 11: fail loud instead
    of silently emitting `NaN`, which strict JSON parsers elsewhere in the fleet would reject)."""
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"a": float("nan")})
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"a": float("inf")})


def test_editing_one_section_moves_exactly_one_digest(tmp_path: Path) -> None:
    """The whole point of per-section digests (§10): `--accept-drift budgets` must accept ONE
    section. If an unrelated section's digest moved too, accepting one would mean accepting every
    co-edited change — "which is how an operator loses a run"."""
    before = load(write_config(tmp_path)).section_digests
    edited = write_config(tmp_path, fleet="budgets:\n  run_max_cost_usd: 42.0\n")
    after = load(edited).section_digests

    moved = [name for name in before if before[name] != after[name]]
    assert moved == ["budgets"]


def test_changing_the_active_profile_moves_only_the_models_digest(tmp_path: Path) -> None:
    """`--profile` "changes which profile every role resolves through and nothing else" (§10), so
    it must show up as `models_profile` drift and not as `llm` drift."""
    models = MODELS_YAML + LOCAL_PROFILE_YAML
    config = write_config(tmp_path, models=models)
    default = load(config).section_digests
    local = load(config, cli_overrides={"llm.profile": "local"}).section_digests

    moved = sorted(name for name in default if default[name] != local[name])
    assert moved == ["llm", MODELS_SECTION]   # `llm` only because `llm.profile` IS an llm key


def test_an_empty_baseline_is_never_read_as_every_section_matching(tmp_path: Path) -> None:
    """§6's back-fill: `config_digests` defaults to `'{}'` meaning "no baseline was recorded".
    Treating that as agreement would let a pre-7 run resume under a silently changed config."""
    settings = load(write_config(tmp_path))
    assert settings.drifted_sections({}) == tuple(sorted(settings.section_digests))
    assert settings.drifted_sections(dict(settings.section_digests)) == ()


# --------------------------------------------------------------------------------------
# fail loud (Rule 11): the message names the file and the key
# --------------------------------------------------------------------------------------


def test_unknown_key_in_fleet_yaml_names_the_file_and_the_key(tmp_path: Path) -> None:
    """A typo'd key that is silently ignored is a setting the operator believes is in force.
    `budgets.run_max_cost` (no `_usd`) must stop the run, naming both the file and the key."""
    config = write_config(tmp_path, fleet="budgets:\n  run_max_cost: 100.0\n")
    with pytest.raises(ConfigValidationError) as excinfo:
        load(config)

    message = str(excinfo.value)
    assert str(config / "fleet.yaml") in message
    assert "budgets.run_max_cost" in message


def test_unknown_fleet_env_var_is_refused_naming_the_variable(tmp_path: Path) -> None:
    """`FLEET_BUDGET__RUN_MAX_COST_USD` (singular) addresses nothing. Ignoring it means the
    operator watches the run spend the number they thought they had lowered."""
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path), env={"FLEET_BUDGET__RUN_MAX_COST_USD": "1"})
    assert "FLEET_BUDGET__RUN_MAX_COST_USD" in str(excinfo.value)


def test_a_malformed_value_reports_the_env_var_as_the_origin(tmp_path: Path) -> None:
    """The key exists in three places; the error must blame the layer that actually set it, or the
    operator edits a file that was never wrong."""
    config = write_config(tmp_path, fleet="graph:\n  break_cycles: auto\n")
    with pytest.raises(ConfigValidationError) as excinfo:
        load(config, env={"FLEET_GRAPH__BREAK_CYCLES": "sometimes"})
    message = str(excinfo.value)
    assert "FLEET_GRAPH__BREAK_CYCLES" in message
    assert "graph.break_cycles" in message


def test_verify_network_rejects_invalid_values(tmp_path: Path) -> None:
    """§9 security fix: `verify.network` must be Literal["none"], not env-overridable.
    Setting FLEET_VERIFY__NETWORK=bridge must raise ConfigValidationError.
    """
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path), env={"FLEET_VERIFY__NETWORK": "bridge"})
    message = str(excinfo.value)
    assert "FLEET_VERIFY__NETWORK" in message
    assert "verify.network" in message


def test_models_yaml_version_1_is_refused_naming_the_two_level_shape(tmp_path: Path) -> None:
    """§9: "a `version: 1` file is refused with a message naming the two-level shape rather than
    silently reinterpreted"."""
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, models=MODELS_YAML.replace("version: 2", "version: 1")))
    assert "roles" in str(excinfo.value) and "profiles" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# unresolvable references are startup errors, not lazy failures
# --------------------------------------------------------------------------------------


def test_unknown_backend_is_a_startup_error(tmp_path: Path) -> None:
    """§13 row 36: a typo'd transport must not become a `KeyError` in wave 7, hours of spend in."""
    models = MODELS_YAML.replace(
        "backend: anthropic, model_id: claude-opus-5",
        "backend: anthropik, model_id: claude-opus-5",
    )
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path, models=models))
    assert "anthropik" in str(excinfo.value)


def test_openai_compatible_target_without_base_url_is_refused(tmp_path: Path) -> None:
    """§9 rule 2: each backend validates its own target fields."""
    models = MODELS_YAML.replace(
        CHEAP_TARGET,
        "    CHEAP:\n"
        "      - { backend: openai_compatible, model_id: local-cheap, effort: low,\n"
        "          price: free }\n",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, models=models))
    assert "base_url" in str(excinfo.value)


def test_harmony_gpt_oss_target_without_base_url_is_refused_at_startup(tmp_path: Path) -> None:
    """§13 row 36: `harmony_gpt_oss` has no vendor default endpoint either. Missing from
    `_REQUIRED_TARGET_FIELDS`, a `pilot` target with no `base_url` loaded with `base_url: None`
    and failed only at the first call."""
    models = MODELS_YAML.replace(
        CHEAP_TARGET,
        "    CHEAP:\n"
        "      - { backend: harmony_gpt_oss, model_id: gpt-oss-120b, effort: low,\n"
        "          price: free }\n",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load(
            write_config(tmp_path, models=models),
            known_backends=("anthropic", "openai_compatible", "harmony_gpt_oss"),
        )
    assert "profiles.default.CHEAP[0].base_url" in str(excinfo.value)


def _cheap_openai_target(base_url: str) -> str:
    return (
        "    CHEAP:\n"
        "      - { backend: openai_compatible, model_id: local-cheap, effort: low,\n"
        f"          price: free, base_url: '{base_url}' }}\n"
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_base_url_is_refused_at_load_not_at_call_time(tmp_path: Path, blank: str) -> None:
    """§13 row 36 wants a target with no usable `base_url` to fail at STARTUP, naming profile,
    tier, target index and field. An `is None` check satisfied the schema and nothing else: an
    empty or whitespace-only string is not an endpoint, so the run cleared config validation,
    cloned repos, and only then failed on the first call — the exact deferral row 36 exists to
    prevent. The whitespace case matters because YAML quoting makes it easy to write by accident.
    """
    models = MODELS_YAML.replace(CHEAP_TARGET, _cheap_openai_target(blank))
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, models=models))

    message = str(excinfo.value)
    assert "base_url" in message                     # the field the operator must edit
    assert "profiles.default.CHEAP[0].base_url" in message   # profile, tier, index, field


def test_a_usable_base_url_still_loads(tmp_path: Path) -> None:
    """The other half of the blank-`base_url` guard: the check must reject empty strings without
    rejecting real ones, or every local profile stops booting."""
    models = MODELS_YAML.replace(CHEAP_TARGET, _cheap_openai_target("http://localhost:8001/v1"))
    settings = load(write_config(tmp_path, models=models))
    assert settings.targets_for_tier(ModelTier.CHEAP)[0].base_url == "http://localhost:8001/v1"


@pytest.mark.parametrize("backend", ["bedrock", "vertex"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_region_is_refused_too_because_one_table_drives_both(
    tmp_path: Path, blank: str, backend: str
) -> None:
    """`base_url` was not a special case. `_REQUIRED_TARGET_FIELDS` drives every backend's required
    fields from ONE table, so the `is None` check leaked `region: ""` to wave 7 for `bedrock` and
    `vertex` exactly as it leaked `base_url` for `openai_compatible`. Fixing the shared loop closed
    all of them at once; this pins that, so a later per-field rewrite cannot silently reopen the
    ones nobody wrote a test for.
    """
    models = MODELS_YAML.replace(
        CHEAP_TARGET,
        "    CHEAP:\n"
        f"      - {{ backend: {backend}, model_id: some-model, effort: low,\n"
        f"          price: free, region: '{blank}' }}\n",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load(
            write_config(tmp_path, models=models),
            known_backends=("anthropic", "openai_compatible", backend),
        )
    assert "profiles.default.CHEAP[0].region" in str(excinfo.value)


def test_an_uninstalled_backend_extra_is_named_in_the_message(tmp_path: Path) -> None:
    """The rule 2 gate validates `backend` against the LIVE registry, so on a host without the
    optional SDKs a config naming `bedrock` correctly exits 2. But "not in the registry" reads as
    a typo, and the operator hunts a spelling mistake that is not there. The message must say the
    backend exists and its extra is not installed, and name the extra to install."""
    models = MODELS_YAML.replace(
        CHEAP_TARGET,
        "    CHEAP:\n"
        "      - { backend: bedrock, model_id: anthropic.claude-haiku, effort: low,\n"
        "          region: us-east-1, price: free }\n",
    )
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(
            write_config(tmp_path, models=models),
            known_backends=("anthropic", "openai_compatible"),
        )

    message = str(excinfo.value)
    assert "fleet[bedrock]" in message               # the extra, not just "an extra"
    assert "not installed" in message


def test_an_uninstalled_harmony_extra_is_named_in_the_message(tmp_path: Path) -> None:
    """Mirrors `test_an_uninstalled_backend_extra_is_named_in_the_message` for the new backend."""
    models = MODELS_YAML.replace(
        CHEAP_TARGET,
        "    CHEAP:\n"
        "      - { backend: harmony_gpt_oss, model_id: gpt-oss-120b, price: free,\n"
        "          base_url: 'http://pilot-spark.internal:8000/v1' }\n",
    )
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(
            write_config(tmp_path, models=models),
            known_backends=("anthropic", "openai_compatible"),
        )

    message = str(excinfo.value)
    assert "fleet[harmony]" in message
    assert "not installed" in message


def test_a_typod_backend_is_not_reported_as_a_missing_extra(tmp_path: Path) -> None:
    """The other half: `anthropik` is a typo, not an uninstalled SDK. Telling the operator to
    `pip install` it would send them after a package that does not exist.

    The negative assertion alone would be satisfied by ANY `UnresolvedReferenceError` lacking the
    literal `pip install` — including one raised by a different check entirely, which would leave
    the typo branch untested while the test stayed green. So pin the message to this typo and to
    the rule 2 registry gate that must have produced it.
    """
    models = MODELS_YAML.replace(
        "backend: anthropic, model_id: claude-opus-5",
        "backend: anthropik, model_id: claude-opus-5",
    )
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path, models=models))

    message = str(excinfo.value)
    assert "anthropik" in message                    # THIS typo, from the backend gate
    assert "is not in the §7.7 registry" in message  # ...and from rule 2, not some earlier check
    assert "pip install" not in message              # no extra to install; it is a misspelling
    assert "CORE dependency" not in message          # nor a broken core install


def test_a_core_dependency_backend_is_not_reported_as_a_missing_extra(tmp_path: Path) -> None:
    """The third cause, distinct from both. `anthropic` is a CORE dependency, not an extra, so a
    correctly-spelled `backend: anthropic` that fails to register means its module did not import
    on this host. Telling that operator to install an extra sends them after `fleet[anthropic]`,
    which does not exist, while the real fault is their environment."""
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path), known_backends=("openai_compatible",))

    message = str(excinfo.value)
    assert "CORE dependency" in message
    assert "failed to import" in message
    assert "pip install" not in message              # there is no extra to install


def test_backend_extras_matches_pyproject(tmp_path: Path) -> None:
    """`_BACKEND_EXTRAS` hand-duplicates `[project.optional-dependencies]`. Nothing but this test
    ties them together, so a lane that adds a backend behind a new extra and forgets the mapping
    silently regresses that backend to the vague "if it ships as an extra" wording — the exact
    message naming the extra was introduced to replace, with no failure to notice it.

    Both directions are pinned: a new extra in pyproject must be classified here, and a mapping
    entry must name an extra that really exists.
    """
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = set(pyproject["project"]["optional-dependencies"])
    backend_extras = declared - {"dev"}   # `dev` is tooling, not a transport

    assert backend_extras == set(_BACKEND_EXTRAS.values())
    assert set(_BACKEND_EXTRAS) <= set(SHIPPED_BACKENDS)   # every key is a backend we ship

    # The gate's THIRD branch asserts that a shipped backend absent from `_BACKEND_EXTRAS` rides on
    # a core dependency ("its module failed to import; check the install"). That claim was prose
    # with nothing behind it. Bind it: the leftover names are exactly these two, and each one's
    # distribution really is in `[project.dependencies]` -- so the branch cannot start telling an
    # operator to check a core install for a package the manifest does not require.
    core_backend_distributions = {"anthropic": "anthropic", "openai_compatible": "openai"}
    assert set(SHIPPED_BACKENDS) - set(_BACKEND_EXTRAS) == set(core_backend_distributions)

    required = {
        re.split(r"[<>=!~\[]", dep)[0].strip() for dep in pyproject["project"]["dependencies"]
    }
    for backend, distribution in sorted(core_backend_distributions.items()):
        assert distribution in required, f"{backend} claims a core dep on absent {distribution!r}"


def test_redaction_defaults_match_obs_redact_patterns() -> None:
    """`RedactionSection.patterns` hand-copies the plain (non-structural, non-entropy-gated)
    entries of `obs.redact.PATTERNS` for `_refuse_secret_material`'s config-file scan — a
    different mechanism from `obs.redact.redact()`'s runtime egress redaction, so it cannot
    simply import the same object, but nothing else ties the two lists together. Without this
    test, a leak pattern widened in `obs.redact.PATTERNS` (a real vendor token format changing
    shape) leaves this copy silently narrower, and a `fleet.yaml` containing the newly-recognized
    secret loads clean instead of being refused at startup.

    Both directions are pinned: every kind this section declares must exist in `obs.redact`'s
    canonical list, and a REALISTIC sample of each kind's secret must be recognized identically by
    both — not just "some kind of match", but the same secret substring.
    """
    from fleet.obs.redact import PATTERNS as REDACT_PATTERNS

    redact_by_kind = {p.kind: p for p in REDACT_PATTERNS}
    settings_patterns = RedactionSection().patterns

    samples: dict[str, str] = {
        "github_pat": "github_pat_" + "A" * 24,
        "github_classic": "ghp_" + "A" * 20,
        "slack": "xoxb-" + "A" * 12,
        "aws_key": "AKIA" + "A" * 16,
        "anthropic": "sk-ant-" + "A" * 24,
        "openai_style": "sk-" + "A" * 40,
        "gcp_sa_key": '"private_key_id": "' + "a" * 40 + '"',
        "private_key": "-----BEGIN RSA PRIVATE KEY-----",
        "url_userinfo": "https://oauth2:github_pat_" + "A" * 24 + "@example.com/repo.git",
    }

    assert set(settings_patterns) <= set(redact_by_kind)
    assert set(settings_patterns) == set(samples)  # this test covers every declared kind

    for kind, text in samples.items():
        redact_pattern = redact_by_kind[kind]
        redact_match = redact_pattern.regex.search(text)
        settings_match = re.search(settings_patterns[kind], text)
        assert redact_match is not None, f"{kind}: obs.redact's own pattern missed its sample"
        assert settings_match is not None, f"{kind}: settings.py's pattern missed the same sample"
        redact_secret = redact_match.group(redact_pattern.group or 0)
        # `url_userinfo` is the one kind where the two patterns disagree on span by DESIGN, not by
        # drift: `obs.redact` anchors with a zero-width `(?<=://)` lookbehind so the scheme
        # separator is never part of the replaced span (its docstring: leaving the `@` out would
        # recreate the leaked pattern), while `settings.py`'s plain-`dict[str, str]` shape has no
        # lookbehind and matches the `://` literally. Both still name the SAME userinfo text.
        assert redact_secret in settings_match.group(0), (
            f"{kind}: obs.redact matched {redact_secret!r} but settings.py's pattern matched "
            f"{settings_match.group(0)!r} — these have drifted apart"
        )


def test_a_role_routed_to_an_empty_tier_is_a_startup_error(tmp_path: Path) -> None:
    """§9 rule 1, verbatim: "A role routed to an empty tier is a startup error, never a runtime
    `KeyError` in wave 7"."""
    models = MODELS_YAML.replace(CHEAP_TARGET, "    CHEAP: []\n")
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path, models=models))
    assert "repo_classify" in str(excinfo.value) and "CHEAP" in str(excinfo.value)


def test_a_role_routed_to_a_tier_that_is_not_a_modeltier_member_is_a_startup_error(
    tmp_path: Path,
) -> None:
    """§12.42's fourth `RunContext`-construction refusal: "a role whose tier is not a `ModelTier`
    member". `ModelsConfig.roles` is typed `dict[str, ModelTier]` (settings.py), so a bogus tier
    string fails Pydantic's own type validation before `_check_routing` ever runs — refused at
    `FleetSettings.load()`, not deferred to wave dispatch. The raised type is
    `ConfigValidationError` (verified against the real load path, not assumed), and the message
    names the offending role."""
    models = MODELS_YAML.replace("escalation: HEAVY", "escalation: NUCLEAR")
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, models=models))
    message = str(excinfo.value)
    assert "roles.escalation" in message           # names the offending role/field
    assert "HEAVY" in message and "WORKHORSE" in message and "CHEAP" in message  # legal members


def test_a_rule_naming_an_unregistered_engine_is_a_startup_error(tmp_path: Path) -> None:
    """§9 `transform.engines`: "An engine a rule names and this map does not resolve is a STARTUP
    error, exactly as an unknown `backend` is" — the fourth engine is data, but not a typo."""
    rules_dir = tmp_path / "config" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "java.yaml").write_text(
        "rules:\n  - rule_id: java-imports\n    engine: ts-morph\n"
        "  - rule_id: kotlin-imports\n    engine: kt-morph\n",
        encoding="utf-8",
    )
    config = write_config(tmp_path)
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(config, root=tmp_path)
    message = str(excinfo.value)
    assert "kt-morph" in message and "kotlin-imports" in message and "java.yaml" in message


def test_a_symlinked_rule_file_is_refused_at_startup(tmp_path: Path) -> None:
    """A symlinked rule file in the transform.rules_dir must be refused, not followed.
    `.is_symlink()` is checked before opening, and the refusal is loud with a named exception."""
    rules_dir = tmp_path / "config" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    # Create a real rule file outside the rules directory
    outside_rule = tmp_path / "outside.yaml"
    outside_rule.write_text(
        "rules:\n  - rule_id: external\n    engine: libcst\n",
        encoding="utf-8",
    )

    # Create a symlink to it in the rules directory
    symlink = rules_dir / "symlinked.yaml"
    symlink.symlink_to(outside_rule)

    assert symlink.is_symlink(), "fixture precondition: a REAL OS-level symlink"
    config = write_config(tmp_path)
    with pytest.raises(ConfigFileError, match="refusing to load a symlink"):
        load(config, root=tmp_path)


def test_a_ladder_rung_naming_an_undeclared_role_is_a_startup_error(tmp_path: Path) -> None:
    """The ADR-0021 ladder resolves `role` through `config/models.yaml`; an unknown role would
    otherwise surface as a failed escalation on attempt 3, after two attempts were paid for."""
    fleet = """\
        transform:
          ladder:
            - { tier: DETERMINISTIC }
            - { tier: LLM_REPAIR, role: transform_repair, context_policy: EVIDENCE_ONLY }
            - { tier: LLM_ESCALATION, role: heroics, context_policy: EVIDENCE_ONLY }
    """
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path, fleet=fleet))
    assert "heroics" in str(excinfo.value)


def test_an_unknown_profile_is_a_startup_error(tmp_path: Path) -> None:
    """`--profile local` against a models.yaml with no `local` must fail before Phase 1, not on the
    first LLM call in wave 3."""
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path), cli_overrides={"llm.profile": "hosted-eu"})
    assert "hosted-eu" in str(excinfo.value)


def test_concurrency_override_may_lower_but_not_raise(tmp_path: Path) -> None:
    """§9: "Raising above `concurrency.llm.*` is refused at startup" — an override is a local
    deployment's throttle, not a back door around the §11 semaphore classes."""
    settings = load(write_config(tmp_path, fleet="llm:\n  concurrency_overrides: { HEAVY: 1 }\n"))
    assert settings.llm_concurrency(ModelTier.HEAVY) == 1

    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, fleet="llm:\n  concurrency_overrides: { HEAVY: 9 }\n"))
    assert "concurrency.llm" in str(excinfo.value)


def test_require_capabilities_refuses_a_first_target_that_cannot_fit_the_evidence(
    tmp_path: Path,
) -> None:
    """§9 rule 3 / §13 row 38: HEAVY work needs the repo's evidence bundle to FIT. A 32k-context
    first target silently truncates every cross-repo judgement the tier exists for."""
    small_heavy = """\
    HEAVY:
      - { backend: openai_compatible, model_id: local-heavy, effort: high, price: free,
          base_url: 'http://localhost:8001/v1',
          capabilities_override: { max_context: 32768 } }
"""
    models = MODELS_YAML.replace(HEAVY_TARGET, small_heavy)
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, models=models))
    message = str(excinfo.value)
    assert "HEAVY" in message and "32768" in message and "100000" in message


# --------------------------------------------------------------------------------------
# secrets: §9 rule 4 — the file names the VARIABLE, the environment holds the value
# --------------------------------------------------------------------------------------


def test_a_secret_never_reaches_the_digest_the_repr_or_a_dump(tmp_path: Path) -> None:
    """The three ways a key escapes: a digest that is logged as run identity, a `repr()` in a
    traceback, and a `model_dump()` of a settings object handed to structlog. §9 accepts only
    `api_key_env` — a variable NAME — so none of the three may ever carry the value.
    """
    planted = "sk-ant-" + "A" * 40
    settings = load(write_config(tmp_path), env={"ANTHROPIC_API_KEY": planted})

    # It resolved: this is a real secret in a real registry, not an absent one trivially passing.
    assert settings.secrets.require("ANTHROPIC_API_KEY").get_secret_value() == planted
    assert settings.secrets.names() == ("ANTHROPIC_API_KEY",)

    digest_input = canonical_json(
        {
            "config": settings.config.model_dump(mode="json"),
            "models": settings.models.model_dump(mode="json"),
            "digests": dict(settings.section_digests),
        }
    )
    assert planted not in digest_input
    assert "ANTHROPIC_API_KEY" in digest_input          # the NAME is config and stays

    assert planted not in repr(settings)
    assert planted not in repr(settings.secrets)
    assert planted not in settings.config.model_dump_json()
    assert planted not in json.dumps(settings.models.model_dump(mode="json"))


def test_a_secret_written_into_a_config_file_is_refused_without_being_echoed(
    tmp_path: Path,
) -> None:
    """§9 rule 4 / §11.4: a value matching a `redaction.patterns` entry is refused at startup. The
    error may not quote the match — reporting a leak by printing it writes the key into the log the
    redactor exists to protect."""
    planted = "sk-ant-" + "B" * 40
    models = MODELS_YAML.replace("api_key_env: ANTHROPIC_API_KEY", f"api_key_env: {planted}")
    with pytest.raises(SecretInConfigError) as excinfo:
        load(write_config(tmp_path, models=models))

    message = str(excinfo.value)
    assert "models.yaml" in message
    assert "anthropic" in message                       # the pattern's NAME
    assert planted not in message
    assert "B" * 40 not in message


def test_redaction_cannot_be_disabled_without_the_documented_escape_hatch(tmp_path: Path) -> None:
    """§9: "setting this false is refused at startup unless FLEET_ALLOW_RAW=1". The switch exists;
    flipping it by accident in a YAML file must not be enough."""
    config = write_config(tmp_path, fleet="redaction:\n  enabled: false\n")
    with pytest.raises(ConfigValidationError) as excinfo:
        load(config)
    assert "FLEET_ALLOW_RAW" in str(excinfo.value)

    allowed = load(config, env={"FLEET_ALLOW_RAW": "1"})
    assert allowed.config.redaction.enabled is False


def test_missing_api_key_env_fails_at_use_naming_the_variable(tmp_path: Path) -> None:
    """A local server's dummy key may legitimately be unset, so load succeeds; the failure lands
    where it is actionable, naming the variable to export rather than "authentication failed"."""
    settings = load(write_config(tmp_path))
    with pytest.raises(RuntimeError) as excinfo:
        settings.secrets.require("ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# odds and ends the loader is the only place to catch
# --------------------------------------------------------------------------------------


def test_ladder_length_must_equal_max_attempts(tmp_path: Path) -> None:
    """§9: "Length must equal max_attempts". A four-rung ladder under `max_attempts: 3` means the
    escalation rung is never reached, which no error would otherwise reveal."""
    fleet = """\
        transform:
          max_attempts: 2
    """
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, fleet=fleet))
    assert "max_attempts" in str(excinfo.value)


def _five_rungs(*, first_bare: bool = True) -> tuple[LadderRung, ...]:
    """A genuinely 5-rung, correctly-shaped ladder — `first_bare=False` corrupts rung 0 only."""
    first = (
        LadderRung(tier=TransformTier.DETERMINISTIC)
        if first_bare
        else LadderRung(
            tier=TransformTier.DETERMINISTIC,
            role="transform_repair",
            context_policy=ContextPolicy.EVIDENCE_ONLY,
        )
    )
    return (
        first,
        LadderRung(
            tier=TransformTier.LLM_REPAIR,
            role="transform_repair",
            context_policy=ContextPolicy.EVIDENCE_ONLY,
        ),
        LadderRung(
            tier=TransformTier.LLM_REPAIR,
            role="transform_repair",
            context_policy=ContextPolicy.EVIDENCE_ONLY,
        ),
        LadderRung(
            tier=TransformTier.LLM_ESCALATION,
            role="escalation",
            context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        ),
        LadderRung(
            tier=TransformTier.LLM_ESCALATION,
            role="escalation",
            context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        ),
    )


def test_transform_section_ladder_length_mismatch_is_refused_at_construction() -> None:
    """§12.13's 5-rung variant: `TransformSection._ladder_matches_attempts` is a
    `@model_validator(mode="after")` that has never been exercised by constructing the `Section`
    directly (only indirectly, through the loader, in
    `test_ladder_length_must_equal_max_attempts` above). A 4-rung ladder under `max_attempts=5`
    must be refused at construction, naming the mismatch."""
    four_rungs = _five_rungs()[:4]
    with pytest.raises(ValidationError, match="4 rungs but max_attempts is 5"):
        TransformSection(max_attempts=5, ladder=four_rungs)

    six_rungs = (
        *_five_rungs(),
        LadderRung(
            tier=TransformTier.LLM_ESCALATION,
            role="escalation",
            context_policy=ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
        ),
    )
    with pytest.raises(ValidationError, match="6 rungs but max_attempts is 5"):
        TransformSection(max_attempts=5, ladder=six_rungs)


def test_transform_section_first_rung_must_be_bare_deterministic() -> None:
    """§9: rung 0 is the deterministic rewrite pass and takes no `role`/`context_policy` — a
    caller who gives it one has smuggled an LLM call into the attempt the ladder promises is
    free and offline."""
    corrupted = _five_rungs(first_bare=False)
    with pytest.raises(ValidationError, match="deterministic"):
        TransformSection(max_attempts=5, ladder=corrupted)


def test_transform_section_five_rung_ladder_constructs_cleanly() -> None:
    """The positive case: a genuinely 5-rung, correctly-shaped ladder under `max_attempts=5` must
    NOT raise — this is what round AA's e2e test below configures."""
    section = TransformSection(max_attempts=5, ladder=_five_rungs())
    assert len(section.ladder) == 5
    assert section.max_attempts == 5


def test_monorepo_dir_overrides_must_be_a_bijection(tmp_path: Path) -> None:
    """§9: validated "with the same bijection check as the registry itself" — two ecosystems in one
    directory silently merge two languages' trees, and no later stage can un-merge them."""
    fleet = """\
        build:
          monorepo_dir_overrides: { npm: js, pypi: js }
    """
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, fleet=fleet))
    assert "bijection" in str(excinfo.value)


def test_missing_config_file_names_the_path(tmp_path: Path) -> None:
    """Rule 11: "config/fleet.yaml: file not found", not a bare FileNotFoundError traceback."""
    with pytest.raises(ConfigFileError, match="file not found") as excinfo:
        load(tmp_path / "config")
    assert "fleet.yaml" in str(excinfo.value)


def test_memory_commitment_is_reported_not_silently_enforced(tmp_path: Path) -> None:
    """§9's `max_host_rss_mb` states a startup refusal its OWN defaults breach
    (4 × 8 GiB + 4 GiB = 36 GiB vs `max_host_rss_mb: 12288`), so the arithmetic is exposed for the
    caller that knows the host's MemTotal rather than making the shipped config unloadable."""
    settings = load(write_config(tmp_path))
    assert settings.memory_commitment_mb() == 4 * 8192 + 4096
    with pytest.raises(RuntimeError, match="max_host_rss_mb"):
        settings.validate_memory_budget()


def test_price_helper_matches_the_11_2_formula() -> None:
    """`price(target, n)` is "exactly `(in_per_mtok * n_in + out_per_mtok * n_out) / 1e6`" (§11.2);
    a ledger that rounds differently from the ceiling check halts on the wrong call."""
    target = BackendTarget(
        backend="anthropic",
        model_id="claude-opus-5",
        price=Price(in_per_mtok=5.0, out_per_mtok=25.0),
    )
    assert target_price_usd(target, in_tokens=200_000, out_tokens=40_000) == pytest.approx(
        (5.0 * 200_000 + 25.0 * 40_000) / 1e6
    )


# --------------------------------------------------------------------------------------
# B1 (round `pilot-criteria-bringup`) — `llm.harmony_vocab_dir`: offline Harmony vocab (§12.50)
# --------------------------------------------------------------------------------------


def _clear_tiktoken_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKTOKEN_ENCODINGS_BASE", raising=False)
    monkeypatch.delenv("TIKTOKEN_RS_CACHE_DIR", raising=False)


def test_harmony_vocab_dir_unset_touches_neither_tiktoken_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (`None`) must be a true no-op: an operator who never touches this key gets the
    SDK's own default (network-fetched, cached) behaviour, unchanged."""
    _clear_tiktoken_env(monkeypatch)
    load(write_config(tmp_path))
    assert "TIKTOKEN_ENCODINGS_BASE" not in os.environ
    assert "TIKTOKEN_RS_CACHE_DIR" not in os.environ


def test_harmony_vocab_dir_points_tiktoken_env_vars_at_the_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.50 offline-vocab half: the HARNESS sets `TIKTOKEN_ENCODINGS_BASE`/`TIKTOKEN_RS_CACHE_DIR`
    itself from `llm.harmony_vocab_dir` — an operator never exports either into their shell."""
    _clear_tiktoken_env(monkeypatch)
    vocab_dir = tmp_path / "vocab"
    vocab_dir.mkdir()
    (vocab_dir / "o200k_base.tiktoken").write_text("stub vocab content\n", encoding="utf-8")
    fleet = f"""\
        llm:
          harmony_vocab_dir: {vocab_dir}
    """
    load(write_config(tmp_path, fleet=fleet))
    assert os.environ["TIKTOKEN_ENCODINGS_BASE"] == str(vocab_dir)
    assert os.environ["TIKTOKEN_RS_CACHE_DIR"] == str(vocab_dir)


def test_harmony_vocab_dir_missing_directory_fails_loud_at_settings_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 11: a missing vocab dir is `ConfigValidationError` (exit 2) AT SETTINGS LOAD, naming the
    path — never a lazy `HarmonyError` surfacing at the backend's first `load_harmony_encoding()`
    call, waves into a run."""
    _clear_tiktoken_env(monkeypatch)
    missing = tmp_path / "does-not-exist"
    fleet = f"""\
        llm:
          harmony_vocab_dir: {missing}
    """
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, fleet=fleet))
    assert str(missing) in str(excinfo.value)
    assert "TIKTOKEN_ENCODINGS_BASE" not in os.environ, "never set on a failed validation"


def test_harmony_vocab_dir_missing_vocab_file_fails_loud_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory existing is not enough: the message names both the configured path and the
    specific missing file, per this round's brief."""
    _clear_tiktoken_env(monkeypatch)
    vocab_dir = tmp_path / "vocab"
    vocab_dir.mkdir()
    fleet = f"""\
        llm:
          harmony_vocab_dir: {vocab_dir}
    """
    with pytest.raises(ConfigValidationError) as excinfo:
        load(write_config(tmp_path, fleet=fleet))
    message = str(excinfo.value)
    assert str(vocab_dir) in message
    assert "o200k_base.tiktoken" in message
    assert "TIKTOKEN_ENCODINGS_BASE" not in os.environ


# --------------------------------------------------------------------------------------
# B2 (round `pilot-criteria-bringup`) — `config/models.local.yaml`: untracked endpoint overrides
# --------------------------------------------------------------------------------------


def test_models_local_yaml_absent_falls_back_to_the_template_value(tmp_path: Path) -> None:
    """No override file (the common case — most clones never create one): the committed
    template's own values resolve, unchanged."""
    settings = load(write_config(tmp_path))
    target = settings.models.profiles["default"]["HEAVY"][0]
    assert target.model_id == "claude-opus-5"


def test_models_local_yaml_overlays_a_field_onto_the_template_target(tmp_path: Path) -> None:
    """The override supplies only the field it wants to change; every other field of that same
    target — `api_key_env`, `price`, `effort` — still comes from the committed template."""
    config_dir = write_config(tmp_path)
    (config_dir / "models.local.yaml").write_text(
        "profiles:\n  default:\n    HEAVY:\n      - { model_id: real-heavy-model }\n",
        encoding="utf-8",
    )
    settings = load(config_dir)
    target = settings.models.profiles["default"]["HEAVY"][0]
    assert target.model_id == "real-heavy-model"
    assert target.api_key_env == "ANTHROPIC_API_KEY"
    assert target.price == Price(in_per_mtok=5.0, out_per_mtok=25.0)


def test_models_local_yaml_naming_an_unknown_profile_fails_loud(tmp_path: Path) -> None:
    """A typo'd profile name must not silently mean "no override applied" (Rule 11)."""
    config_dir = write_config(tmp_path)
    (config_dir / "models.local.yaml").write_text(
        "profiles:\n  nope:\n    HEAVY:\n      - { model_id: x }\n", encoding="utf-8"
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load(config_dir)
    assert "nope" in str(excinfo.value)


def test_models_local_yaml_naming_an_out_of_range_index_fails_loud(tmp_path: Path) -> None:
    """`profiles.default.HEAVY` has exactly one target in the fixture template; an override naming
    index 1 names a target the template does not have."""
    config_dir = write_config(tmp_path)
    (config_dir / "models.local.yaml").write_text(
        "profiles:\n  default:\n    HEAVY:\n"
        "      - { model_id: first }\n      - { model_id: second }\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load(config_dir)
    assert "index 1" in str(excinfo.value)


def test_models_local_yaml_is_covered_by_the_secret_material_scan(tmp_path: Path) -> None:
    """§9 rule 4 applies to this file too, not only to the committed three — an override file is
    still config an operator could accidentally paste a real key into."""
    config_dir = write_config(tmp_path)
    (config_dir / "models.local.yaml").write_text(
        "profiles:\n  default:\n    HEAVY:\n"
        "      - { api_key_env: sk-ant-api03-" + "A" * 40 + " }\n",
        encoding="utf-8",
    )
    with pytest.raises(SecretInConfigError):
        load(config_dir)

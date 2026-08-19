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
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, Price
from fleet.settings import (
    CONFIG_SECTIONS,
    MODELS_SECTION,
    BuildSection,
    ConfigFileError,
    ConfigValidationError,
    FleetSettings,
    SecretInConfigError,
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
      - { backend: anthropic, model_id: claude-haiku-4-5, effort: low,
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
    assert excinfo.value.exit_code == 2
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
    assert excinfo.value.exit_code == 2


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
    assert excinfo.value.exit_code == 2


def test_a_usable_base_url_still_loads(tmp_path: Path) -> None:
    """The other half of the blank-`base_url` guard: the check must reject empty strings without
    rejecting real ones, or every local profile stops booting."""
    models = MODELS_YAML.replace(CHEAP_TARGET, _cheap_openai_target("http://localhost:8001/v1"))
    settings = load(write_config(tmp_path, models=models))
    assert settings.targets_for_tier(ModelTier.CHEAP)[0].base_url == "http://localhost:8001/v1"


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
    assert excinfo.value.exit_code == 2


def test_a_typod_backend_is_not_reported_as_a_missing_extra(tmp_path: Path) -> None:
    """The other half: `anthropik` is a typo, not an uninstalled SDK. Telling the operator to
    `pip install` it would send them after a package that does not exist."""
    models = MODELS_YAML.replace(
        "backend: anthropic, model_id: claude-opus-5",
        "backend: anthropik, model_id: claude-opus-5",
    )
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path, models=models))
    assert "pip install" not in str(excinfo.value)


def test_a_role_routed_to_an_empty_tier_is_a_startup_error(tmp_path: Path) -> None:
    """§9 rule 1, verbatim: "A role routed to an empty tier is a startup error, never a runtime
    `KeyError` in wave 7"."""
    models = MODELS_YAML.replace(CHEAP_TARGET, "    CHEAP: []\n")
    with pytest.raises(UnresolvedReferenceError) as excinfo:
        load(write_config(tmp_path, models=models))
    assert "repo_classify" in str(excinfo.value) and "CHEAP" in str(excinfo.value)


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

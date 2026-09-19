"""test_config_writer.py — S8: ``resolve.write_profile_config_keys`` (§9.3/§13).

Three REAL shapes (F2), vendored as pristine committed copies under
``tests/data/fixtures/`` and copied into a tmp root at staging time — the
writer only ever touches the copy (coordinator rule: never write into the
live ``config/`` tree):

* ``agency-orchestrator``      — the 5-line pack fragment (no ``terminal:``
  and no ``plugins:`` section: writes into it must CREATE those sections);
* ``agency-backend-engineer``  — the full 2 167-line commented template
  (its ``terminal.cwd`` line — 367 — holds the sentinel ``"."``);
* a freshly instantiated profile with **no config.yaml at all** (create path).

Wherever the Verify table says "byte-identical", we diff; where it says
"exactly one write", we count the atomic primitive.

S10a (plan deviation 8) extends the SAME matrix to the three keys the writer
picked up: ``skills.disabled`` (a DENY list — there is no ``skills.enabled``
key in the substrate), ``model`` and ``provider`` (the model-override pair).
The bottom section of this file is that extension; the guards above it are
re-run against the new keys rather than rewritten.
"""

from __future__ import annotations

import difflib
import importlib
import importlib.util
import logging
import os
from pathlib import Path

import pytest

import hermes_cli.config
import hermes_constants
import utils
import yaml

import resolve
from errors import (E_TEAM_INVALID, E_TEAM_MANAGED, E_TEAM_NO_PROFILE,
                    TeamError)

# ── fixture sources: VENDORED pristine copies (committed test data) ──────────
#
# These used to be read live out of the deployment tree
# (``<repo>/config/profiles/agency-{orchestrator,backend-engineer}``), which
# made the suite depend on the deployment's reconcile state — once the S11
# live gate reconciled those profiles, the "pristine shape" assertions could
# never pass again. The pristine pre-reconcile bytes are byte-identical to
# the guard-3 backups the reconciler left behind
# (``config.yaml.hermes-agent-synergia.bak``), and those two files are vendored here:
#
#   agency-orchestrator   -> fragment/config.yaml   (pristine 63 B, 5 lines)
#   agency-backend-engineer -> full/config.yaml     (pristine 114357 B)
#
# The live ``config/`` tree is NOT touched by the suite (or by these copies).

_FIXTURES = Path(__file__).resolve().parent / "data" / "fixtures"
_FRAGMENT = _FIXTURES / "fragment" / "config.yaml"
_FULL = _FIXTURES / "full" / "config.yaml"

requires_fixtures = pytest.mark.skipif(
    not (_FRAGMENT.is_file() and _FULL.is_file()),
    reason=f"vendored config fixtures absent under {_FIXTURES}",
)

# S10a's skill-read seam is ``hermes_cli.web_server_profiles._profile_scope``
# (the substrate's own profile-scoped skill read — there is no public one), and
# that module imports fastapi at module scope. The seam tests are therefore
# environment-gated, NOT deleted: the arithmetic they protect is additionally
# covered unconditionally by the monkeypatched complement test at the bottom of
# this file.
requires_fastapi = pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None,
    reason="the profile-scoped skill seam lives in hermes_cli.web_server_profiles, "
           "which imports fastapi — absent from .venv-team and from the stack's "
           "python3 (S10a finding); the complement arithmetic is covered without it",
)


@pytest.fixture
def stage(profile):
    """Factory: ``stage(name, src=...)`` copies the bytes of *src* into
    ``profiles/<name>/config.yaml`` under the tmp root. ``src=None`` leaves a
    freshly instantiated home with **no config.yaml at all** (the create
    path). Returns the config path."""
    def make(name: str, src=None) -> Path:
        home = profile(name)                      # creates home + stub config
        path = home / "config.yaml"
        if src is None:
            path.unlink()
        else:
            path.write_bytes(Path(src).read_bytes())
        return path
    return make


# ── diff helpers ─────────────────────────────────────────────────────────────

def changed_regions(old_lines: list, new_lines: list) -> list:
    """Non-equal opcode regions of a line diff — every equal span is proof
    of byte identity of the untouched lines."""
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    return [op for op in sm.get_opcodes() if op[0] != "equal"]


def lines(text: str) -> list:
    return text.splitlines(keepends=True)


def cfg_of(home: Path) -> Path:
    return home / "config.yaml"


# ── the three real shapes (F2): one write, one hunk, rest byte-identical ───

@requires_fixtures
def test_fragment_append_terminal_section(stage):
    """5-line fragment + one new key: original five lines survive
    byte-identical, ONE hunk at EOF creates the missing ``terminal:``."""
    frag = stage("agency-orchestrator", src=_FRAGMENT)
    original = frag.read_bytes()
    resolve.write_profile_config_keys("agency-orchestrator",
                                      {"terminal.cwd": "/root/workspace/proj"})
    after = frag.read_bytes()
    ops = changed_regions(lines(original.decode("utf-8")), lines(after.decode("utf-8")))
    assert len(ops) == 1, f"expected exactly one hunk, got {ops}"
    tag, a0, a1 = ops[0][0], ops[0][1], ops[0][2]
    assert tag == "insert" and a0 == a1, \
        f"the fragment itself must be untouched (pure append), got {ops[0]}"
    assert after[:len(original)] == original          # byte-identical prefix
    parsed = yaml.safe_load(after.decode("utf-8"))
    assert parsed["toolsets"] == ["kanban", "clarify", "memory", "session_search"]
    assert parsed["terminal"] == {"cwd": "/root/workspace/proj"}


@requires_fixtures
def test_fragment_replace_toolsets_one_hunk(stage):
    """The same file, the key it HAS: replaced in place, parses back exactly."""
    frag = stage("agency-orchestrator", src=_FRAGMENT)
    resolve.write_profile_config_keys("agency-orchestrator",
                                      {"toolsets": ["kanban", "team"]})
    parsed = yaml.safe_load(frag.read_text(encoding="utf-8"))
    assert parsed == {"toolsets": ["kanban", "team"]}


@requires_fixtures
def test_full_template_cwd_single_line_change(stage):
    """The 2 167-line commented template: rewriting the sentinel cwd is the
    ONLY line change; every comment and sibling key survives byte-for-byte."""
    cfg = stage("agency-backend-engineer", src=_FULL)
    original_bytes = cfg.read_bytes()
    original = original_bytes.decode("utf-8")
    before = original.splitlines(keepends=True)
    assert before[366].lstrip().startswith('cwd:'), \
        "fixture drift: template line 367 is no longer terminal.cwd"
    resolve.write_profile_config_keys("agency-backend-engineer",
                                      {"terminal.cwd": "/root/workspace/proj"})
    after = cfg.read_text(encoding="utf-8")
    ops = changed_regions(before, lines(after))
    assert len(ops) == 1, f"expected one hunk, got {ops}"
    tag, i1, i2, j1, j2 = ops[0]
    assert tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1, \
        f"expected a single-LINE replacement at 367, got {ops[0]}"
    assert i1 == 366, f"expected the cwd line (index 366), got {i1}"
    assert after.splitlines()[366].strip().startswith('cwd:'), \
        "the change must land on the cwd line"
    assert len(after.splitlines()) == len(before), "line count must not move"
    # Comments around it survive byte-identically (the diff already proves
    # this for every other line — assert the specific neighbours explicitly):
    assert original.splitlines()[365] == after.splitlines()[365]   # 'terminal:'
    assert original.splitlines()[368] == after.splitlines()[368]   # next key line
    parsed = yaml.safe_load(after)
    assert parsed["terminal"]["cwd"] == "/root/workspace/proj"
    # The backup holds the pristine original (guard 3).
    bak = cfg.with_name("config.yaml.hermes-agent-synergia.bak")
    assert bak.read_bytes() == original_bytes


@requires_fixtures
def test_full_template_creates_plugins_section(stage):
    """The template has NO ``plugins:`` section — the write must create it
    (F2), appended as one hunk, template body untouched."""
    cfg = stage("agency-backend-engineer", src=_FULL)
    original = cfg.read_bytes()
    assert "\nplugins:" not in original.decode("utf-8")
    resolve.write_profile_config_keys("agency-backend-engineer",
                                      {"plugins.enabled": ["team"]})
    after = cfg.read_bytes()
    assert after[:len(original)] == original        # byte-identical prefix
    assert len(changed_regions(lines(original.decode("utf-8")),
                               lines(after.decode("utf-8")))) == 1
    assert yaml.safe_load(after.decode("utf-8"))["plugins"] == {"enabled": ["team"]}


def test_fresh_profile_no_config_creates_file(stage):
    """Shape 3: home exists, no config.yaml — the write CREATES the file."""
    cfg = stage("brand-new", src=None)
    assert not cfg.exists()
    resolve.write_profile_config_keys("brand-new",
                                      {"terminal.cwd": "/root/team-x/repo",
                                       "toolsets": ["kanban", "team"]})
    assert cfg.is_file()
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == {
        "terminal": {"cwd": "/root/team-x/repo"},
        "toolsets": ["kanban", "team"],
    }
    # Nothing was "modified", so nothing was backed up (create path).
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


# ── block sequences whose items sit at the KEY's own indent (safe_dump) ─────

@pytest.mark.parametrize("existing", [
    # what yaml.safe_dump writes: the dash at the parent key's column
    "plugins:\n  enabled:\n  - other-plugin\n",
    # what utils.atomic_yaml_write writes here: the dash indented under it
    "plugins:\n  enabled:\n    - other-plugin\n",
    # flow style, for good measure
    "plugins:\n  enabled: [other-plugin]\n",
])
def test_nested_list_replacement_consumes_the_old_items(profile, existing):
    """Regression: ``_value_end`` only recognised same-indent ``- `` items at
    column 0, so replacing a NESTED list left the old items orphaned below the
    new ones — invalid YAML the reparse guard then refused, making the write
    impossible for any config the substrate itself had written."""
    home = profile("p", existing)
    resolve.write_profile_config_keys(
        "p", {"plugins.enabled": ["other-plugin", "hermes-agent-synergia"]})
    text = cfg_of(home).read_text()
    assert yaml.safe_load(text)["plugins"]["enabled"] == [
        "other-plugin", "hermes-agent-synergia"]
    assert text.count("other-plugin") == 1          # no orphaned leftovers


def test_top_level_list_replacement_consumes_the_old_items(profile):
    """The same shape one level up (``toolsets:`` written by safe_dump)."""
    home = profile("p", "toolsets:\n- a\n- b\nmodel: gpt\n")
    resolve.write_profile_config_keys("p", {"toolsets": ["a"]})
    parsed = yaml.safe_load(cfg_of(home).read_text())
    assert parsed["toolsets"] == ["a"]
    assert parsed["model"] == "gpt"


def test_unparseable_surgical_result_fails_closed_with_our_code(profile,
                                                                monkeypatch):
    """A surgical edit that would not re-parse never reaches the disk, and the
    caller gets ``E_TEAM_INVALID`` — not a raw yaml.parser traceback."""
    home = profile("p", {"model": "gpt"})
    before = cfg_of(home).read_bytes()
    monkeypatch.setattr(resolve, "_render_key",
                        lambda key, value, indent: ["  : : broken\n"])
    with pytest.raises(TeamError) as ei:
        resolve.write_profile_config_keys("p", {"toolsets": ["a"]})
    assert ei.value.code == E_TEAM_INVALID
    assert cfg_of(home).read_bytes() == before


# ── guard 4: allow-list ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    # S8's refusals. ``model`` LEFT this list in S10a (deviation 8), and
    # ``model.default``/``model.provider``/``model.base_url`` left it once the
    # pin moved into the model section; every other model sub-key stays out.
    "model.api_key", "terminal.backend", "kanban",
    "kanban.tenant", "agent.browser", "terminal.cwd.child",
    "plugins", "plugins.enabled.deeper", "terminal.cwdx", "",
    # S10a's refusals: the new keys are three EXACT dotted names.
    "skills", "skills.enabled", "skills.platform_disabled",
    "skills.disabled.deeper", "providers", "provider.name", "model_1",
])
def test_key_outside_allow_list_raises(profile, bad):
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {bad: "nope"})
    assert excinfo.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


def test_batch_with_bad_key_writes_nothing(profile):
    """All-or-nothing: one illegal key voids the whole dict-of-updates call."""
    home = profile("p", {"terminal": {"cwd": "/keep/me"}})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {
            "terminal.cwd": "/root/ok",      # legal...
            "model.api_key": "sk-9",         # ...and one that is not
        })
    assert excinfo.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before


def test_non_list_toolsets_refused(profile):
    home = profile("p")
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    for bad in ("kanban", ["kanban", 7], [""]):
        with pytest.raises(TeamError) as excinfo:
            resolve.write_profile_config_keys("p", {"toolsets": bad})
        assert excinfo.value.code == E_TEAM_INVALID
        assert cfg.read_bytes() == before


# ── guard 5: terminal.cwd sentinels and relative paths ──────────────────────

@pytest.mark.parametrize("sentinel", ["", ".", "./", "auto", "cwd", " . "])
def test_sentinel_cwd_refused(profile, sentinel):
    home = profile("p", {"terminal": {"cwd": "/keep/me"}})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {"terminal.cwd": sentinel})
    assert excinfo.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before


def test_relative_cwd_refused(profile):
    home = profile("p")
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {"terminal.cwd": "relative/path"})
    assert excinfo.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before


# ── guard 1: managed install ────────────────────────────────────────────────

def test_is_managed_refuses_writes(profile, monkeypatch):
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    monkeypatch.setattr(hermes_cli.config, "is_managed", lambda: True,
                        raising=True)
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {"toolsets": ["team"]})
    assert excinfo.value.code == E_TEAM_MANAGED
    assert cfg.read_bytes() == before
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


def test_managed_check_precedes_key_validation(profile, monkeypatch):
    """Guard ORDER: a managed install refuses with E_TEAM_MANAGED even
    when the update dict is also illegal."""
    profile("p")
    monkeypatch.setattr(hermes_cli.config, "is_managed", lambda: True,
                        raising=True)
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {"model.api_key": "x"})
    assert excinfo.value.code == E_TEAM_MANAGED


# ── guard 2: fail closed on unreadable / corrupt targets ───────────────────

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode 000")
def test_unreadable_target_refuses_writes_nothing(profile):
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    mode = cfg.stat().st_mode
    try:
        cfg.chmod(0o000)
        with pytest.raises(TeamError) as excinfo:
            resolve.write_profile_config_keys("p", {"toolsets": ["team"]})
    finally:
        cfg.chmod(mode)
    assert excinfo.value.code == E_TEAM_INVALID
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == {"toolsets": ["kanban"]}
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


def test_corrupt_target_refuses(profile, monkeypatch):
    """require_readable_config_before_write is honoured, not bypassed: a
    RuntimeError refusal maps to E_TEAM_INVALID and nothing is touched."""
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    def refuse(config_path=None):
        raise RuntimeError("Refusing to overwrite: top-level YAML must be a mapping")
    monkeypatch.setattr(hermes_cli.config, "require_readable_config_before_write",
                        refuse, raising=True)
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("p", {"toolsets": ["team"]})
    assert excinfo.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before


# ── §9.3 refusals: tombstoned / missing profile homes ──────────────────────

def test_tombstoned_profile_refused(profile):
    home = profile("gone", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    hermes_constants.mark_named_profile_deleted(home)
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("gone", {"toolsets": ["team"]})
    assert excinfo.value.code == E_TEAM_NO_PROFILE
    assert cfg.read_bytes() == before


def test_missing_profile_home_refused(profile):
    home = profile("gone", {"toolsets": ["kanban"]})
    cfg_of(home).unlink()
    home.rmdir()
    with pytest.raises(TeamError) as excinfo:
        resolve.write_profile_config_keys("gone", {"toolsets": ["team"]})
    assert excinfo.value.code == E_TEAM_NO_PROFILE


# ── §9.3 per-file batching: ONE atomic replace per file per run ────────────

@pytest.fixture
def write_counter(monkeypatch):
    """Patch the substrate's atomic-replace primitive with a delegating
    counter. The step names ``utils.atomic_yaml_write``; the writer commits
    through ``utils.atomic_write_text`` instead — the same temp+fsync+rename
    guarantee without re-serialising the document (see the deviation note in
    resolve.py's S8 banner: a yaml.dump of the 2 167-line commented template
    would destroy every comment and make 'byte-identical' impossible). The
    Verify bullet's intent — count the real writes — is preserved exactly."""
    calls = []
    real = utils.atomic_write_text
    def counting(path, content, **kw):
        calls.append(str(path))
        return real(path, content, **kw)
    monkeypatch.setattr(utils, "atomic_write_text", counting, raising=True)
    return calls


def test_three_keys_one_file_exactly_one_write(profile, write_counter):
    home = profile("p", {
        "terminal": {"backend": "local", "cwd": "."},
        "toolsets": ["kanban"],
        "kanban": {"max_in_progress_per_profile": 1},
    })
    cfg = cfg_of(home)
    resolve.write_profile_config_keys("p", {
        "terminal.cwd": "/root/w",
        "toolsets": ["kanban", "team"],
        "kanban.max_in_progress_per_profile": 2,
    })
    assert len(write_counter) == 1, f"expected ONE write, got {write_counter}"
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["terminal"]["cwd"] == "/root/w"
    assert parsed["terminal"]["backend"] == "local"      # sibling survived
    assert parsed["toolsets"] == ["kanban", "team"]
    assert parsed["kanban"]["max_in_progress_per_profile"] == 2
    # The one write happened AFTER the backup (guard 3 precedes mutation).
    assert cfg.with_name("config.yaml.hermes-agent-synergia.bak").is_file()


def test_idempotent_rerun_writes_nothing(profile, write_counter):
    """Already-current keys: zero writes, zero mtime change, no .bak (§9.3)."""
    home = profile("p", {"terminal": {"cwd": "/root/w"}})
    cfg = cfg_of(home)
    stat = cfg.stat()
    resolve.write_profile_config_keys("p", {"terminal.cwd": "/root/w"})
    assert write_counter == [], "already-current keys must not be rewritten"
    after = cfg.stat()
    assert (after.st_mtime_ns, after.st_size) == (stat.st_mtime_ns, stat.st_size)
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


# ── guard 6: the pipeline runs under the profile-home override ────────────

def test_write_uses_home_override_and_resets(profile, monkeypatch):
    home = profile("p", {"toolsets": ["kanban"]})
    seen = []
    real_set = hermes_constants.set_hermes_home_override
    real_reset = hermes_constants.reset_hermes_home_override
    def spy_set(path):
        seen.append(Path(path))
        return real_set(path)
    def spy_reset(token):
        seen.append("reset")
        return real_reset(token)
    monkeypatch.setattr(hermes_constants, "set_hermes_home_override", spy_set,
                        raising=True)
    monkeypatch.setattr(hermes_constants, "reset_hermes_home_override", spy_reset,
                        raising=True)
    resolve.write_profile_config_keys("p", {"toolsets": ["team"]})
    assert seen[0] == home, "the override must be the PROFILE home"
    assert seen[-1] == "reset", "the token must always be reset"
    assert yaml.safe_load(cfg_of(home).read_text(encoding="utf-8"))["toolsets"] == ["team"]


# ── §13 audit: applied changes logged at INFO, refusals not "applied" ─────

def test_applied_write_is_audited(profile, caplog):
    profile("p", {"toolsets": ["kanban"]})
    with caplog.at_level(logging.INFO, logger="hermes_agent_synergia"):
        resolve.write_profile_config_keys("p", {"toolsets": ["kanban", "team"]})
    lines_seen = [r.getMessage() for r in caplog.records if "hermes-agent-synergia" in r.getMessage()]
    assert any("applied to 'p'" in m and "toolsets" in m for m in lines_seen), lines_seen


def test_noop_is_not_logged_as_applied(profile, caplog):
    profile("p", {"terminal": {"cwd": "/root/w"}})
    with caplog.at_level(logging.INFO, logger="hermes_agent_synergia"):
        resolve.write_profile_config_keys("p", {"terminal.cwd": "/root/w"})
    assert not [r for r in caplog.records if "applied to" in r.getMessage()]


def test_refusal_is_not_audited_as_applied(profile, monkeypatch, caplog):
    profile("p")
    monkeypatch.setattr(hermes_cli.config, "is_managed", lambda: True,
                        raising=True)
    with caplog.at_level(logging.INFO, logger="hermes_agent_synergia"):
        with pytest.raises(TeamError):
            resolve.write_profile_config_keys("p", {"toolsets": ["team"]})
    assert not [r for r in caplog.records if "applied to" in r.getMessage()]


# ── guard 3: backup semantics ──────────────────────────────────────────────

def test_backup_precedes_change_and_keeps_pristine(profile):
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    original = cfg.read_bytes()
    resolve.write_profile_config_keys("p", {"toolsets": ["kanban", "team"]})
    bak = cfg.with_name("config.yaml.hermes-agent-synergia.bak")
    assert bak.read_bytes() == original
    # A second change round must not clobber the pristine backup.
    resolve.write_profile_config_keys("p", {"toolsets": ["kanban", "team", "memory"]})
    assert bak.read_bytes() == original
    assert "memory" in cfg.read_text(encoding="utf-8")


# ── S10a (plan deviation 8): skills.disabled / model / provider ─────────────
#
# The three keys reconcile.apply (S10) projects ``Member.skills`` /
# ``Member.model`` / ``Member.provider`` onto. The point of this section is
# that the S8 matrix is RE-RUN against the new keys instead of trusted to
# "generalise": same allow-list refusal, same all-or-nothing, same
# byte-identical template body, same one-write batching, same backup rule.
#
# Shape notes the tests pin down:
#   * ``skills.disabled`` is a DENY list — the substrate has no
#     ``skills.enabled`` key anywhere, so "member gets X and Y" is stored as
#     everything else (S10a Do 3);
#   * subtracting ``ESSENTIAL_SKILLS`` can empty that list, and an empty list
#     must render as ``[]`` rather than a bare ``disabled:`` that reparses to
#     ``None`` (the reparse guard would refuse the write outright);
#   * ``model``/``provider`` are the model-override PAIR, and on the full
#     template ``model:`` is a commented MAPPING section — pinning a scalar
#     replaces that whole block, key for key, which is exactly what "touches
#     only the keys it sets" means for a scalar write to a mapping key.

def essential_skills() -> list:
    """``agent.skill_utils.ESSENTIAL_SKILLS`` — the point of the test is that
    the WRITER enforces the veto, so the fixture is the substrate's own set."""
    from agent.skill_utils import ESSENTIAL_SKILLS
    return sorted(ESSENTIAL_SKILLS)


def untouched_text(old_lines: list, new_lines: list) -> tuple:
    """The bytes of every line OUTSIDE every hunk, before and after the write.
    Equal ⇒ every untouched line of the 2 167-line template survived byte for
    byte (the Verify bullet's "byte-compare the rest")."""
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    ops = [op for op in sm.get_opcodes() if op[0] == "equal"]
    return ("".join(seg for _, i1, i2, _, _ in ops for seg in old_lines[i1:i2]),
            "".join(seg for _, _, _, j1, j2 in ops for seg in new_lines[j1:j2]))


def test_s10a_new_keys_are_written_in_one_replace(profile, write_counter):
    """The create path for the three keys, ONE atomic replace for all three."""
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    resolve.write_profile_config_keys("p", {
        "skills.disabled": ["arxiv", "dogfood"],
        "model": "claude-opus-5",
        "provider": "anthropic",
    })
    assert len(write_counter) == 1, f"expected ONE write, got {write_counter}"
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["skills"]["disabled"] == ["arxiv", "dogfood"]
    assert parsed["model"] == "claude-opus-5"
    assert parsed["provider"] == "anthropic"
    assert parsed["toolsets"] == ["kanban"], "the sibling key must survive"


def test_s10a_rerun_with_identical_values_writes_nothing(profile, write_counter):
    """§9.3 idempotence for the new keys — including an EQUAL-but-not-IDENTICAL
    deny list (a fresh list object must still count as 'already current')."""
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    updates = {"skills.disabled": ["arxiv", "dogfood"],
               "model": "claude-opus-5", "provider": "anthropic"}
    resolve.write_profile_config_keys("p", updates)
    assert len(write_counter) == 1
    stat = cfg.stat()
    resolve.write_profile_config_keys(
        "p", {k: (list(v) if isinstance(v, list) else v) for k, v in updates.items()})
    after = cfg.stat()
    assert len(write_counter) == 1, f"the second run wrote {write_counter}"
    assert (after.st_mtime_ns, after.st_size) == (stat.st_mtime_ns, stat.st_size)


@pytest.mark.parametrize("extra", ["agent.browser", "skills.enabled",
                                   "skills", "model.api_key", "providers"])
def test_s10a_unlisted_key_mixed_with_legal_ones_writes_nothing(profile, extra):
    """All-or-nothing WITH the new keys in the batch: the refusal happens
    before any mutation, so a legal skills/model write dies with the illegal
    one instead of half-applying."""
    home = profile("p", {"terminal": {"cwd": "/keep/me"}})
    cfg = cfg_of(home)
    before, stat = cfg.read_bytes(), cfg.stat()
    with pytest.raises(TeamError) as ei:
        resolve.write_profile_config_keys("p", {
            "skills.disabled": ["arxiv"],      # legal...
            "model": "claude-opus-5",          # ...legal...
            "provider": "anthropic",           # ...legal...
            extra: "nope",                     # ...and one that is not
        })
    assert ei.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before
    after = cfg.stat()
    assert (after.st_mtime_ns, after.st_size) == (stat.st_mtime_ns, stat.st_size)
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


@pytest.mark.parametrize("updates", [
    # a bare provider: mirrors kanban_db._validate_model_override — a provider
    # with no model would re-resolve the profile's own model against another
    # backend, which is the mismatch the override pair exists to kill.
    {"provider": "anthropic"},
    # ...and the same refusal when the pair-mate is merely PRESENT IN THE FILE
    # (the file below already has model: keep-me): the pair is one write.
    {"provider": "anthropic", "skills.disabled": ["arxiv"]},
])
def test_s10a_provider_without_model_refuses(profile, updates):
    home = profile("p", {"model": "keep-me"})
    cfg = cfg_of(home)
    before, stat = cfg.read_bytes(), cfg.stat()
    with pytest.raises(TeamError) as ei:
        resolve.write_profile_config_keys("p", updates)
    assert ei.value.code == E_TEAM_INVALID
    assert "model" in str(ei.value)
    assert cfg.read_bytes() == before
    after = cfg.stat()
    assert (after.st_mtime_ns, after.st_size) == (stat.st_mtime_ns, stat.st_size)
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


def test_model_section_keys_turn_a_scalar_model_into_a_section(profile):
    home = profile("p", "plugins:\n  enabled:\n    - hermes-agent-synergia\nmodel: 'For '\n")
    cfg = cfg_of(home)
    resolve.write_profile_config_keys("p", {
        "model.default": "qwen-x", "model.provider": "custom",
        "model.base_url": "http://llm:8000/v1"})
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["model"] == {"default": "qwen-x", "provider": "custom",
                               "base_url": "http://llm:8000/v1"}
    assert parsed["plugins"] == {"enabled": ["hermes-agent-synergia"]}


def test_model_section_keys_keep_sibling_model_settings(profile):
    home = profile("p", {"model": {"default": "old", "context_length": 200000}})
    cfg = cfg_of(home)
    resolve.write_profile_config_keys("p", {"model.default": "new"})
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["model"] == {"default": "new", "context_length": 200000}


@pytest.mark.parametrize("updates", [{"model.provider": "anthropic"},
                                     {"model.base_url": "http://x/v1"}])
def test_model_section_provider_without_default_refuses(profile, updates):
    home = profile("p", {"model": {"default": "keep-me"}})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    with pytest.raises(TeamError) as ei:
        resolve.write_profile_config_keys("p", updates)
    assert ei.value.code == E_TEAM_INVALID
    assert cfg.read_bytes() == before


def test_s10a_bad_value_shapes_refused(profile):
    """Do 4: the deny list is a list of non-empty strings; the pair are
    non-empty scalars (a mapping would not round-trip, a bool would render as
    ``model: true``)."""
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    before = cfg.read_bytes()
    for bad_list in ("arxiv", ["arxiv", 7], [""], {"a": "b"}, None):
        with pytest.raises(TeamError) as ei:
            resolve.write_profile_config_keys("p", {"skills.disabled": bad_list})
        assert ei.value.code == E_TEAM_INVALID, bad_list
    for bad_scalar in ("", "   ", None, True, [], {"default": "gpt-9"}):
        for key in ("model", "provider"):
            with pytest.raises(TeamError) as ei:
                resolve.write_profile_config_keys(
                    "p", {key: bad_scalar, "model": "m"} if key == "provider"
                    else {key: bad_scalar})
            assert ei.value.code == E_TEAM_INVALID, (key, bad_scalar)
    assert cfg.read_bytes() == before
    assert not cfg.with_name("config.yaml.hermes-agent-synergia.bak").exists()


def test_s10a_essential_skill_never_lands_in_the_deny_list(profile):
    """Do 3: the writer subtracts ESSENTIAL_SKILLS itself — the same veto
    ``save_disabled_skills`` applies — so no caller can disable the agent's own
    operating manual by calling the writer directly."""
    essential = essential_skills()
    assert essential, "the substrate must expose ESSENTIAL_SKILLS"
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    resolve.write_profile_config_keys(
        "p", {"skills.disabled": ["arxiv", essential[0], "dogfood"]})
    text = cfg.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["skills"]["disabled"] == ["arxiv", "dogfood"]
    for name in essential:
        assert name not in text, f"{name} leaked into the deny list"


def test_s10a_subtracting_essentials_can_empty_the_list(profile, write_counter):
    """The subtraction must not strand the write: an empty deny list renders as
    ``skills: disabled: []`` (the substrate's own spelling), reparses to
    ``[]``, and is then idempotent."""
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    resolve.write_profile_config_keys("p", {"skills.disabled": essential_skills()})
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert parsed["skills"]["disabled"] == []
    assert len(write_counter) == 1
    stat = cfg.stat()
    resolve.write_profile_config_keys("p", {"skills.disabled": []})
    after = cfg.stat()
    assert len(write_counter) == 1, "an already-empty deny list must not be rewritten"
    assert (after.st_mtime_ns, after.st_size) == (stat.st_mtime_ns, stat.st_size)


def test_s10a_backup_written_once_and_never_overwritten(profile):
    """Guard 3 across three separate write rounds on the new keys: the .bak
    holds the pristine pre-team bytes and is never re-copied."""
    home = profile("p", {"toolsets": ["kanban"]})
    cfg = cfg_of(home)
    original = cfg.read_bytes()
    resolve.write_profile_config_keys("p", {"skills.disabled": ["arxiv"]})
    bak = cfg.with_name("config.yaml.hermes-agent-synergia.bak")
    assert bak.read_bytes() == original
    stamp = bak.stat().st_mtime_ns
    resolve.write_profile_config_keys("p", {"model": "claude-opus-5",
                                            "provider": "anthropic"})
    resolve.write_profile_config_keys("p", {"skills.disabled": ["arxiv", "dogfood"]})
    assert bak.read_bytes() == original, "the pristine backup was clobbered"
    assert bak.stat().st_mtime_ns == stamp, "the backup was rewritten, not kept"


@requires_fixtures
def test_s10a_three_keys_on_the_full_template_touch_only_their_own_lines(stage):
    """The Verify bullet: writing all three against the 2 167-line commented
    template leaves every other line byte-identical.

    Three hunks, and the model hunk is the big one BY DESIGN: on this template
    ``model:`` is a ~300-line COMMENTED MAPPING (``model.default``,
    ``model.provider``, ``api_key``, ``base_url``, banners). The allow-listed
    key is ``model`` and the value is a SCALAR, so the write replaces that one
    key — the key it sets and nothing else; the Kanban banner and ``kanban:``
    below it keep their own bytes (asserted below, and the whole untouched body
    by ``untouched_text``). The pristine 2 167 lines are in the .bak (guard 3).

    Before the S10a ``_value_end`` generalisation this write REFUSED (the
    replacement stopped at the first blank line inside ``model:``, orphaning
    ``api_key:`` below it; the reparse guard fail-closed, so the key was
    unwritable on the very template the writer exists to protect — see
    ``test_value_span_crosses_blank_and_comment_runs``).
    """
    cfg = stage("agency-backend-engineer", src=_FULL)
    original = cfg.read_bytes()
    before = original.decode("utf-8").splitlines(keepends=True)
    model_at = next(i for i, t in enumerate(before) if t.rstrip("\n") == "model:")
    resolve.write_profile_config_keys("agency-backend-engineer", {
        "skills.disabled": ["arxiv", "dogfood"],
        "model": "claude-opus-5",
        "provider": "anthropic",
    })
    after = cfg.read_text(encoding="utf-8")
    new = lines(after)

    ops = changed_regions(before, new)
    assert len(ops) == 3, f"expected exactly three hunks, got {ops}"
    # 1. the model KEY (its whole block on this template) -> one scalar line.
    # The hunk may consume the key line and that key's OWN indented block and
    # nothing else — every other old line it swallows is a bug, and the
    # byte-compare below is what proves it swallowed no more.
    tag, i1, i2, j1, j2 = ops[0]
    assert tag == "replace" and i1 == model_at, f"model hunk: {ops[0]}"
    # One line, the model key, the value we asked for — the writer's scalar
    # renderer double-quotes (as the template itself does: ``cwd: "."``), so
    # the assertion is on the PARSED hunk, not on its spelling.
    assert j2 - j1 == 1, f"the scalar must be ONE line: {new[j1:j2]}"
    assert yaml.safe_load(new[j1]) == {"model": "claude-opus-5"}, new[j1]
    assert all(not t.strip() or t.lstrip().startswith("#") or t[:1] in (" ", "\t")
               for t in before[i1 + 1:i2]), \
        "the model hunk may only swallow the model key's own block (indented " \
        "lines, and the blank/comment runs inside it)"
    nxt = next(t for t in before[i2:] if t.strip() and not t.lstrip().startswith("#"))
    assert nxt.rstrip("\n") == "kanban:", \
        "the hunk must stop exactly at the next top-level key"
    # 2. disabled: inserted INSIDE the existing skills: block (no new section)
    tag, i1, i2, j1, j2 = ops[1]
    assert tag == "insert" and i1 == i2, f"skills hunk: {ops[1]}"
    assert new[j1].lstrip().startswith("disabled:"), new[j1]
    assert all(t.startswith("  ") for t in new[j1:j2]), "must stay inside skills:"
    # 3. provider: appended at EOF (the template has no top-level provider key)
    tag, i1, i2, j1, j2 = ops[2]
    assert tag == "insert" and i1 == i2 == len(before), f"provider hunk: {ops[2]}"
    assert new[-1].startswith("provider: "), new[-2:]
    assert yaml.safe_load(new[-1]) == {"provider": "anthropic"}, new[-1]

    # The byte-compare: every line outside those three hunks is identical on
    # both sides (~2 100 lines of comments and other sections). Not vacuous —
    # the untouched body must still be the bulk of the template.
    old_rest, new_rest = untouched_text(before, new)
    assert old_rest == new_rest, "an untouched line did not survive byte for byte"
    assert len(old_rest.splitlines()) > 1800, len(old_rest.splitlines())
    # Neighbouring sections survive untouched, explicitly.
    parsed = yaml.safe_load(after)
    assert parsed["kanban"] == yaml.safe_load(original.decode("utf-8"))["kanban"]
    assert parsed["terminal"]["cwd"] == "."        # the sentinel line, unharmed
    assert parsed["skills"]["creation_nudge_interval"] == 15
    assert parsed["skills"]["disabled"] == ["arxiv", "dogfood"]
    assert parsed["model"] == "claude-opus-5"
    assert parsed["provider"] == "anthropic"
    # Guard 3: the 2 167-line original is recoverable.
    assert cfg.with_name("config.yaml.hermes-agent-synergia.bak").read_bytes() == original


def t_started_at_column_0(text: str) -> bool:
    """A top-level key line (column 0, not a comment)."""
    return bool(text) and text[0] not in " \t#" and ":" in text


# ── S10a read seam: the profile's installed skill set (Do 3) ────────────────

def _install_skill(home: Path, name: str) -> None:
    """One installed skill in *home*'s own ``skills/`` tree — what
    ``_profile_scope`` + ``_find_all_skills`` are meant to see."""
    d = Path(home) / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: S10a fixture skill {name}\n---\n"
        f"Body of {name}.\n", encoding="utf-8")


def test_s10a_disabled_skills_for_is_the_complement_over_the_installed_set(monkeypatch):
    """Do 3 arithmetic: there is no ``skills.enabled``, so an enabled selection
    is stored as its COMPLEMENT over the installed set, minus ESSENTIAL_SKILLS.

    The installed set is patched (the real read needs the dashboard's deps —
    see ``requires_dashboard_deps``), so this asserts exactly the arithmetic
    S10 depends on: complement, essential-veto, sorted, dedup, ``None`` means
    "everything" only in the sense of an empty selection.
    """
    installed = sorted(["github", "arxiv", "dogfood", *essential_skills()])
    monkeypatch.setattr(resolve, "installed_skill_names", lambda _p: installed)
    denied = resolve.disabled_skills_for("p", ["github", "arxiv"])
    assert denied == ["dogfood"], denied                  # essentials never listed
    assert set(resolve.disabled_skills_for("p", installed)) == set()
    assert resolve.disabled_skills_for("p", ["dogfood"]) == \
        sorted({"github", "arxiv"} - set(essential_skills()))
    # Nothing enabled => everything except the essentials.
    assert resolve.disabled_skills_for("p", None) == ["arxiv", "dogfood", "github"]
    assert resolve.disabled_skills_for("p", []) == ["arxiv", "dogfood", "github"]
    # Junk in the selection is ignored, not smuggled through.
    assert resolve.disabled_skills_for("p", [" github ", ""]) == \
        ["arxiv", "dogfood"] + [s for s in essential_skills() if s == ""]


@requires_fastapi
def test_s10a_installed_skill_names_reads_the_profile_scoped_tree(profile):
    """Do 3's substrate seam, end to end: ``_profile_scope`` retargets
    ``HERMES_HOME`` AND rebinds ``skills_tool.SKILLS_DIR``, so
    ``_find_all_skills`` sees THIS profile's skills and nobody else's."""
    home = profile("p", {"toolsets": ["kanban"]})
    _install_skill(home, "s10a-alpha")
    _install_skill(home, "s10a-beta")
    installed = resolve.installed_skill_names("p")
    assert {"s10a-alpha", "s10a-beta"} <= set(installed), installed
    assert installed == sorted(set(installed))
    # The complement is what reaches the writer; the essentials stay out of it.
    denied = resolve.disabled_skills_for("p", ["s10a-alpha"])
    assert "s10a-alpha" not in denied and "s10a-beta" in denied
    assert set(denied) == set(installed) - {"s10a-alpha"} - set(essential_skills())
    # A skill installed nowhere is still "enabled" — it cannot be disabled.
    assert "s10a-never-installed" not in denied


@requires_fastapi
def test_s10a_disabled_skills_for_reaches_disk(profile, write_counter, monkeypatch):
    """Seam and writer agree: the complement the seam produces is accepted by
    the writer, lands verbatim, and is idempotent on the second sync."""
    home = profile("p", {"toolsets": ["kanban"]})
    _install_skill(home, "s10a-gamma")
    _install_skill(home, "s10a-delta")
    # Pin the universe so the assertion is about the projection, not about
    # whatever else this checkout has installed under .claude/skills.
    monkeypatch.setattr(resolve, "installed_skill_names",
                        lambda _p: ["s10a-gamma", "s10a-delta"])
    denied = resolve.disabled_skills_for("p", ["s10a-gamma"])
    resolve.write_profile_config_keys("p", {"skills.disabled": denied})
    parsed = yaml.safe_load(cfg_of(home).read_text(encoding="utf-8"))
    assert parsed["skills"]["disabled"] == ["s10a-delta"]
    assert len(write_counter) == 1
    resolve.write_profile_config_keys("p", {"skills.disabled": denied})
    assert len(write_counter) == 1, "the second sync rewrote an unchanged deny list"

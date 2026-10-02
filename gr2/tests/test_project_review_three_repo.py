"""M1 local three-repository contract, deliberately landed before its service."""

from __future__ import annotations

from tests.native_root_helper import native_root

import argparse
import subprocess
import tomllib
import dataclasses
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lanes


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _source(root: Path, name: str) -> tuple[Path, str, str]:
    origin = root / f"{name}.git"
    _git(root, "init", "--bare", "-b", "main", str(origin))
    source = root / name
    _git(root, "clone", "-q", str(origin), str(source))
    _git(source, "config", "user.email", "m1@example.invalid")
    _git(source, "config", "user.name", "m1")
    (source / "README.md").write_text(f"{name} base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "push", "-q", "origin", "main")
    base = _git(source, "rev-parse", "main")
    branch = f"review/{name}"
    _git(source, "checkout", "-q", "-b", branch)
    (source / "review.txt").write_text(f"{name} review\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "review")
    _git(source, "push", "-q", "origin", branch)
    head = _git(source, "rev-parse", branch)
    _git(source, "checkout", "-q", "main")
    return source, base, head


def _world(tmp_path: Path, *, bootstrap_from_manifest: bool = False):
    workspace = tmp_path / "workspace"
    if bootstrap_from_manifest:
        manifest = workspace / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('''version: 2
repos:
  alpha:
    url: https://example.invalid/alpha.git
    path: sources/alpha
  beta:
    url: https://example.invalid/beta.git
    path: sources/beta
  gamma:
    url: https://example.invalid/gamma.git
    path: sources/gamma
''')
        (workspace / ".gitgrip" / "agents.toml").write_text('''[agents.atlas]
worktree = "main"
channel = "dev"
''')
        from gr2.python_cli.migration import bootstrap_gr1_workspace
        bootstrap_gr1_workspace(workspace)
        native_root(workspace)  # review binds live in a native root's own .git
    else:
        (workspace / ".grip").mkdir(parents=True)
        native_root(workspace)
        (workspace / ".grip" / "workspace_spec.toml").write_text('''schema_version = 1
workspace_name = "m1"

[[repos]]
name = "alpha"
path = "sources/alpha"
url = "https://example.invalid/alpha.git"

[[repos]]
name = "beta"
path = "sources/beta"
url = "https://example.invalid/beta.git"

[[repos]]
name = "gamma"
path = "sources/gamma"
url = "https://example.invalid/gamma.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["alpha", "beta", "gamma"]
''')
    sources = {name: _source(tmp_path, name) for name in ("alpha", "beta", "gamma")}
    # The compiled workspace is the authorization surface. Replace the fixture
    # placeholders with the actual canonical origins after source creation.
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        "workspace_name = \"m1\"\n\n"
        + "\n".join(
            f'[[repos]]\nname = "{name}"\npath = "sources/{name}"\nurl = "{_git(source[0], "remote", "get-url", "origin")}"\n'
            for name, source in sources.items()
        )
        + '\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["alpha", "beta", "gamma"]\n'
    )
    home = tmp_path / "home"
    _git(tmp_path, "init", "-q", str(home))
    _git(home, "config", "user.email", "m1@example.invalid")
    _git(home, "config", "user.name", "m1")
    (home / "tracked.txt").write_text("base\n")
    _git(home, "add", ".")
    _git(home, "commit", "-q", "-m", "home")
    (home / "tracked.txt").write_text("dirty\n")
    (home / "untracked.txt").write_text("keep\n")
    lanes.create_lane(argparse.Namespace(workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature", repos="alpha", branch="main", source="test", default_commands=[]))
    lanes.enter_lane(argparse.Namespace(workspace_root=workspace, owner_unit="atlas", lane_name="home", actor="agent:atlas", notify_channel=False, recall=False))
    current = lanes.current_lane_file(workspace, "atlas").read_bytes()
    return workspace, sources, home, current


def test_three_repo_exact_pins_open_only_after_all_members_verify(tmp_path: Path) -> None:
    workspace, sources, home, current = _world(tmp_path)
    from gr2.python_cli.project_review import ProjectReviewPin, make_spec, open_project_review
    pins = [ProjectReviewPin(key=name, repo=f"local:{source[0]}", path=f"repos/{name}", base=source[1], head=source[2]) for name, source in reversed(list(sources.items()))]
    from gr2.python_cli import grip
    native_root(workspace)
    spec = make_spec(workspace, pins)
    outcome = open_project_review(workspace=workspace, owner_unit="atlas", lane_name="review-m1", spec=spec, sources={name: (source[0], f"review/{name}") for name, source in sources.items()}, allow_local=True)
    assert outcome.status == "opened"
    assert [record.head for record in outcome.observed] == [sources[name][2] for name in ("alpha", "beta", "gamma")]
    assert all((outcome.review_root / "repos" / name / ".git").is_dir() for name in sources)
    assert home.joinpath("tracked.txt").read_text() == "dirty\n"
    assert home.joinpath("untracked.txt").read_text() == "keep\n"
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() != current


def test_three_immutable_seed_sources_open_at_pins_not_source_heads(tmp_path: Path) -> None:
    """The dogfood shape: transports may name a resolved SHA, not a branch.

    Every source remains on main, which differs from its review pin. All three
    lanes must still materialize at their immutable pins and enter together.
    """
    workspace, sources, _home, _current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    pins = [
        project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2])
        for name, source in sources.items()
    ]
    spec = project_review.make_spec(workspace, pins)
    assert all(_git(source[0], "rev-parse", "HEAD") != source[2] for source in sources.values())

    outcome = project_review.open_project_review(
        workspace=workspace,
        owner_unit="atlas",
        lane_name="immutable-seeds",
        spec=spec,
        sources={name: (source[0], source[2]) for name, source in sources.items()},
        allow_local=True,
    )

    assert outcome.status == "opened"
    assert [record.head for record in outcome.observed] == [sources[name][2] for name in ("alpha", "beta", "gamma")]
    assert all(
        _git(outcome.review_root / "repos" / name, "rev-parse", "HEAD") == sources[name][2]
        for name in sources
    )


def test_deleting_explicit_seed_recreates_wrong_source_head_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation control: omitting the explicit seed reproduces the live mismatch.

    The retained post-materialization check catches it, but the review remains
    partial and never enters the current lane. This proves the seed propagation,
    rather than the final checker, is what opens all three pinned repositories.
    """
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review, review

    native_root(workspace)
    pins = [
        project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2])
        for name, source in sources.items()
    ]
    spec = project_review.make_spec(workspace, pins)
    real = review.ensure_lane_checkout
    calls: list[dict[str, object]] = []

    def drop_seed(**kwargs):
        calls.append(dict(kwargs))
        kwargs.pop("seed_commit")
        return real(**kwargs)

    monkeypatch.setattr(review, "ensure_lane_checkout", drop_seed)
    outcome = project_review.open_project_review(
        workspace=workspace,
        owner_unit="atlas",
        lane_name="seed-deleted",
        spec=spec,
        sources={name: (source[0], source[2]) for name, source in sources.items()},
        allow_local=True,
    )

    assert calls and calls[0]["seed_commit"] == sources["alpha"][2]
    assert outcome.status == "partial"
    assert outcome.failures[0].key == "alpha"
    assert sources["alpha"][1] in outcome.failures[0].reason
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current
    assert not (workspace / "reviews" / "atlas" / "seed-deleted" / "repos" / "alpha").exists()


def test_manifest_bootstrap_opens_the_authorized_three_repo_review(tmp_path: Path) -> None:
    """M1's real consumer can start from gr1 manifest state with no hand-written spec."""
    workspace, sources, _home, _current = _world(tmp_path, bootstrap_from_manifest=True)
    from gr2.python_cli import project_review
    pins = [
        project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2])
        for name, source in sources.items()
    ]
    spec = project_review.make_spec(workspace, pins)
    outcome = project_review.open_project_review(
        workspace=workspace,
        owner_unit="atlas",
        lane_name="bootstrap-review",
        spec=spec,
        sources={name: (source[0], f"review/{name}") for name, source in sources.items()},
        allow_local=True,
    )
    assert outcome.status == "opened"
    assert [record.head for record in outcome.observed] == [sources[name][2] for name in ("alpha", "beta", "gamma")]


@pytest.mark.parametrize("owner_unit,lane_name", [("../escaped-owner", "review"), ("atlas", ".."), ("C:", "review"), (r"\\server\share", "review"), ("atlas", "child/name")])
def test_unsafe_review_root_components_refuse_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_unit: str, lane_name: str
) -> None:
    workspace, sources, _home, _current = _world(tmp_path, bootstrap_from_manifest=True)
    from gr2.python_cli import project_review
    pins = [project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2]) for name, source in sources.items()]
    spec = project_review.make_spec(workspace, pins)
    calls: list[str] = []
    monkeypatch.setattr(project_review.review, "open_review_lane", lambda **_kwargs: calls.append("clone"))

    outcome = project_review.open_project_review(
        workspace=workspace, owner_unit=owner_unit, lane_name=lane_name, spec=spec,
        sources={name: (source[0], f"review/{name}") for name, source in sources.items()}, allow_local=True,
    )

    assert outcome.status == "refused"
    assert outcome.failures[0].key == "review_root"
    assert calls == []
    assert not (workspace / "escaped-owner").exists()


def test_invalid_review_root_preflight_creates_no_escaped_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, _current = _world(tmp_path, bootstrap_from_manifest=True)
    from gr2.python_cli import project_review
    pins = [project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2]) for name, source in sources.items()]
    spec = project_review.make_spec(workspace, pins)
    monkeypatch.setattr(project_review, "_review_path_component", lambda value, _field: value)

    with pytest.raises(SystemExit, match="invalid owner_unit"):
        project_review.open_project_review(
            workspace=workspace, owner_unit="../escaped-owner", lane_name="review", spec=spec,
            sources={name: (source[0], f"review/{name}") for name, source in sources.items()}, allow_local=True,
        )

    assert not (workspace / "escaped-owner").exists()


def test_missing_pin_refuses_before_any_review_materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review
    native_root(workspace)
    pins = [project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], ("f" * 40 if name == "gamma" else source[2])) for name, source in sources.items()]
    spec = project_review.make_spec(workspace, pins)
    calls: list[str] = []
    monkeypatch.setattr(project_review.review, "open_review_lane", lambda **kwargs: calls.append("clone"))
    outcome = project_review.open_project_review(workspace=workspace, owner_unit="atlas", lane_name="missing", spec=spec, sources={name: (source[0], f"review/{name}") for name, source in sources.items()}, allow_local=True)
    assert outcome.status == "refused" and outcome.failures[0].key == "gamma"
    assert calls == []
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


def test_beta_failure_is_partial_and_never_enters_review_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review
    native_root(workspace)
    spec = project_review.make_spec(workspace, [project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2]) for name, source in sources.items()])
    real = project_review.review.open_review_lane
    calls: list[str] = []
    def fail_beta(**kwargs):
        key = kwargs["lane_repo_root"].name
        calls.append(key)
        if key == "beta":
            raise RuntimeError("beta injected failure")
        return real(**kwargs)
    monkeypatch.setattr(project_review.review, "open_review_lane", fail_beta)
    outcome = project_review.open_project_review(workspace=workspace, owner_unit="atlas", lane_name="partial", spec=spec, sources={name: (source[0], f"review/{name}") for name, source in sources.items()}, allow_local=True)
    assert outcome.status == "partial" and calls == ["alpha", "beta"]
    assert [record.head for record in outcome.observed] == [sources["alpha"][2]]
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


@pytest.mark.parametrize("field,value", [("repo", "local:/substituted"), ("path", "repos/substituted"), ("base", "a" * 40), ("head", "b" * 40)])
def test_full_gr_commit_field_mismatch_refuses_before_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review
    native_root(workspace)
    pins = [project_review.ProjectReviewPin(name, f"local:{source[0]}", f"repos/{name}", source[1], source[2]) for name, source in sources.items()]
    spec = project_review.make_spec(workspace, pins)
    beta = next(pin for pin in spec.pins if pin.key == "beta")
    mismatched = dataclasses.replace(beta, **{field: value})
    bad_spec = dataclasses.replace(spec, pins=tuple(mismatched if pin.key == "beta" else pin for pin in spec.pins))
    calls: list[str] = []
    monkeypatch.setattr(project_review.review, "open_review_lane", lambda **_kwargs: calls.append("open"))
    outcome = project_review.open_project_review(workspace=workspace, owner_unit="atlas", lane_name="mismatch", spec=bad_spec, sources={name: (source[0], f"review/{name}") for name, source in sources.items()}, allow_local=True)
    assert outcome.status == "refused"
    assert outcome.failures[0].key == "beta" and field in outcome.failures[0].reason
    assert calls == []
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


def test_project_review_cli_adapter_and_structured_rendering_are_registered() -> None:
    from gr2.python_cli import app as app_module
    from gr2.python_cli import project_review
    commands = {command.name: command for command in app_module.review_app.registered_commands}
    assert "open-project" in commands
    outcome = project_review.ProjectReviewOutcome("refused", "a" * 40, (), (project_review.ProjectReviewFailure("beta", "mismatch for head"),), None, False)
    assert project_review.outcome_payload(outcome) == {"status": "refused", "grip_commit": "a" * 40, "observed": [], "failures": [{"key": "beta", "reason": "mismatch for head"}], "review_root": None, "current_lane_changed": False}


def test_unknown_workspace_key_refuses_before_clone_or_review_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    alpha = sources["alpha"]
    pin = project_review.ProjectReviewPin("grip", f"local:{alpha[0]}", "repos/grip", alpha[1], alpha[2])
    spec = project_review.make_spec(workspace, [pin])
    calls: list[str] = []
    monkeypatch.setattr(project_review.review, "open_review_lane", lambda **_kwargs: calls.append("clone"))

    outcome = project_review.open_project_review(
        workspace=workspace, owner_unit="atlas", lane_name="unknown-key", spec=spec,
        sources={"grip": (alpha[0], alpha[2])}, allow_local=True,
    )

    assert outcome.status == "refused" and outcome.failures[0].key == "grip"
    assert "unknown workspace repository key" in outcome.failures[0].reason
    assert calls == []
    assert outcome.review_root is None
    assert not (workspace / "reviews" / "atlas" / "unknown-key").exists()
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


def test_workspace_identity_and_source_origin_refuse_before_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    alpha, beta = sources["alpha"], sources["beta"]
    correct = project_review.ProjectReviewPin("alpha", f"local:{alpha[0]}", "repos/alpha", alpha[1], alpha[2])
    bad_identity = dataclasses.replace(correct, repo=f"local:{beta[0]}")
    calls: list[str] = []
    monkeypatch.setattr(project_review.review, "open_review_lane", lambda **_kwargs: calls.append("clone"))

    identity_outcome = project_review.open_project_review(
        workspace=workspace, owner_unit="atlas", lane_name="identity", spec=project_review.make_spec(workspace, [bad_identity]),
        sources={"alpha": (alpha[0], alpha[2])}, allow_local=True,
    )
    source_outcome = project_review.open_project_review(
        workspace=workspace, owner_unit="atlas", lane_name="source-origin", spec=project_review.make_spec(workspace, [correct]),
        sources={"alpha": (beta[0], beta[2])}, allow_local=True,
    )

    assert identity_outcome.status == "refused" and "workspace repository identity mismatch" in identity_outcome.failures[0].reason
    assert source_outcome.status == "refused" and "selected source identity mismatch" in source_outcome.failures[0].reason
    assert calls == []
    assert identity_outcome.review_root is None and source_outcome.review_root is None
    assert not (workspace / "reviews" / "atlas" / "identity").exists()
    assert not (workspace / "reviews" / "atlas" / "source-origin").exists()
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


def test_malformed_workspace_spec_refuses_before_transport_or_lane_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    alpha = sources["alpha"]
    spec = project_review.make_spec(workspace, [project_review.ProjectReviewPin("alpha", f"local:{alpha[0]}", "repos/alpha", alpha[1], alpha[2])])
    (workspace / ".grip" / "workspace_spec.toml").write_text("[[repos]\nname = \"unterminated\"\n")
    calls: list[str] = []
    monkeypatch.setattr(project_review.review, "open_review_lane", lambda **_kwargs: calls.append("clone"))

    outcome = project_review.open_project_review(
        workspace=workspace, owner_unit="atlas", lane_name="malformed-spec", spec=spec,
        sources={"alpha": (alpha[0], alpha[2])}, allow_local=True,
    )

    assert outcome.status == "refused"
    assert outcome.failures[0].key == "workspace_spec"
    assert calls == [] and outcome.review_root is None
    assert not (workspace / "reviews" / "atlas" / "malformed-spec").exists()
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


def test_deleting_malformed_spec_translation_recreates_parser_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, _current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    alpha = sources["alpha"]
    spec = project_review.make_spec(workspace, [project_review.ProjectReviewPin("alpha", f"local:{alpha[0]}", "repos/alpha", alpha[1], alpha[2])])
    (workspace / ".grip" / "workspace_spec.toml").write_text("[[repos]\nname = \"unterminated\"\n")
    monkeypatch.setattr(project_review, "_load_workspace_boundary_doc", lambda path: (project_review.spec_apply.load_workspace_spec_doc(path), None))

    with pytest.raises(tomllib.TOMLDecodeError):
        project_review.open_project_review(
            workspace=workspace, owner_unit="atlas", lane_name="translation-deleted", spec=spec,
            sources={"alpha": (alpha[0], alpha[2])}, allow_local=True,
        )


def test_deleting_workspace_boundary_recreates_unknown_key_clone_side_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    alpha = sources["alpha"]
    pin = project_review.ProjectReviewPin("grip", f"local:{alpha[0]}", "repos/grip", alpha[1], alpha[2])
    spec = project_review.make_spec(workspace, [pin])
    monkeypatch.setattr(project_review, "_validate_workspace_repository_boundary", lambda **_kwargs: None)

    with pytest.raises(SystemExit, match="unknown repos for lane: grip"):
        project_review.open_project_review(
            workspace=workspace, owner_unit="atlas", lane_name="boundary-deleted", spec=spec,
            sources={"grip": (alpha[0], alpha[2])}, allow_local=True,
        )

    assert (workspace / "reviews" / "atlas" / "boundary-deleted" / "repos" / "grip" / ".git").is_dir()
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() == current


def test_deleting_identity_boundary_recreates_clone_side_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, sources, _home, _current = _world(tmp_path)
    from gr2.python_cli import grip, project_review

    native_root(workspace)
    alpha, beta = sources["alpha"], sources["beta"]
    pin = project_review.ProjectReviewPin("alpha", f"local:{beta[0]}", "repos/alpha", alpha[1], alpha[2])
    spec = project_review.make_spec(workspace, [pin])
    monkeypatch.setattr(project_review, "_validate_workspace_repository_boundary", lambda **_kwargs: None)

    outcome = project_review.open_project_review(
        workspace=workspace, owner_unit="atlas", lane_name="identity-deleted", spec=spec,
        sources={"alpha": (alpha[0], alpha[2])}, allow_local=True,
    )

    assert outcome.status == "opened"
    assert (workspace / "reviews" / "atlas" / "identity-deleted" / "repos" / "alpha" / ".git").is_dir()


def test_canonical_repo_identity_local_prefix_honors_allow_local_false(tmp_path: Path) -> None:
    """The ``local:`` branch of ``_canonical_repo_identity``
    ignored ``allow_local`` in BOTH sub-cases, so a production review
    (allow_local=False) accepted a local: pin at the very boundary this commit
    exists to enforce. Both sub-cases must refuse with the same ReviewError as a
    plain non-GitHub origin; reverting the fix (hardcoded allow_local=True on the
    origin branch, bare ``local:<path>`` return on the no-origin branch) reds this.
    """
    from gr2.python_cli import project_review, review

    # sub-case 1: a local: value with no resolvable origin (bare path).
    bare = tmp_path / "not-a-checkout"
    bare.mkdir()
    with pytest.raises(review.ReviewError):
        project_review._canonical_repo_identity(f"local:{bare}", allow_local=False)

    # sub-case 2: a real checkout whose own origin is itself a local filesystem
    # path (this is exactly what _source builds) -- the origin branch must not
    # hardcode allow_local=True.
    checkout, _base, _head = _source(tmp_path, "svc")
    assert _git(checkout, "remote", "get-url", "origin").startswith(str(tmp_path))
    with pytest.raises(review.ReviewError):
        project_review._canonical_repo_identity(f"local:{checkout}", allow_local=False)

    # controls: allow_local=True still accepts both, byte-identical to pre-fix.
    assert project_review._canonical_repo_identity(f"local:{bare}", allow_local=True) == f"local:{bare.resolve()}"
    assert project_review._canonical_repo_identity(f"local:{checkout}", allow_local=True).startswith("local:")
    # control: a GitHub-shaped local: origin canonicalizes regardless of the flag.
    assert project_review._canonical_repo_identity("https://github.com/o/r", allow_local=False) == "https://github.com/o/r"

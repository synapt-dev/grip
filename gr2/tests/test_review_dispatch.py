"""Item-3 spike: the review-verb collapse dispatch decisions.
- ``open`` dispatches on its argument (gr:<sha>/hex -> open-gr, digits -> PR, else project).
- ``close`` reads the lane marker (open-gr reconstruct marker -> reconstruction teardown).
End-to-end: a real open-gr marker routes to close_open_gr_lane and reclaims the lane;
a lane with no marker is a PR lane and open-gr teardown refuses it."""
import pytest

from python_cli import review_dispatch
from python_cli import open_gr_review


def _flat_output(result) -> str:
    """CLI error text is rich-wrapped inside a box (newlines + │ borders + padding);
    collapse all whitespace so a substring needle survives the wrapping."""
    return " ".join((result.output or "").split())


def test_review_open_legacy_pr_head_positionals_do_not_classify_owner_unit(tmp_path):
    """The legacy PR-head form OWNER_UNIT REPO PR_NUMBER (the shape every repo test
    uses) must NOT be classified: its owner_unit is an arbitrary word that
    classify_open_target reads as "project". Positionals decide first, so a full
    three-positional open reaches the PR-head path and never the project refusal."""
    from typer.testing import CliRunner
    from python_cli.app import app

    runner = CliRunner()
    ws = tmp_path / "ws"
    ws.mkdir()
    # atlas = owner_unit, recall = repo, 12 = pr_number  (a word owner_unit)
    r = runner.invoke(app, ["review", "open", str(ws), "atlas", "recall", "12"])
    assert r.exit_code != 0  # no workspace spec/remote here, so it fails downstream
    # the point of the fix: it did NOT bail with the project-review refusal
    assert "project-review" not in _flat_output(r)


def test_review_open_lone_project_id_still_refuses_with_project_message(tmp_path):
    """Control: with only a lone non-hex/non-digit target (no repo/pr_number),
    classification still fires and the project-review refusal is the one raised."""
    from typer.testing import CliRunner
    from python_cli.app import app

    runner = CliRunner()
    ws = tmp_path / "ws"
    ws.mkdir()
    r = runner.invoke(app, ["review", "open", str(ws), "proj-review-x"])
    assert r.exit_code != 0
    assert "project-review" in _flat_output(r)


def test_review_open_lone_pr_number_refuses_needing_owner_unit_and_repo(tmp_path):
    """A lone PR number cannot open a PR-head lane: that path needs OWNER_UNIT and
    REPO positionals too. It refuses, and NOT with the project message."""
    from typer.testing import CliRunner
    from python_cli.app import app

    runner = CliRunner()
    ws = tmp_path / "ws"
    ws.mkdir()
    r = runner.invoke(app, ["review", "open", str(ws), "12"])
    assert r.exit_code != 0
    flat = _flat_output(r)
    assert "OWNER_UNIT" in flat and "PR_NUMBER" in flat
    assert "project-review" not in flat


@pytest.mark.parametrize("target,kind", [
    ("gr:deadbeef", "gr"),
    ("gr:0123456789abcdef0123456789abcdef01234567", "gr"),
    ("deadbeef", "gr"),                       # bare 8-hex sha
    ("0123456789abcdef0123456789abcdef01234567", "gr"),  # bare 40-hex
    ("1186", "pr"),
    ("42", "pr"),
    ("proj-review-x", "project"),
    ("recall-0.25", "project"),               # has letters + a dot, not hex
])
def test_classify_open_target(target, kind):
    assert review_dispatch.classify_open_target(target) == kind


def test_close_classifies_and_routes_a_real_reconstruction_lane(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "some-reconstructed-repo").mkdir()  # the disposable tree open-gr would create
    open_gr_review.write_open_gr_marker(
        lane,
        "gr:deadbeef",
        {"recall": {"bound_head_tree": "t", "reconstructed_tree": "t"}},
    )
    # decision: the marker tells close this is a reconstruction lane
    assert review_dispatch.classify_close_lane(lane) == "reconstruction"
    # end-to-end: the reconstruction teardown reclaims it
    result = open_gr_review.close_open_gr_lane(lane)
    assert result["gr_commit"] == "gr:deadbeef"  # close returns the marker's gr_commit verbatim
    assert not lane.exists()


def test_close_classifies_a_pr_lane_and_open_gr_teardown_refuses_it(tmp_path):
    lane = tmp_path / "pr-lane"
    lane.mkdir()
    (lane / "checkout").mkdir()  # a PR-head lane, no open-gr marker
    assert review_dispatch.classify_close_lane(lane) == "pr"
    # the open-gr teardown must NOT remove a non-reconstruction lane
    with pytest.raises(open_gr_review.OpenGrReviewError):
        open_gr_review.close_open_gr_lane(lane)
    assert lane.exists()  # untouched

"""Run context belongs to reconstruction markers, never sibling/latest lanes."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import review_run as rr
from gr2.python_cli.app import app


def marker(lane, name=rr._MARKER_NAME, kind='review-open'):
    lane.mkdir(parents=True, exist_ok=True)
    (lane / name).write_text(json.dumps({'kind': kind, 'repos': []}))


@pytest.mark.parametrize('legacy', [False, True])
def test_enclosing_marker_from_lane_and_member(tmp_path, legacy):
    lane = tmp_path / 'lane'
    marker(lane, '.grip-open-gr-reconstruct.json' if legacy else rr._MARKER_NAME,
           'open-gr-reconstruct' if legacy else 'review-open')
    nested = lane / 'alpha' / 'tests'
    nested.mkdir(parents=True)
    assert rr.resolve_run_lane(None, cwd=lane) == lane
    assert rr.resolve_run_lane(None, cwd=nested) == lane


def test_explicit_override_and_invalid_never_fallback(tmp_path):
    ambient, explicit = tmp_path/'ambient', tmp_path/'explicit'
    marker(ambient)
    marker(explicit)
    assert rr.resolve_run_lane(explicit, cwd=ambient) == explicit
    with pytest.raises(rr.ReviewRunRefused, match='no_marker'):
        rr.resolve_run_lane(tmp_path/'missing', cwd=ambient)


def test_missing_context_does_not_scan_siblings(tmp_path):
    marker(tmp_path/'sibling')
    with pytest.raises(rr.ReviewRunRefused, match='no_review_context'):
        rr.resolve_run_lane(None, cwd=tmp_path)


def test_nested_markers_are_ambiguous_but_explicit_still_wins(tmp_path):
    marker(tmp_path)
    nested = tmp_path/'nested'
    marker(nested)
    with pytest.raises(rr.ReviewRunRefused, match='ambiguous_review_context'):
        rr.resolve_run_lane(None, cwd=nested)
    assert rr.resolve_run_lane(nested, cwd=nested) == nested


@pytest.mark.parametrize('payload,code', [('broken', 'bad_marker'), ('[]', 'bad_marker'),
                                       ('{"kind":"development"}', 'not_open_gr')])
def test_bad_marker_refuses_without_fallback(tmp_path, payload, code):
    (tmp_path/rr._MARKER_NAME).write_text(payload)
    with pytest.raises(rr.ReviewRunRefused, match=code):
        rr.resolve_run_lane(None, cwd=tmp_path)


def test_cli_bare_run_delegates_resolved_lane(tmp_path, monkeypatch):
    marker(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls = []
    def run(lane, **kwargs):
        calls.append(lane)
        return {'result': 'green', 'passed': 1}
    monkeypatch.setattr(rr, 'run_review_lane', run)
    result = CliRunner().invoke(app, ['review', 'run', '--json'])
    assert result.exit_code == 0, result.output
    assert calls == [tmp_path]
    assert json.loads(result.stdout)['result'] == 'green'


def test_cli_invalid_explicit_refuses_before_run(tmp_path, monkeypatch):
    marker(tmp_path)
    monkeypatch.chdir(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('invalid explicit path must not run enclosing lane')
    monkeypatch.setattr(rr, 'run_review_lane', forbidden)
    result = CliRunner().invoke(app, ['review', 'run', str(tmp_path/'missing'), '--json'])
    assert result.exit_code == 2
    assert json.loads(result.stdout)['refusal_code'] == 'no_marker'

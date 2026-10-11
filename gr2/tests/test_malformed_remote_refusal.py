"""A remote that cannot be parsed is refused, never a traceback.

`urlsplit` raises ValueError on a malformed bracketed host. The one owner of the credential rule,
`grip.url_has_credentials`, used to let that escape, so every writer that asks it exited with a Python
traceback instead of a named refusal. A URL that cannot be parsed cannot be shown credential-free, so the
owner fails closed (the direction `_is_portable_remote` already takes) and each caller's refusal names the
member, never the URL.
"""
from __future__ import annotations

import pytest

from gr2.python_cli import grip
from gr2.python_cli.grip_cli import _url_has_credentials

MALFORMED = ("https://[bad/o/r.git", "https://[::1/o/r.git", "https://host]/o/r.git", "ssh://git@[bad/o/r.git")


@pytest.mark.parametrize("url", MALFORMED)
def test_owner_fails_closed_on_a_malformed_url(url):
    assert grip.url_has_credentials(url) is True


@pytest.mark.parametrize("url", MALFORMED)
def test_store_init_wrapper_fails_closed_on_a_malformed_url(url):
    assert _url_has_credentials(url) is True


@pytest.mark.parametrize("url", MALFORMED)
def test_review_record_writer_refuses_a_malformed_remote_by_name(url):
    with pytest.raises(grip.GripReviewRefused) as caught:
        grip._refuse_remote_credentials("member", url)
    assert "member" in str(caught.value) and url not in str(caught.value)


@pytest.mark.parametrize("url", MALFORMED)
def test_review_open_refuses_a_malformed_recorded_remote_by_name(url, tmp_path):
    from types import SimpleNamespace

    from gr2.python_cli import open_gr_review

    pin = SimpleNamespace(key="alpha", repo=url)
    with pytest.raises(open_gr_review.OpenGrReviewError) as caught:
        open_gr_review.resolve_sources_from_pins([pin], cache_root=tmp_path / "cache")
    assert "alpha" in str(caught.value) and url not in str(caught.value)
    assert not (tmp_path / "cache").exists()

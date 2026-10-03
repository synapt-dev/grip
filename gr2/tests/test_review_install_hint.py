"""The `.review-install` hint reads the same whether it is written as TOML or as bare `key = value` lines, and
refuses (naming the file and the line) anything that is neither, instead of skipping it. The two spellings
were hit independently by two strangers in the stranger-path arm (a6 day 1 and day 2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import review_run as rr


def _hint(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".review-install").write_text(text)
    return tmp_path


LEGACY = "install = {venv} -m pip install -q . pytest\npackage = alpha\n"
TOML = 'install = "{venv} -m pip install -q . pytest"\npackage = "alpha"\n'
WANT = {"install": "{venv} -m pip install -q . pytest", "package": "alpha"}


def test_toml_and_bare_forms_read_the_same(tmp_path: Path) -> None:
    assert rr.read_install_hint(_hint(tmp_path, TOML)) == WANT
    assert rr.read_install_hint(_hint(tmp_path, LEGACY)) == WANT


def test_a_mixed_file_reads_the_same(tmp_path: Path) -> None:
    mixed = 'install = {venv} -m pip install -q . pytest\npackage = "alpha"\n'
    assert rr.read_install_hint(_hint(tmp_path, mixed)) == WANT


def test_the_committed_bare_hint_in_this_repo_still_reads(tmp_path: Path) -> None:
    text = "# c\ninstall = {venv} -m pip install -e {lane}/gr2[dev]\npackage = gr2\n"
    assert rr.read_install_hint(_hint(tmp_path, text)) == {
        "install": "{venv} -m pip install -e {lane}/gr2[dev]",
        "package": "gr2",
    }


def test_a_section_header_refuses_naming_file_and_line(tmp_path: Path) -> None:
    text = '[review]\npackage = alpha\n'  # not valid TOML for the whole file only because of the bare value
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.read_install_hint(_hint(tmp_path, text))
    assert exc.value.code == "bad_hint"
    assert ".review-install line 1" in str(exc.value), str(exc.value)


def test_a_toml_section_refuses_too(tmp_path: Path) -> None:
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.read_install_hint(_hint(tmp_path, '[review]\npackage = "alpha"\n'))
    assert exc.value.code == "bad_hint"
    assert ".review-install line 1" in str(exc.value) and "[review] section" in str(exc.value), str(exc.value)


def test_a_stray_line_refuses_naming_its_line(tmp_path: Path) -> None:
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.read_install_hint(_hint(tmp_path, "# c\npackage = alpha\npip install .\n"))
    assert exc.value.code == "bad_hint"
    assert "line 3" in str(exc.value), str(exc.value)


def test_a_list_value_refuses_naming_the_key(tmp_path: Path) -> None:
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.read_install_hint(_hint(tmp_path, 'install = ["{venv}", "-m", "pip"]\n'))
    assert exc.value.code == "bad_hint"
    assert "'install'" in str(exc.value) and "list" in str(exc.value)


def test_a_repeated_key_in_the_bare_form_keeps_the_last_value(tmp_path: Path) -> None:
    # Unchanged behaviour: the review fixtures append an `install =` line to override the default one.
    got = rr.read_install_hint(_hint(tmp_path, "install = {venv} a\ninstall = {venv} b\n"))
    assert got == {"install": "{venv} b"}


def test_an_unknown_key_still_names_the_key_in_both_forms(tmp_path: Path) -> None:
    for text in ('instal = "x"\n', "instal = x y\n"):
        with pytest.raises(rr.ReviewRunRefused) as exc:
            rr.read_install_hint(_hint(tmp_path, text))
        assert exc.value.code == "bad_hint" and "'instal'" in str(exc.value)


def test_blank_comments_crlf_and_a_bom_read(tmp_path: Path) -> None:
    (tmp_path / ".review-install").write_bytes(b"\xef\xbb\xbf# c\r\n\r\npackage = alpha\r\ninstall = {venv} x\r\n")
    assert rr.read_install_hint(tmp_path) == {"package": "alpha", "install": "{venv} x"}
    (tmp_path / ".review-install").write_text("")
    assert rr.read_install_hint(tmp_path) == {}
    (tmp_path / ".review-install").write_text("# only a comment\n")
    assert rr.read_install_hint(tmp_path) == {}


def test_a_quoted_multi_word_value_in_the_bare_form_keeps_its_words(tmp_path: Path) -> None:
    got = rr.read_install_hint(_hint(tmp_path, 'install = "{venv}" -m pip install .\npackage = x\n'))
    assert got["install"] == '"{venv}" -m pip install .'


def test_no_file_is_none(tmp_path: Path) -> None:
    assert rr.read_install_hint(tmp_path) is None

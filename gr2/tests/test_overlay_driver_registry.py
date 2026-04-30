from __future__ import annotations

import os
from pathlib import Path

import pytest

from gr2_overlay.drivers import CURATED_DRIVERS, install_driver_registry, invoke_driver
from gr2_overlay.types import OverlayRef


def test_curated_driver_registry_is_exact_and_closed() -> None:
    assert set(CURATED_DRIVERS) == {
        "overlay-deep",
        "overlay-prepend",
        "overlay-union",
    }


def test_install_writes_driver_entries_to_home_gitconfig_not_calling_shell_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    shell_override = tmp_path / "shell-global.gitconfig"
    shell_override.write_text("")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(shell_override))

    install_driver_registry()

    home_gitconfig = fake_home / ".gitconfig"
    assert home_gitconfig.exists()
    config_text = home_gitconfig.read_text()

    assert '[merge "overlay-deep"]' in config_text
    assert '[merge "overlay-prepend"]' in config_text
    assert '[merge "overlay-union"]' in config_text
    assert "%O %A %B %P" in config_text

    assert shell_override.read_text() == ""


@pytest.mark.parametrize(
    ("relative_path", "current_text", "other_text", "expected_snippets"),
    [
        (
            "settings.toml",
            'theme = "base"\n[agent]\nname = "atlas"\n',
            'theme = "overlay"\n[agent]\nrole = "reviewer"\n',
            ['theme = "overlay"', 'name = "atlas"', 'role = "reviewer"'],
        ),
        (
            "settings.yml",
            "theme: base\nagent:\n  name: atlas\n",
            "theme: overlay\nagent:\n  role: reviewer\n",
            ["theme: overlay", "name: atlas", "role: reviewer"],
        ),
        (
            "settings.json",
            '{\n  "theme": "base",\n  "agent": {"name": "atlas"}\n}\n',
            '{\n  "theme": "overlay",\n  "agent": {"role": "reviewer"}\n}\n',
            ['"theme": "overlay"', '"name": "atlas"', '"role": "reviewer"'],
        ),
    ],
)
def test_overlay_deep_merges_tier_a_structured_files_with_overlay_wins(
    tmp_path: Path,
    relative_path: str,
    current_text: str,
    other_text: str,
    expected_snippets: list[str],
) -> None:
    ancestor = tmp_path / "ancestor"
    current = tmp_path / "current"
    other = tmp_path / "other"
    ancestor.write_text("")
    current.write_text(current_text)
    other.write_text(other_text)

    invoke_driver(
        "overlay-deep",
        ancestor,
        current,
        other,
        relative_path,
        source_overlay=OverlayRef(author="atlas", name="theme-dark"),
        trusted_overlay_sources={"refs/overlays/atlas/theme-dark"},
    )

    merged_text = current.read_text()
    for snippet in expected_snippets:
        assert snippet in merged_text


def test_overlay_prepend_writes_overlay_before_base(tmp_path: Path) -> None:
    ancestor = tmp_path / "ancestor"
    current = tmp_path / "current"
    other = tmp_path / "other"
    ancestor.write_text("")
    current.write_text("base line 1\nbase line 2\n")
    other.write_text("overlay line 1\noverlay line 2\n")

    invoke_driver(
        "overlay-prepend",
        ancestor,
        current,
        other,
        "COMPOSE.md",
        source_overlay=OverlayRef(author="atlas", name="compose-overlay"),
        trusted_overlay_sources={"refs/overlays/atlas/compose-overlay"},
    )

    assert current.read_text() == "overlay line 1\noverlay line 2\nbase line 1\nbase line 2\n"


def test_overlay_union_dedupes_duplicates_while_preserving_unique_lines(tmp_path: Path) -> None:
    ancestor = tmp_path / "ancestor"
    current = tmp_path / "current"
    other = tmp_path / "other"
    ancestor.write_text("")
    current.write_text("line-a\nshared\nline-b\n")
    other.write_text("shared\nline-c\nline-b\n")

    invoke_driver(
        "overlay-union",
        ancestor,
        current,
        other,
        "COMPOSE.md",
        source_overlay=OverlayRef(author="atlas", name="compose-overlay"),
        trusted_overlay_sources={"refs/overlays/atlas/compose-overlay"},
    )

    assert current.read_text() == "line-a\nshared\nline-b\nline-c\n"


def test_driver_invocation_refuses_unallowlisted_overlay_source(tmp_path: Path) -> None:
    ancestor = tmp_path / "ancestor"
    current = tmp_path / "current"
    other = tmp_path / "other"
    ancestor.write_text("")
    current.write_text("theme = \"base\"\n")
    other.write_text("theme = \"overlay\"\n")

    with pytest.raises(PermissionError):
        invoke_driver(
            "overlay-deep",
            ancestor,
            current,
            other,
            "settings.toml",
            source_overlay=OverlayRef(author="mallory", name="malicious"),
            trusted_overlay_sources={"refs/overlays/atlas/theme-dark"},
        )

    assert current.read_text() == 'theme = "base"\n'


def test_driver_invocation_refuses_unknown_driver_even_if_requested(tmp_path: Path) -> None:
    ancestor = tmp_path / "ancestor"
    current = tmp_path / "current"
    other = tmp_path / "other"
    ancestor.write_text("")
    current.write_text("safe\n")
    other.write_text("unsafe\n")

    with pytest.raises(ValueError):
        invoke_driver(
            "overlay-rce",
            ancestor,
            current,
            other,
            "settings.toml",
            source_overlay=OverlayRef(author="atlas", name="theme-dark"),
            trusted_overlay_sources={"refs/overlays/atlas/theme-dark"},
        )

    assert current.read_text() == "safe\n"

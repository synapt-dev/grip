"""docs/vocabulary.json: the words gr2 uses, each with the command that does it.

Three things are held here. The file carries only its public fields, so nothing meant for another
reader can ride in it. Every command it names is a live, visible command in ``api/cli.api``, the
committed dump of gr2's surface. And ``docs/GLOSSARY.md`` is exactly what the generator writes from it.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

GR2 = Path(__file__).resolve().parents[1]
VOCABULARY = GR2 / "docs" / "vocabulary.json"
GLOSSARY = GR2 / "docs" / "GLOSSARY.md"
CLI_API = GR2 / "api" / "cli.api"
GENERATOR = GR2 / "scripts" / "gen_vocabulary_glossary.py"

MAX_MEANING_WORDS = 25
MAX_NOTE_WORDS = 40
REQUIRED = {"term", "meaning", "gr2"}
ALLOWED = REQUIRED | {"note"}
EARLIER_NAMES = {"lane bind", "review bind", "review approve", "review rebind"}


def _load() -> dict:
    return json.loads(VOCABULARY.read_text())


def _verbs(cli_api_text: str) -> tuple[set[str], set[str]]:
    """(visible, hidden) command paths from an ``api/cli.api`` dump: ``verb  review pin   stable``."""
    visible: set[str] = set()
    hidden: set[str] = set()
    for line in cli_api_text.splitlines():
        if not line.startswith("verb "):
            continue
        body = re.split(r"\s{2,}", line[5:].strip())[0]
        if body.endswith("(hidden)"):
            hidden.add(body[: -len("(hidden)")].strip())
        else:
            visible.add(body)
    return visible, hidden


def _exists(command: str, visible: set[str]) -> bool:
    """A command exists when it is a visible verb, or a group that has a visible verb under it."""
    return command in visible or any(v.startswith(command + " ") for v in visible)


def _generator():
    spec = importlib.util.spec_from_file_location("gen_vocabulary_glossary", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dump_parser_sees_visible_and_hidden_commands():
    # Control: a parser that returned nothing would make every command-exists row below pass for the wrong reason.
    visible, hidden = _verbs(CLI_API.read_text())
    assert "review pin" in visible and "lane create" in visible
    assert "lane current" in hidden and "lane current" not in visible
    assert _exists("review", visible) and not _exists("review nonesuch", visible)


def test_the_file_holds_only_public_fields():
    data = _load()
    assert data["audience"] == "public"
    assert set(data) == {"audience", "about", "terms"}, set(data)
    assert data["terms"], "the vocabulary has no terms"
    for entry in data["terms"]:
        name = entry.get("term")
        assert REQUIRED <= set(entry), f"{name!r} lacks {sorted(REQUIRED - set(entry))}"
        assert set(entry) <= ALLOWED, f"{name!r} carries fields outside {sorted(ALLOWED)}: {sorted(set(entry) - ALLOWED)}"
        assert isinstance(name, str) and name == name.strip() and name, f"bad term {name!r}"
        assert isinstance(entry["meaning"], str) and 1 <= len(entry["meaning"].split()) <= MAX_MEANING_WORDS, name
        assert "note" not in entry or len(entry["note"].split()) <= MAX_NOTE_WORDS, name
        assert isinstance(entry["gr2"], list) and all(isinstance(c, str) and c for c in entry["gr2"]), name


def test_each_term_is_defined_once():
    names = [entry["term"].lower() for entry in _load()["terms"]]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})


def test_every_command_it_names_is_a_live_visible_command():
    visible, hidden = _verbs(CLI_API.read_text())
    bad = []
    for entry in _load()["terms"]:
        for command in entry["gr2"]:
            if command in EARLIER_NAMES:
                bad.append(f"{entry['term']}: `gr2 {command}` is an earlier name, name the current command")
            elif command in hidden and command not in visible:
                bad.append(f"{entry['term']}: `gr2 {command}` is hidden")
            elif not _exists(command, visible):
                bad.append(f"{entry['term']}: `gr2 {command}` is not in api/cli.api")
    assert not bad, "\n  ".join(["the vocabulary names commands gr2 does not have:"] + bad)


def test_the_glossary_is_what_the_generator_writes():
    expected = _generator().render(_load())
    assert GLOSSARY.read_text() == expected, "docs/GLOSSARY.md is out of date: run scripts/gen_vocabulary_glossary.py"


def test_the_generator_check_agrees():
    result = subprocess.run([sys.executable, "-I", str(GENERATOR), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_readme_points_at_the_glossary():
    assert "docs/GLOSSARY.md" in (GR2 / "README.md").read_text()

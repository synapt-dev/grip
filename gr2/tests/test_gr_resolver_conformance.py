"""Run the shared `gr` resolver table against this package's resolver module, launched as a separate process.

No rule lives here: the runner builds each row's layout and stub binaries, runs the installed `gr`, and compares
the tool, the stderr line count, the `--which` text and the exit code. A skipped row is a red row: the run prints
`ran N of M rows` and fails unless N equals M minus the rows the table declares not for this half.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

TABLE = Path(__file__).resolve().parents[2] / "conformance" / "gr-resolver" / "cases.toml"
CONSUMER = "python"
MARKERS = (".gitgrip", "grip.toml", ".grip/workspace_spec.toml")
STUB = (
    '#!/bin/sh\nprintf "STUB {name}"; for a in "$@"; do printf " %s" "$a"; done; printf "\\n"; '
    'printf "GR_RESOLVED=%s\\n" "${{GR_RESOLVED:-}}"; exit "${{STUB_EXIT:-0}}"\n'
)


def _launcher(directory: Path, name: str) -> Path:
    """A script that runs the resolver module in a new process under the given command name.

    The package installs no `gr` command yet, so the resolver is launched through the interpreter; the name is
    set as argv[0] so a row that invokes it as `gr1` or `gr2` reaches the same code path a real link would. `-I`
    keeps the current directory off the import path: a layout that is itself a gr2 checkout holds a `gr2/` directory
    that would otherwise shadow the installed package.
    """
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -I -c "import sys; sys.argv[0]=\'{name}\'; '
        'from gr2.python_cli.gr_resolver import main; sys.exit(main())" "$@"\n'
    )
    script.chmod(0o755)
    return script


def _gr_under_test(directory: Path) -> Path:
    import gr2.python_cli.gr_resolver as module

    print(f"resolver under test: {module.__file__} (interpreter {sys.executable})")
    return _launcher(directory, "gr")


def _materialize(root: Path, layout: list[str]) -> None:
    for entry in layout:
        if " -> " in entry:
            link, target = entry.split(" -> ")
            (root / link).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(root / target, root / link)
        elif entry.endswith("/"):
            (root / entry).mkdir(parents=True, exist_ok=True)
        else:
            (root / entry).parent.mkdir(parents=True, exist_ok=True)
            (root / entry).write_text("")


def _which_norm(out: str, realroot: str) -> str:
    parts = out.strip().split(" ", 2)
    if len(parts) != 3:
        return out.strip()
    kind, where, binary = parts
    rel = where if where == "none" else os.path.relpath(where, realroot)
    return f"{kind} {rel} {os.path.basename(binary)}"


def _tool_of(stdout: str) -> str | None:
    first = (stdout.splitlines() or [""])[0]
    m = re.match(r"STUB (gr1|gr2)\b", first) or re.match(r"(gr1|gr2)\b", first)
    if m:
        return m.group(1)
    m = re.match(r"gr (\d)\.", first)
    return f"gr{m.group(1)}" if m else None


def run_table(gr: Path, table: Path = TABLE) -> tuple[int, int, list[str], list[tuple[str, list[str]]]]:
    data = tomllib.loads(table.read_text())
    cases = data["case"]
    assert data["count"] == len(cases), f"table says count={data['count']} but has {len(cases)} rows"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate row id"
    base = Path(tempfile.mkdtemp(prefix="gr-conf-")).resolve()
    try:
        ancestor = base
        while True:  # a stray marker above the scratch root would decide every "none" row
            for marker in MARKERS:
                assert not (ancestor / marker).exists(), f"HERMETIC GUARD: {ancestor / marker} exists above {base}"
            if ancestor == ancestor.parent:
                break
            ancestor = ancestor.parent
        ran, red, excluded = 0, [], []
        for i, c in enumerate(cases):
            if CONSUMER not in c.get("applies_to", ["oracle", "rust", "python"]):
                excluded.append(f"{c['id']} (applies_to {c['applies_to']})")
                continue
            if c.get("unix_only") and os.name == "nt":
                excluded.append(f"{c['id']} (unix_only)")
                continue
            root = base / f"r{i}"
            root.mkdir()
            realroot = os.path.realpath(root)
            binp = base / f"bin{i}"
            binp.mkdir()
            for name in c.get("installed", ["gr1", "gr2"]):
                (binp / name).write_text(STUB.format(name=name))
                (binp / name).chmod(0o755)
            _materialize(root, c["layout"])
            path = str(binp)
            if c.get("expect_which_stderr"):
                decoy = base / f"decoy{i}"
                decoy.mkdir()
                (decoy / "gr").write_text("#!/bin/sh\necho decoy\n")
                (decoy / "gr").chmod(0o755)
                path += f":{decoy}"
            home = base / f"home{i}"
            home.mkdir()
            env = {"HOME": str(home), "PATH": path + ":/usr/bin:/bin", **c.get("env", {})}
            exe = gr
            if c.get("argv0"):
                link = base / f"argv{i}"
                link.mkdir()
                exe = _launcher(link, c["argv0"])
            cwd = root / c["cwd"]
            fails: list[str] = []

            def run(args: list[str]) -> subprocess.CompletedProcess:
                return subprocess.run([str(exe), *args], cwd=cwd, env=env, capture_output=True, text=True)

            r = run(c.get("args", ["--version"]))
            lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
            if c["expect_tool"] == "refuse":
                if r.returncode != c.get("expect_rc"):
                    fails.append(f"rc {r.returncode} != {c.get('expect_rc')}")
            else:
                got = _tool_of(r.stdout)
                if got != c["expect_tool"]:
                    fails.append(f"tool {got!r} != {c['expect_tool']!r} (rc {r.returncode}, stderr {r.stderr.strip()[:80]!r})")
                if len(lines) != c["expect_lines"]:
                    fails.append(f"stderr lines {len(lines)} != {c['expect_lines']} ({lines[:2]})")
                if r.returncode != c.get("expect_rc", 0):
                    fails.append(f"rc {r.returncode} != {c.get('expect_rc', 0)}")
            for sub in c.get("expect_stderr", []):
                if sub not in r.stderr:
                    fails.append(f"stderr missing {sub!r} (got {r.stderr.strip()[:100]!r})")
            for sub in c.get("expect_stdout", []):
                if sub not in r.stdout:
                    fails.append(f"stdout missing {sub!r} (got {r.stdout.strip()[:100]!r})")
            if c.get("expect_which"):
                w = run(["--which"])
                got_which = _which_norm(w.stdout, realroot)
                if got_which != c["expect_which"]:
                    fails.append(f"--which {got_which!r} != {c['expect_which']!r} (rc {w.returncode})")
                for sub in c.get("expect_which_stderr", []):
                    if sub not in w.stderr:
                        fails.append(f"--which stderr missing {sub!r} (got {w.stderr.strip()[:100]!r})")
            ran += 1
            if fails:
                red.append((c["id"], fails))
        return ran, len(cases), excluded, red
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_gr_resolver_table_runs_green_through_the_module() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="gr-launcher-")).resolve()
    try:
        ran, total, excluded, red = run_table(_gr_under_test(scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"ran {ran} of {total} rows; excluded: {', '.join(excluded) if excluded else 'none'}")
    assert ran == total - len(excluded), f"ran {ran} but expected {total - len(excluded)}: a row was skipped silently"
    detail = "\n".join(f"RED {rid}\n    " + "\n    ".join(fails) for rid, fails in red)
    assert not red, detail


def test_the_package_declares_no_gr_console_script_yet() -> None:
    """A `gr` command installed by this package would shadow an existing gr1 on a user's PATH; the entry point
    ships with the Rust half, so until then the package must not declare one."""
    scripts = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["scripts"]
    assert "gr" not in scripts, f"pyproject declares a gr console script: {scripts['gr']}"

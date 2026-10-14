"""S3b mutants: each edits ONE line of the build (asserted to apply exactly once), runs its named row in both store
modes, and must turn it red; the file is restored whatever happens. Run from gr2/: python tests/s3b/mutants.py"""
import subprocess, sys
from pathlib import Path

CLI, SH = Path("gr2/python_cli/grip_cli.py"), Path("gr2/python_cli/shadow.py")
ONLY = sys.argv[1:]
MUTANTS = [
    ("skip-walk", CLI, "rewound = _member_rewinds(root, tip, leases[_shadow_ref(root)], list(allow_rewind or []))",
     "rewound = []", "b2"),
    ("rewind-passes", CLI, 'if kind in ("rewind", "rewrite"):', 'if kind in ("rewrite",):', "b2"),
    ("rewrite-passes", CLI, 'if kind in ("rewind", "rewrite"):', 'if kind in ("rewind",):', "b3"),
    ("allow-takes-all", CLI, "if name not in allow_rewind:", "if not allow_rewind:", "b5"),
    ("no-fetch", SH, 'got = gitops.run(repo, "fetch", "-q", "--no-tags", remote, sha)',
     'got = gitops.run(repo, "rev-parse", "HEAD")', "b6"),
    ("unverifiable-passes", CLI, "        except shadow_mod.CannotVerify as exc:\n            raise NativeStoreRefusal(f\"{exc}; nothing was pushed\", 5) from exc",
     "        except shadow_mod.CannotVerify:\n            continue", "b7"),
]
killed = 0
RUN = [m for m in MUTANTS if not ONLY or m[0] in ONLY]
for name, path, old, new, row in RUN:
    src = path.read_text()
    assert src.count(old) == 1, f"{name}: anchor not found once"
    try:
        path.write_text(src.replace(old, new))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_store_push_s3b.py", "-k", f"test_{row}_"],
                           capture_output=True, text=True)
        red = "failed" in r.stdout.splitlines()[-1] if r.stdout.strip() else False
    finally:
        path.write_text(src)
    killed += red
    print(("KILLED" if red else "SURVIVED"), name, f"({row}):", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:], flush=True)
print(f"s3b mutants: {killed}/{len(RUN)} killed")

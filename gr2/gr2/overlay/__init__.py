"""gr2 overlay substrate: config-overlay capture, composition, and materialization.

M1 scope: Tier A only (config files), eager materialization, trust-gated.

The distribution version is single-sourced from the package metadata (the static
number in gr2/pyproject.toml); this module carries no ``__version__`` literal so a
stale third number cannot drift here. Read it with
``importlib.metadata.version("gitgrip")`` or ``gr2 --version``.
"""

# gr2 platform adapter protocol: changes

This file ships inside the `gitgrip` wheel and sdist, so an adapter author can read it from the installed
package:

```python
from importlib.resources import files
print((files("gr2") / "PLATFORM_ADAPTER_CHANGELOG.md").read_text())
```

It covers what an out-of-tree adapter implements or receives: the `PlatformAdapter` protocol in
`gr2.python_cli.platform`, the request and result types passed through it, and the registration seam.
Each entry names the release that first carried it. The full release notes for every change are in the
project's `CHANGELOG.md` and its GitHub releases.

`PLATFORM_ADAPTER_API_VERSION` is the protocol's version number. It is `1`, and it changes only for a change
an existing adapter cannot absorb.

## 2.0.0a8

- **`CreatePRRequest.remote`** is new (default `None`): the exact Git URL configured for the selected
  member in the workspace spec or review bind. Created PR groups retain it as `prs[].remote` when it is
  supplied. Direct Python callers may omit it and keep the previous stored-group shape. This is additive
  to adapter API version 1; factories still take no arguments and `repo` keeps its existing value.
  Later lifecycle calls still receive the existing repo string; this field supplies creation context only.

## 2.0.0a7

- **Registration.** Adapters register through the `gr2.platform_adapters` entry point group
  (`PLATFORM_ADAPTER_ENTRY_POINTS`), or in-process with `register_platform_adapter(name, factory)`, where
  `factory` takes no arguments and returns the adapter. A factory is loaded only when its platform is
  selected. Names are lowercase (`[a-z][a-z0-9_-]*`); `github` and `gh` are reserved, and a name registered
  twice is refused.
- **`PLATFORM_ADAPTER_API_VERSION = 1`.** An adapter may set `platform_adapter_api_version`; when the adapter
  is loaded, gr2 refuses one that declares any other value.
- **`require_adapter_capability(adapter, method)`.** A verb that needs a method the adapter does not
  provide now fails with a named refusal before any member is changed, instead of a `TypeError` or
  `AttributeError` mid-run. Every adapter must provide `create_pr` when it is loaded. **`merge_pr` must
  accept the `method` and `expected_head` keywords**; an adapter whose `merge_pr` does not is refused rather
  than called without the head pin.
- **`CreatePRRequest.draft` defaults to `True`** (it was `False`). A pull request opens as a draft unless
  the caller asks otherwise, so an adapter must honour `draft` rather than assume a ready PR.

## 2.0.0a6

- **`pr_view(repo, number) -> PRDetail`** is new: one PR's full state. `PRDetail` carries `ref`, `state`,
  `body`, `author`, `labels`, `review_decision`, `reviews`, `head_oid`, `is_draft`, `merged`, `mergeable` and
  `checks`.
- **New type `PRReview`**, one review on a PR: `user` and `state`.
- **`merge_pr(..., *, method, expected_head=None)`** gains `expected_head`. When it is given, the adapter
  must merge only if the PR's head is still that commit, and refuse otherwise; `gr2 pr merge
  --match-head-commit` relies on it.
- **`PRStatus.head_oid`** is new (default `None`): the PR's current head commit, which gr2 compares against
  the reviewed head.

## 2.0.0a5

- **`edit_pr_body(repo, number, body) -> None`** is new: replaces a PR's description. gr2 uses it to keep a
  grouped PR set's cross-links current.

## 2.0.0a1 to 2.0.0a4

The protocol as first published: `name`, `create_pr(CreatePRRequest) -> PRRef`, `merge_pr(repo, number, *,
method) -> MergeReceipt`, `pr_status(repo, number) -> PRStatus`, `list_prs(repo, *, head_branch=None)`, and
`pr_checks(repo, number) -> list[PRCheck]`. No protocol change between these four releases.

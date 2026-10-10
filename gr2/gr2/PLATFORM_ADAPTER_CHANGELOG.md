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

`PLATFORM_ADAPTER_API_VERSION` is the protocol's version number. It changes only for a change an existing
adapter cannot absorb. The released version is `1`; version `2` is in this file's Unreleased section and no
release carries it yet.

## Unreleased (adapter API version 2)

Nothing here is in a gr2 release. An adapter that declares no `platform_adapter_api_version` is a version 1
adapter and is called exactly as before.

- **`PLATFORM_ADAPTER_API_VERSION = 2`.** gr2 accepts a declared `1` or `2` and refuses any other value.
  `adapter_api_version(adapter)` returns the declaration, and `1` when there is none.
- **`RemoteTarget`** is new: `raw` (the exact configured Git URL) and the adapter's reading of it, `host`,
  `org`, `project` and `repo`, each `None` when the platform has no such part. gr2 never parses a hosting URL.
- **`resolve_target(remote) -> RemoteTarget`** is new and is required of a version 2 adapter: it is the only
  place a URL is parsed.
- **`CreatePRRequest.target`** is new (default `None`): the member's `RemoteTarget`, resolved by the adapter
  before the first PR of a group is created. A version 2 adapter always receives it; a version 1 adapter
  receives `None`. `CreatePRRequest.remote` is unchanged and still carries the raw URL.
- **`target` keyword on the lifecycle calls.** For a version 2 adapter gr2 passes `target=<that member's
  RemoteTarget>` to `pr_status`, `merge_pr`, `edit_pr_body`, `pr_checks`, `pr_view` and `list_prs`, so the adapter routes by the member's own
  target and not by a repo string. A version 1 adapter is never passed `target`.
- **Old groups are refused by name.** A version 2 adapter given a stored group whose member has neither a
  `target` nor a `remote` is refused as `group_lacks_routing_context`, before any adapter call; the group is
  re-created with `gr2 pr create`. A member whose stored remote now resolves to a different target than the
  one stored is refused as `routing_target_changed`.
- **Review verbs.** `review open` reads the PR's base branch through the adapter's own target when the adapter
  is version 2 (`pr_status(..., target=<origin's RemoteTarget>)`), and refuses by name when the repository has
  no origin url; a version 1 adapter is read exactly as before. Review creation resolves each member through
  `resolve_target` and requires the effective URL (after any `url.<base>.insteadOf` rewrite) to resolve to the
  same `(host, org, project, repo)` as the bound one.
- **Selection refuses a version 2 adapter without `resolve_target`** by name (`platform adapter lacks required
  resolve_target capability`), at the same point it refuses one without `create_pr`.

## 2.0.0a8

- **`CreatePRRequest.remote`** is new (default `None`): the exact Git URL configured for the selected
  member in the workspace spec or review bind. Created PR groups retain it as `prs[].remote` when it is
  supplied. Direct Python callers may omit it and keep the previous stored-group shape. This is additive
  to adapter API version 1; factories still take no arguments and `repo` keeps its existing value.
  Later lifecycle calls still receive the existing repo string; this field supplies creation context only.
  Lane URLs are stripped of surrounding whitespace; blank values are treated as absent.
  A lane member URL carrying credentials is refused by member name before adapter calls or PR state writes.

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

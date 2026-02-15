# Platform Capabilities

gitgrip supports four hosting platforms. All platforms support core PR operations.
Optional capabilities vary by platform.

## Capability Matrix

| Capability | GitHub | GitLab | Azure DevOps | Bitbucket |
|---|:---:|:---:|:---:|:---:|
| **Core PR Operations** | | | | |
| Create PR | Yes | Yes | Yes | Yes |
| Merge PR | Yes | Yes | Yes | Yes |
| Find PR by branch | Yes | Yes | Yes | Yes |
| Get reviews | Yes | Yes | Yes | Yes |
| Status checks | Yes | Yes | Yes | Yes |
| Merge methods | Yes | Yes | Yes | Yes |
| PR diff | Yes | Yes | Yes | Yes |
| **Optional Operations** | | | | |
| Update branch (merge base) | Yes | - | - | - |
| Auto-merge | Yes | - | - | - |
| Create repository | Yes | Yes | Yes | - |
| Delete repository | Yes | Yes | Yes | - |
| Create release | Yes | - | - | - |

## Notes

- **Update branch**: Merges the base branch into the PR head branch via API. Only GitHub supports this natively.
- **Auto-merge**: Automatically merges the PR when all required checks pass. GitHub-only via `gh` CLI.
- **Create/Delete repository**: API-driven repo management. Bitbucket adapter does not implement this.
- **Create release**: Creates a tagged release with release notes. GitHub-only via Octocrab API.

## Checking Capabilities Programmatically

```rust
use gitgrip::platform::capabilities::{platform_supports, PlatformCapability};
use gitgrip::core::manifest::PlatformType;

if platform_supports(PlatformType::GitHub, PlatformCapability::AutoMerge) {
    // Enable auto-merge
}
```

## Adding a New Platform

1. Create adapter in `src/platform/newplatform.rs`
2. Implement `HostingPlatform` trait (14 required methods, 5 optional)
3. Add detection logic in `src/platform/mod.rs`
4. Update `platform_capabilities()` in `src/platform/capabilities.rs`
5. Update this table

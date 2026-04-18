## Summary

<!-- What does this PR do? 1-3 sentences. -->

## Closes

<!-- Link issues this PR resolves. Use "Closes #N" for auto-close on merge. -->
<!-- Example: Closes #123, Closes #456 -->

## Premium Boundary

**Premium boundary**: <!-- e.g. "grip is OSS. This PR adds workspace orchestration, not identity or org." -->

### Boundary Checklist

- [ ] **Boundary declaration** present above (one-line: what repo, OSS or premium, why)
- [ ] **Identity test**: does this PR answer "who is this agent?" or "what workspace is this?" If yes, it must go in `synapt-private`.
- [ ] **Plugin seam**: if this extends OSS for a premium feature, is the extension seam built in OSS first, with the implementation in `synapt-private`?

## Test Plan

<!-- How was this tested? What should reviewers verify? -->

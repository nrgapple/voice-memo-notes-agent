# Releasing

Public releases distribute source code. The default bootstrap builds and signs the app locally so macOS privacy approvals remain tied to the user's installation. Do not distribute the locally signed app bundle or its signing material.

1. Update `VERSION` and `CHANGELOG.md`.
2. Run `python3 -m pip install -r requirements-dev.txt`.
3. Run `./scripts/run_harness.sh fixture`.
4. On a configured Mac, run bootstrap twice, `./scripts/run_harness.sh live`, and a private live canary following `references/improvement-harness.md`.
5. Confirm secret scanning and dependency review are clean.
6. Merge through review, tag `v$(cat VERSION)`, and create GitHub release notes from the changelog.

A prebuilt public app requires a separate Developer ID signing and Apple notarization workflow. Until that exists, releases must remain source-only and must not claim Gatekeeper-ready binary distribution.

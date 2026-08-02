# Releasing

Public releases distribute source plus ad hoc-signed Apple Silicon, Intel, and universal CI builds. The default bootstrap still builds and signs the app locally so macOS privacy approvals remain tied to the user's installation. Do not distribute the locally signed app bundle or its signing material.

1. Update `VERSION` and `CHANGELOG.md`.
2. Run `python3 -m pip install -r requirements-dev.txt`.
3. Run `./scripts/build_icon.sh`, `./scripts/run_harness.sh fixture`, and `./scripts/build_release.sh <empty-output-directory>`.
4. On a configured Mac, run bootstrap twice, `./scripts/run_harness.sh live`, and a private live canary following `references/improvement-harness.md`.
5. Confirm secret scanning and dependency review are clean.
6. Merge through review, tag `v$(cat VERSION)`, and create GitHub release notes from the changelog. The `Release Builds` workflow attaches architecture archives, checksums, and build-provenance attestations when the release is published; it can also be dispatched for an existing release tag.

A Gatekeeper-ready public app requires a separate Developer ID signing and Apple notarization workflow. Until that exists, release archives must be described as ad hoc-signed CI builds and must not claim production binary distribution.

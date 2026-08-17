# Code signing policy

## Provider

Free code signing provided by SignPath.io, certificate by SignPath Foundation

Provider links: [SignPath.io](https://signpath.io/) · [SignPath Foundation](https://signpath.org/)

SignPath Foundation approval is still pending. Until approval and final signature verification are complete, QuizForge does not publish an unsigned Windows installer as a public release and does not enable the one-click update manifest.

## Signed artifacts and build provenance

Only Windows artifacts for the [QuizForge repository](https://github.com/lhd23333/QuizForge) may be submitted under this project. Candidate artifacts must be produced from a public commit by [the repository's GitHub Actions workflow](https://github.com/lhd23333/QuizForge/actions/workflows/windows-release-candidate.yml), using the build scripts and locked dependencies stored in the repository.

The intended signed artifacts are `QuizForge.exe` and `QuizForge-X.Y.Z-Setup.exe`. Third-party upstream binaries such as Pandoc may be included unchanged with their license and corresponding source, but must not be signed with the QuizForge project certificate. Every signing request requires manual approval, and the final downloadable Setup must be checked again for version metadata, SHA-256, Authenticode validity and signer identity before publication.

The current workflow deliberately uploads an unsigned, short-retention candidate only for build verification and SignPath onboarding. It does not create a GitHub Release. The SignPath signing steps and final release workflow will be added only after SignPath assigns the project and signing configuration.

## Team roles

QuizForge currently has one maintainer, so the same trusted person holds the required roles:

- Committer and reviewer: [lhd23333](https://github.com/lhd23333)
- Approver: [lhd23333](https://github.com/lhd23333)

The maintainer may directly author trusted repository changes. Contributions from other people must be submitted through a pull request and reviewed before merge. Signing requests are never approved automatically. Accounts used for GitHub and SignPath must have multi-factor authentication enabled.

## Privacy

This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.

The complete networking and data-handling description is available in the [QuizForge privacy policy](../PRIVACY.md). In particular, update checks are user-initiated and do not upload question-bank content, paths or API credentials; OCR and model providers receive documents only when the user explicitly selects and configures those third-party services.

## Incident handling

If a signing key, SignPath account, GitHub workflow or published artifact is suspected to be compromised, distribution and update manifests must be stopped first. The maintainer will preserve evidence, notify SignPath when applicable, request certificate revocation if required, publish a security notice and issue a new signed patch release rather than replacing an existing Release asset in place.

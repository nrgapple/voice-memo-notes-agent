# Security Policy

## Supported versions

Only the latest tagged beta or release is supported. This project is currently a single-user, local macOS automation and is not designed as a multi-tenant service.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that may expose recordings, transcripts, notes, credentials, filesystem access, or GitHub access. Use GitHub's private vulnerability reporting for this repository. If that feature is unavailable, open an issue containing no vulnerability details and ask the maintainers to establish a private channel.

Include the affected version or commit, impact, reproduction steps using synthetic data, and suggested mitigation when known. Never attach real audio, transcripts, private notes, tokens, or local state.

## Security boundaries

- Audio and transcription happen locally before qualification.
- A qualified transcript and capped note context are sent to Codex for one structured planning call.
- Codex runs ephemerally in a tiny read-only workspace with user rules and MCP configuration disabled.
- Deterministic code validates paths, links, additive changes, provenance, Git state, and publication.
- Fresh installations publish review branches by default. Direct publishing is an explicit trust choice.
- Pushover receives minimal metadata and never receives transcript or note contents.

The notes repository and local macOS account remain trusted inputs. Do not use an untrusted or shared vault without an additional isolation layer.

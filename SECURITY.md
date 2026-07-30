# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature if it is enabled for this repository.
If a private reporting channel is added later, use that channel instead of opening a public issue.

Do not post secrets, tokens, or private manuscripts in issues or pull requests.

## Supported versions

StoryForge is currently a public work in progress. Security fixes will be prioritized for the current pre-release line and any later stable release once one exists.

## Sensitive data handling

- Treat provider credentials as secrets.
- Keep manuscript content private unless it is explicitly synthetic or redacted.
- Do not upload generated audio or sidecars that might contain private content.
- If a secret was ever committed, rotate it immediately.

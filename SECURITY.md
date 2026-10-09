# Security policy

Mobster acts on a real phone with your accounts on it, so security reports get priority.

## Report a vulnerability

Report it privately through GitHub: [open a security advisory](https://github.com/RadishSoftware/mobster/security/advisories/new) for this repository. Only the maintainers can see it. Do not open a public issue for a vulnerability.

Include the version (`mobster --version`), what an attacker can do, and the steps to reproduce it. Leave out real keys, journals and screenshots of personal screens. We reply in the advisory, work on the fix there with you, and credit you when it is published unless you ask us not to.

## Supported versions

Fixes go into the latest release and `main`. Mobster CLI is at 0.x, so older releases do not get separate fixes.

## What counts

These are in scope:

- Anything that lets someone other than you make Mobster act on your phone, such as reaching the local API or WebDriverAgent from another machine, or getting past its API token.
- Getting Mobster to take an action that needs your approval without asking, while "Ask before acting" is on.
- Leaking keys, API tokens, journals or screen contents to anyone other than the model providers you configured.
- Getting past the device lock, so that two Mobster processes control one phone at once.

These are known properties rather than vulnerabilities:

- WebDriverAgent has no authentication of its own. Mobster binds it to `127.0.0.1` over USB. Exposing it on a network is a configuration you chose.
- What Mobster reads on the screen goes to Jev and to your helper model, as [the README](https://github.com/RadishSoftware/mobster/blob/main/README.md#how-it-works) describes.
- An app's content can include text that tries to steer the agent (prompt injection). Report a case where that text gets Mobster past a guard or an approval.

## How Mobster protects you

- The local API listens only on `127.0.0.1`, checks that requests come from a local origin, and needs a random token that is new for each launch.
- Keys live in env files you create with mode 600. The API returns only the last four characters of a key.
- Every action is written to the journal before it is sent, and an action with an unknown outcome is never retried.
- "Ask before acting" is on by default, and text that Mobster types is never written to the journal.

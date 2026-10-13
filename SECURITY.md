# Security policy

The engine acts on a real phone with your accounts on it, so security reports get priority.

## Report a vulnerability

Report it privately through GitHub: [open a security advisory](https://github.com/RadishSoftware/mobster/security/advisories/new) for this repository. Only the maintainers can see it. Don't open a public issue for a vulnerability.

Include the commit (`SOURCE_REV`), what an attacker can do, and the steps to reproduce it. Leave out real keys, journals and screenshots of personal screens. We reply in the advisory, fix it there with you, and credit you when it is published unless you ask us not to.

## What counts

- Getting the engine to take an action that needs your approval (a send, buy, post or delete) without asking.
- Leaking keys or screen contents to anyone other than the model provider whose key you set.
- Getting past the device lock, so that two runs control one phone at once.

These are known properties rather than vulnerabilities:

- WebDriverAgent has no authentication of its own. Mobster for Mac binds it to `127.0.0.1` over USB. A runner you expose on a network is a configuration you chose.
- What the engine reads on the screen goes to the model on your key.
- An app's content can include text that tries to steer the agent (prompt injection). Report a case where that text gets the engine past a guard or an approval.

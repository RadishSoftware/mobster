"""A stand-in login Keychain: FakeBridge plays the Security framework, runner /usr/bin/security."""

from types import SimpleNamespace

from mobile_agent import keychain_acl


class FakeBridge:
    def __init__(self):
        # (service, account) -> {"data": bytes, "marked": bool, "trusted": list | None}
        self.items = {}
        self.calls = []  # (method, service, account, data or None)
        self.refuse = set()  # method names that fail with "interaction not allowed"
        self.fail = set()  # method names that fail with another status
        self.prompts_allowed = True
        self.cli = []  # argv lists runner() was given, with their stdin

    def _check(self, name):
        if name in self.refuse:
            raise keychain_acl.BridgeError(-25308)
        if name in self.fail:
            raise keychain_acl.BridgeError(-34018)

    def legacy(self, service, account, secret):
        """An item as `security add-generic-password` made it: trusts /usr/bin/security, no marker."""
        self.items[(service, account)] = {"data": secret.encode(), "marked": False, "trusted": ["/usr/bin/security"]}

    def find(self, service, account, *, marked):
        self.calls.append(("find", service, account, None))
        self._check("find")
        item = self.items.get((service, account))
        if item is None or (marked and not item["marked"]):
            return None
        return item["data"] if marked else b""

    def update(self, service, account, data):
        self.calls.append(("update", service, account, data))
        self._check("update")
        item = self.items.get((service, account))
        if item is None or not item["marked"]:
            return False
        item["data"] = data
        return True

    def add(self, service, account, data, trusted):
        self.calls.append(("add", service, account, data))
        self._check("add")
        if (service, account) in self.items:
            raise keychain_acl.BridgeError(keychain_acl.DUPLICATE)
        self.items[(service, account)] = {"data": data, "marked": True, "trusted": ["<this program>", *trusted]}

    def delete(self, service, account):
        self.calls.append(("delete", service, account, None))
        self._check("delete")
        item = self.items.get((service, account))
        if item is None or not item["marked"]:
            return False
        del self.items[(service, account)]
        return True

    def no_prompts(self):
        self.prompts_allowed = False

    def runner(self, argv, data=None):
        """/usr/bin/security: find-generic-password -w, delete-generic-password and `-i` add-generic-password -U,
        on items that trust it (an item behind the access list answers as security(1) does when refused: 51)."""
        self.cli.append((list(argv), data))
        done = lambda code=0, out=b"", err=b"": SimpleNamespace(returncode=code, stdout=out, stderr=err)  # noqa: E731
        if argv[1:2] == ["-i"]:
            words = (data.decode() if isinstance(data, bytes) else data).split('"')
            service, account, secret = words[1], words[3], words[5]
            item = self.items.get((service, account))
            if item is not None and item["marked"]:
                return done(err=b"SecKeychainItemModifyContent: User interaction is not allowed.")
            self.legacy(service, account, secret)
            return done()
        service, account = argv[argv.index("-s") + 1], argv[argv.index("-a") + 1]
        item = self.items.get((service, account))
        if item is None:
            return done(keychain_acl.CLI_NOT_FOUND)
        if item["marked"]:
            return done(51, err=b"User interaction is not allowed.")
        if argv[1] == "find-generic-password":
            return done(out=item["data"] + b"\n")
        del self.items[(service, account)]
        return done()


def store(service="app.mobster.test", bridge=None,
          trusted=("/Applications/Mobster.app/Contents/MacOS/mobster-desktop",)):
    bridge = bridge or FakeBridge()
    return keychain_acl.Store(service, bridge=bridge, runner=bridge.runner, trusted=trusted), bridge

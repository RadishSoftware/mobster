# Network conditions on a simulator: spike (24 Oct 2026)

Question: on macOS 26.5 with Xcode 26.4 and the iOS 26.4 runtime, what can limit one simulator's traffic and answer
named requests, with no root, no kernel extension and no change to the Mac's own traffic?

A simulator is a set of ordinary Mac processes. They share the Mac's network stack, its routes and its system proxy
settings. There is no network namespace to shape. Every approach below either shapes the Mac or shapes only the
processes it can name.

## What we tried

All runs on one throwaway iPhone 17 Pro simulator (iOS 26.4) made for the spike and deleted after, at load 18 to 49.
The probe was a 30-line UIKit app that fetches a URL through `URLSession.shared`, a `.default` session and an
`.ephemeral` session and logs status, bytes, milliseconds and error code. The proxy and the origin were Python scripts
on `127.0.0.1`; `*.test` hosts were mapped to the origin by the proxy.

| # | Approach | Result |
|---|---|---|
| 1 | `dnctl` + `pfctl` dummynet | Rejected without running it. `pfctl -s info` answers `/dev/pf: Permission denied` without root, so every rule needs root. `pf` can match a local user or group but not "the processes of this simulator": simulator apps run as you, like everything else on the Mac, so a rule shapes the Mac's other traffic too. That breaks the lane's rule. |
| 2 | Network Link Conditioner | Rejected. The profiles are kernel-side, installed by a preference pane that needs an admin password, and nothing scripts them. They shape the whole Mac. |
| 3 | `networksetup -setwebproxy` (the system proxy) | Rejected. It changes every app on the Mac, needs an administrator, and a crash leaves the proxy set. |
| 4 | `HTTP_PROXY` / `http_proxy` / `ALL_PROXY` in the launch environment (`SIMCTL_CHILD_*`) | Does not work. `URLSession` ignored all three: the request failed DNS (`-1003`) and the proxy saw nothing. |
| 5 | A proxy dictionary in the simulator's `defaults` | Not tried. CFNetwork reads proxies from the Mac's SystemConfiguration store, which the simulator shares; there is no per-simulator proxy key to write. |
| 6 | Interpose `CFNetworkCopySystemProxySettings` with `DYLD_INSERT_LIBRARIES` | Does not work. `URLSession` doesn't call it through the symbol we can interpose. |
| 7 | **Inject a 40-line dylib that returns a proxy from `-[NSURLSessionConfiguration connectionProxyDictionary]`** (`SIMCTL_CHILD_DYLD_INSERT_LIBRARIES`) and run a local proxy | **Works.** `URLSession.shared`, `.default` and `.ephemeral` all went through the proxy, for `http://` (the proxy saw `GET http://api.spike.test/a`) and `https://` (the proxy saw `CONNECT example.com:443`, the page loaded: status 200, 577 bytes). No root, nothing installed on the Mac, nothing to undo: the setting lives in the app's environment and ends with its process. |
| 8 | Same dylib set for the whole simulator with `simctl spawn <udid> launchctl setenv` | Works too, and reaches processes Mobster doesn't launch: Mobile Safari and the system daemons (`configuration.apple.com`, `wps.apple.com`, `safebrowsing.googleapis.com` all arrived at the proxy). It stays set until `launchctl unsetenv`, so a crash would leave it: we don't use it by default (see the decision below). |

What the proxy can do to a request, measured in the spike with the probe app:

| Proxy answer | What `URLSession` reports |
|---|---|
| Close the connection | `-1005` (network connection lost) for `http://`; the `https://` case was not measured (a cached response hid it) |
| Reset the connection (RST) | `-1005` for `http://`, `310` (HTTPS proxy connection failed) for `https://` |
| `HTTP/1.1 502` | status 502 for `http://` |
| Forward | status 200 and the full body |

## What the chosen approach covers

Covered: `URLSession` in all its forms (`shared`, default, ephemeral), so Swift and Objective-C apps, Alamofire,
Moya, and React Native `fetch` and `XMLHttpRequest` (React Native's HTTP module is built on `NSURLSession`'s default
configuration; read from its source, not run in this spike). Image loaders built on `URLSession` (Kingfisher, SDWebImage, Nuke).

Not covered, and the docs say so:

- **Reachability stays "online".** `NWPathMonitor`, `SCNetworkReachability` and the status bar read the Mac's link,
  which we don't touch. `offline` resets every connection, so `http://` fails with `-1005` and `https://` with `310`
  (measured with the probe on 26 Oct 2026), never `-1009` (not connected to the internet),
  and an app that shows its offline screen only on a reachability change won't show it. Apps that show it when a
  request fails will.
- **Sockets outside `URLSession`.** Flutter's `dart:io`, gRPC, Cronet, Firebase's own transport, raw `NWConnection`
  and `CFStream` clients, QUIC. The shim doesn't reach them and the run notes that the proxy saw no request.
- **Web views.** WebKit's network process isn't launched with the app's environment. Mode 8 reaches it; mode 7 doesn't.
- **Daemons and other apps**, again only in mode 8.
- **Certificate pinning.** Pass-through HTTPS is a CONNECT tunnel the proxy never opens, so pinned apps keep working
  under `network:`. A `stubs:` entry for a host does need the proxy to present its own certificate for that host, and a
  pinned app refuses it. The check fails with the app's own error, not a silent pass.
- **Apps with a hardened runtime or that block `DYLD_INSERT_LIBRARIES`.** Simulator builds from Xcode don't. An app
  that strips the variable gets no shaping and the run records that no request reached the proxy.

## Decision

Use approach 7: a local proxy per run plus the injected shim, per launch, simulator only.

- **The shim** is Objective-C source in the package (`mobile_agent/verify/netshim.m`), compiled on first use with Xcode's
  `clang` for the simulator (arm64 and x86_64), cached by hash, ad-hoc signed. Nothing precompiled ships.
- **The proxy** is a Python asyncio server on `127.0.0.1` with an ephemeral port, started for the run and stopped
  when the run ends. It applies the profile (latency, bandwidth, stalls, resets), answers stubs, and passes the rest through.
- **State.** Nothing outside the app process and the proxy: no rule, no setting, no file on the Mac. The cleanup the
  lane asks for ("never left behind on failure") is closing a socket. If Mobster dies, the proxy dies with it
  and the app, which is relaunched without the variable, is unshaped.
- **Mode 8 is not used.** It would shape the system daemons and WebKit but it persists past a crash and injects into
  every process of the simulator. If web views turn out to matter it can be added as an opt-in with an explicit unset in `restore`.
- **HTTPS stubs** need a certificate the simulator trusts. `simctl keychain <udid> add-root-cert` does that without
  root, but there is no `remove`: `simctl keychain reset` clears the whole keychain, including the app's. So the run
  makes a fresh CA, deletes its private key when the run ends, and the public certificate stays in the simulator's trust
  store, where it can sign nothing. It's added only when a stub names an `https` host. This is the one thing a run
  leaves behind; it needs the owner's OK (see the PR).
- **No privileged helper is needed.** The pf/dummynet route would need one (the capture helper's pattern), and it would
  shape the Mac, so we didn't design it.

Spike artifacts (probe app, shim, proxy scripts) were in the scratchpad; the shim and proxy are rebuilt as product code
in this branch with tests.

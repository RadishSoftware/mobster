"""WebDriverAgent patches: one gate for screen and accessibility captures, on for simulators and off for iPhones.

WDA's MJPEG broadcaster asks XCTest for a screenshot on its own queue while a request asks for the
app's accessibility snapshot. On iOSWorld simulators an app stopped answering snapshots after a
scroll with the stream running, and stayed stuck until it was closed (scorezone-003, 10 Oct 2026).
The gate keeps the two apart, unless the runner starts with ``MOBSTER_CAPTURE_GATE=off`` in its
environment (``TEST_RUNNER_`` prefix through xcodebuild):

- every accessibility snapshot (``-[XCAXClient_iOS requestSnapshotForElement:...]``, which /source,
  element queries and attribute reads all go through) holds it, waiting at most
  ``FBMobsterSnapshotGateWait`` seconds for it, then running without it, as upstream does;
- every screenshot (``+[FBScreenshot takeInOriginalResolutionWithScreenID:...]``) holds it;
- the MJPEG broadcaster waits for it at most one frame interval, then skips that frame.

With ``off`` nothing is locked, and the runner behaves as upstream's.

Measured cost, 10 Oct 2026, "Frontier Bench 1" simulator, 4 other simulators busy, ABBA, 8 rounds of
60 back-to-back source reads each with the stream at 30 fps:
- read p50 went from 66.5 to 93 ms (median of rounds), and p90 from 82.5 to 127.5 ms;
- the stream went from 26.6 to 18.7 frames a second while reads ran.
A broadcaster that skipped a busy frame at once, instead of waiting a frame interval, sent 1.9 frames
a second instead of 9.6 (stream at 10 fps).

Measured effect on the hang, 11 Oct 2026: the tuning loop's 4-simulator scripted harness (iOSWorld
scorezone-003, a swipe then done, no model), off and on interleaved ABBA, 4 runs per simulator a round.
Off, 10 of 64 runs hung (15.6%, 95% CI 8.7-26.4%). On, 2 of 64 did (3.1%, 0.9-10.7%; Fisher p = 0.03).
On those simulators, reads cost 61.5 -> 80.5 ms at p50 and the stream ran at 22.6 instead of 27.4 fps.
Upstream's own guard (the ``accessibilityDeadline`` session setting, 3 s) hung none of 32 runs the same way,
but 4 failed at 79 s instead: the stuck app's main run loop did not answer, so its reads failed sooner.

Measured on an iPhone, 11 Oct 2026: an iPhone 15 Pro on iOS 26.0.1 over USB, off and on interleaved ABBA.
- 8 rounds of 2 x 60 back-to-back reads of Settings > General, the stream at 30 fps: read p50 went from
  160 to 241.5 ms, p90 from 193.5 to 290 ms; the stream went from 19.2 to 14.7 frames a second.
- A scripted swipe then read on YouTube Shorts, 40 runs a side: no read stalled either way (0 of 40,
  95% CI 0-8.8%). With the gate off, 2 runs failed at once with a transport error (cause not recorded).
  Read p50 went from 86 to 168 ms, and the swipe's from 1025 to 1509 ms.
- 10 short tasks on Smart, 20 runs off and 12 on: 19 of 20 passed off, 10 of 10 graded on; the median
  per-task difference was +0.29 s with the gate on.
The phone paid the cost and showed no hang to prevent, so ``runner_env`` starts iPhone runners with
the gate off and simulator runners with it on. ``MOBSTER_CAPTURE_GATE=on`` or ``off`` in Mobster's own
environment overrides that for both.

Applied with ``patches.WDA_PATCHES`` (anchored, idempotent edits on the pinned release).
"""

import os

# What the runner reads (its environment, through xcodebuild's TEST_RUNNER_ prefix), and the override.
RUNNER_VARIABLE = "TEST_RUNNER_MOBSTER_CAPTURE_GATE"
OVERRIDE = "MOBSTER_CAPTURE_GATE"
# Per device kind: on for simulators (10 of 64 scripted runs hung without it), off for iPhones (no hang in
# 40 runs either way, 81.5 ms more per read with it). Measurements above.
DEFAULTS = {"simulator": "on", "phone": "off"}
WORDS = {"on": "on", "1": "on", "true": "on", "yes": "on", "off": "off", "0": "off", "false": "off", "no": "off"}


def gate_for(kind, environ=None):
    if kind not in DEFAULTS:
        raise ValueError(f"A runner's device kind is one of {', '.join(sorted(DEFAULTS))}, not {kind!r}")
    environ = os.environ if environ is None else environ
    for name in (OVERRIDE, RUNNER_VARIABLE):
        said = WORDS.get(str(environ.get(name) or "").strip().casefold())
        if said:
            return said
    return DEFAULTS[kind]


def runner_env(kind, environ=None):
    """The runner's gate as xcodebuild passes it on; merge it last into every runner's environment."""
    return {RUNNER_VARIABLE: gate_for(kind, environ)}

SNAPSHOT_FILE = "WebDriverAgentLib/Categories/XCAXClient_iOS+FBSnapshotReqParams.m"
SCREENSHOT_FILE = "WebDriverAgentLib/Utilities/FBScreenshot.m"
MJPEG_FILE = "WebDriverAgentLib/Utilities/FBMjpegServer.m"

GATE_DECLARATION = (
    "// Mobster: one gate for screen and accessibility captures (XCAXClient_iOS+FBSnapshotReqParams.m).\n"
    "NSRecursiveLock *FBMobsterCaptureGate(void);\n")

GATE_DEFINITION = """// Mobster: one gate for screen and accessibility captures, unless the runner's environment has
// MOBSTER_CAPTURE_GATE=off (then nil: nothing is locked). A snapshot holds it, waiting at most
// FBMobsterSnapshotGateWait seconds, then running without it; a screenshot holds it; the MJPEG
// broadcaster waits at most one frame interval for it, then skips that frame.
static const NSTimeInterval FBMobsterSnapshotGateWait = 2.0;

NSRecursiveLock *FBMobsterCaptureGate(void)
{
  static NSRecursiveLock *gate;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    if (![NSProcessInfo.processInfo.environment[@"MOBSTER_CAPTURE_GATE"] isEqualToString:@"off"]) {
      gate = [NSRecursiveLock new];
      gate.name = @"mobster.capture-gate";
    }
    [FBLogger logFmt:@"Mobster capture gate: %@", nil == gate ? @"off" : @"on"];
  });
  return gate;
}

static id swizzledRequestSnapshotForElement(id self, SEL _cmd, id element, id attributes,
                                            id parameters, NSError **error)
{
  NSRecursiveLock *gate = FBMobsterCaptureGate();
  BOOL held = nil != gate
    && [gate lockBeforeDate:[NSDate dateWithTimeIntervalSinceNow:FBMobsterSnapshotGateWait]];
  if (nil != gate && !held) {
    [FBLogger log:@"Mobster: a snapshot ran without the capture gate after waiting for a screenshot"];
  }
  @try {
    return FBMobsterUngatedSnapshot(self, _cmd, element, attributes, parameters, error);
  } @finally {
    if (held) {
      [gate unlock];
    }
  }
}

"""

PATCHES = (
    # Snapshots: upstream's swizzle is renamed and wrapped by a gated one with its old name.
    (SNAPSHOT_FILE,
     "static id swizzledRequestSnapshotForElement(id self, SEL _cmd, id element, id attributes, id parameters, "
     "NSError **error)\n{\n",
     "static id FBMobsterUngatedSnapshot(id self, SEL _cmd, id element, id attributes, id parameters, "
     "NSError **error)\n{\n"),
    (SNAPSHOT_FILE,
     "@implementation XCAXClient_iOS (FBSnapshotReqParams)\n",
     GATE_DEFINITION + "@implementation XCAXClient_iOS (FBSnapshotReqParams)\n"),
    # Screenshots hold the gate from the request until its reply or timeout.
    (SCREENSHOT_FILE,
     "#import \"XCUIScreen.h\"\n\nstatic const NSTimeInterval SCREENSHOT_TIMEOUT = 20.;\n",
     "#import \"XCUIScreen.h\"\n\n" + GATE_DECLARATION + "\nstatic const NSTimeInterval SCREENSHOT_TIMEOUT = 20.;\n"),
    (SCREENSHOT_FILE,
     "  [proxy _XCT_requestScreenshot:screnshotRequest\n",
     "  NSRecursiveLock *gate = FBMobsterCaptureGate();\n"
     "  [gate lock];\n"
     "  [proxy _XCT_requestScreenshot:screnshotRequest\n"),
    (SCREENSHOT_FILE,
     "  if (nil != error && nil != innerError) {\n    *error = innerError;\n  }\n  return screenshotData;\n",
     "  [gate unlock];\n"
     "  if (nil != error && nil != innerError) {\n    *error = innerError;\n  }\n  return screenshotData;\n"),
    # The broadcaster waits one frame interval for a snapshot in flight, then skips the frame.
    (MJPEG_FILE,
     "static const char *QUEUE_NAME = \"JPEG Screenshots Provider Queue\";\n",
     "static const char *QUEUE_NAME = \"JPEG Screenshots Provider Queue\";\n" + GATE_DECLARATION),
    (MJPEG_FILE,
     "  NSData *screenshotData = [FBScreenshot takeInOriginalResolutionWithScreenID:self.mainScreenID\n",
     "  // Mobster: a frame waits for a snapshot in flight at most one frame interval, then is skipped.\n"
     "  NSRecursiveLock *captureGate = FBMobsterCaptureGate();\n"
     "  if (nil != captureGate && ![captureGate lockBeforeDate:[NSDate dateWithTimeIntervalSinceNow:\n"
     "        (NSTimeInterval)timerInterval / NSEC_PER_SEC]]) {\n"
     "    [self scheduleNextScreenshotWithInterval:timerInterval timeStarted:timeStarted];\n"
     "    return;\n"
     "  }\n"
     "  NSData *screenshotData = [FBScreenshot takeInOriginalResolutionWithScreenID:self.mainScreenID\n"),
    (MJPEG_FILE,
     "                                                                      timeout:FRAME_TIMEOUT\n"
     "                                                                        error:&error];\n",
     "                                                                      timeout:FRAME_TIMEOUT\n"
     "                                                                        error:&error];\n"
     "  [captureGate unlock];\n"),
)

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import device_manager as dm
from mobile_agent import mac_tools, supervised
from mobile_agent.wda import gate as wda_gate
from mobile_agent.wda import patches as wda_patches

# The pinned release's lines around each anchor (WebDriverAgent v16.12.10), cut down.
SNAPSHOT = """#import "FBXCAXClientProxy.h"

static id (*original_requestSnapshotForElement)(id, SEL, id, id, id, NSError **);

static id swizzledRequestSnapshotForElement(id self, SEL _cmd, id element, id attributes, id parameters, NSError **error)
{
  NSTimeInterval timeout = FBConfiguration.sharedInstance.accessibilityDeadline;
  return original_requestSnapshotForElement(self, _cmd, element, attributes, parameters, error);
}

@implementation XCAXClient_iOS (FBSnapshotReqParams)

+ (void)load
{
  IMP swizzledRequestSnapshotForElementImp = (IMP)swizzledRequestSnapshotForElement;
}

@end
"""  # noqa: E501

SCREENSHOT = """#import "XCUIScreen.h"

static const NSTimeInterval SCREENSHOT_TIMEOUT = 20.;

+ (NSData *)takeInOriginalResolutionWithScreenID:(long long)screenID
{
  dispatch_semaphore_t sem = dispatch_semaphore_create(0);
  [proxy _XCT_requestScreenshot:screnshotRequest
                      withReply:^(id image, NSError *err) {
    dispatch_semaphore_signal(sem);
  }];
  if (0 != dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, timeoutNs))) {
  }
  if (nil != error && nil != innerError) {
    *error = innerError;
  }
  return screenshotData;
}
"""

MJPEG = """static NSString *const SERVER_NAME = @"WDA MJPEG Server";
static const char *QUEUE_NAME = "JPEG Screenshots Provider Queue";

- (void)streamScreenshot
{
  NSError *error;
  NSData *screenshotData = [FBScreenshot takeInOriginalResolutionWithScreenID:self.mainScreenID
                                                           compressionQuality:compressionQuality
                                                                          uti:UTTypeJPEG
                                                                      timeout:FRAME_TIMEOUT
                                                                        error:&error];
  if (nil == screenshotData) {
    [self scheduleNextScreenshotWithInterval:backoffInterval timeStarted:timeStarted];
    return;
  }
}
"""


def patched_tree():
    root = Path(tempfile.mkdtemp())
    for name, text in ((wda_gate.SNAPSHOT_FILE, SNAPSHOT), (wda_gate.SCREENSHOT_FILE, SCREENSHOT),
                       (wda_gate.MJPEG_FILE, MJPEG)):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def apply(root):
    original = wda_patches.WDA_PATCHES
    try:
        wda_patches.WDA_PATCHES = wda_gate.PATCHES
        wda_patches.patch_wda(root)
    finally:
        wda_patches.WDA_PATCHES = original
    return {name: (root / name).read_text() for name in
            (wda_gate.SNAPSHOT_FILE, wda_gate.SCREENSHOT_FILE, wda_gate.MJPEG_FILE)}


class CaptureGatePatchTests(unittest.TestCase):
    def test_every_build_applies_the_gate(self):
        for patch in wda_gate.PATCHES:
            self.assertIn(patch, wda_patches.WDA_PATCHES)

    def test_a_snapshot_holds_the_gate_and_runs_without_it_only_after_waiting(self):
        text = apply(patched_tree())[wda_gate.SNAPSHOT_FILE]
        ungated = text.index("static id FBMobsterUngatedSnapshot(")
        gated = text.index("static id swizzledRequestSnapshotForElement(")
        load = text.index("+ (void)load")
        self.assertLess(ungated, gated)
        self.assertLess(gated, load)
        body = text[gated:text.index("@implementation")]
        self.assertIn("lockBeforeDate:", body)
        self.assertIn("return FBMobsterUngatedSnapshot(self, _cmd, element, attributes, parameters, error);", body)
        self.assertIn("@finally {\n    if (held) {\n      [gate unlock];", body)
        self.assertEqual(text.count("NSRecursiveLock *FBMobsterCaptureGate(void)\n{"), 1)

    def test_a_source_without_the_anchors_is_refused(self):
        root = patched_tree()
        (root / wda_gate.MJPEG_FILE).write_text("// another release\n")
        with self.assertRaises(RuntimeError):
            apply(root)


GATE = wda_gate.RUNNER_VARIABLE


class RunnerGateTests(unittest.TestCase):
    def test_an_unknown_device_kind_is_refused(self):
        with self.assertRaises(ValueError):
            wda_gate.runner_env("tablet", {})

    def test_the_iphone_runner_starts_with_the_gate_off_unless_overridden(self):
        for environ, expected in (({}, "off"), ({wda_gate.OVERRIDE: "on"}, "on")):
            manager = dm.DeviceManager(tempfile.mkdtemp())
            manager.save_settings(team="ABCDE12345")
            started = []
            with gate_environ(environ), mock.patch.object(manager, "device", return_value={"udid": UDID}), \
                    mock.patch.object(manager, "built", return_value=True), \
                    mock.patch.object(manager, "xcode_env", return_value=None), \
                    mock.patch.object(mac_tools, "tool", lambda name: f"/bin/{name}"), \
                    mock.patch.object(mac_tools, "iproxy_binds_loopback", return_value=True), \
                    mock.patch("mobile_agent.devices.listening", lambda port, **k: False), \
                    mock.patch.object(supervised.Supervised, "start", lambda service: started.append(service)):
                manager.start_runner()
                over_wifi = manager.runner_env()
            runner = next(service for service in started if service.name == "wda-runner")
            self.assertEqual(runner.env[GATE], expected)
            self.assertEqual(over_wifi[GATE], expected)

    def test_the_simulator_runner_starts_with_the_gate_on_unless_overridden(self):
        from mobile_agent.sim.wda import SimulatorWDA, WebDriverAgent
        from mobile_agent.tests.test_sim_fakes import TOOLS
        for environ, expected in (({}, "on"), ({wda_gate.OVERRIDE: "off"}, "off")):
            folder = Path(tempfile.mkdtemp())
            plan = str(folder / "Runner.xctestrun")
            Path(plan).write_text("")
            wda = WebDriverAgent(folder)
            wda.popen = mock.MagicMock()
            with gate_environ(environ), mock.patch("mobile_agent.sim.wda.subprocess.Popen") as popen:
                wda.start("AAAA-BBBB", 8310, 9310, plan, TOOLS)
                restarted = SimulatorWDA("AAAA-BBBB", 8310, 9310, plan, tools=TOOLS).environment()
                SimulatorWDA("AAAA-BBBB", 8310, 9310, plan).start()
            self.assertEqual(wda.popen.call_args.kwargs["env"][GATE], expected)
            self.assertIn(f"{GATE}={expected}", wda.runner_log("AAAA-BBBB").read_text())
            self.assertEqual(restarted[GATE], expected)
            self.assertEqual(popen.call_args.kwargs["env"][GATE], expected)


UDID = "00008130-001A2B3C4D5E6F70"


@contextlib.contextmanager
def gate_environ(environ):
    with mock.patch.dict(os.environ):
        for name in (GATE, wda_gate.OVERRIDE):
            os.environ.pop(name, None)
        os.environ.update(environ)
        yield


if __name__ == "__main__":
    unittest.main()

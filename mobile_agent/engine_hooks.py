"""The harness's harness_api.EngineHooks: what the engine reaches in the full Mobster."""


class Harness:
    def __init__(self):
        self.target_driver, self.cli, self.runtime_for = None, [], None

    def model_routes(self):
        from .ai_plans import providers
        return providers

    def key(self, provider, env):
        from . import keys
        return (keys.anthropic_key if provider == "anthropic" else keys.openai_key)(env)

    def tool_policy(self, skills, *, run_context, driver, guard):
        from .harness.tools import offered
        return offered(skills, run_context=run_context, driver=driver, guard=guard)

    def first_screen(self, driver, run_context):
        from .harness.prewarm import take
        return take(driver, run_context)

    def private_rects(self, elements, bundle_id):
        from .run_gif import private_rects
        return private_rects(elements, bundle_id)

    def long_report(self):
        from .longform import report
        return report

    def wifi_attached(self, udid):
        from .wireless import transport
        value = transport.attached(udid)
        return (False, None) if value is transport.NOT_WIFI else (True, value)

    def wifi_lost(self, udid):
        from .wireless import transport
        return transport.lost(udid)

    def wifi_may_reach(self, udid):
        from .paths import user_data_dir
        from .wireless import store, transport, transport_on
        return transport_on() and transport.link(udid) is None and store.enabled(user_data_dir(), udid)

    def usb_attached(self, udid):
        from .mac_tools import usb_attached
        return usb_attached(udid)

    def devicectl_json(self, args, timeout):
        from .wireless import devicectl
        return devicectl.run_json(args, timeout)

    def coredevice_locked(self, udid):
        from .wireless import devicectl
        return devicectl.read_lock_state(udid)

    def coredevice_apps(self, udid, kind):
        from .wireless import devicectl
        return devicectl.read_apps(udid, kind)

    def track_disabled(self, track, reason):
        from . import tracks
        tracks.disable(track, reason)

    def hedge(self):
        from . import hedge
        return hedge

    def skills(self):
        from .skills import default_skills
        return default_skills()

    def app_catalog(self):
        from .catalog import APPS
        return APPS

    def image_policy(self):
        from . import image_policy
        return image_policy

    def frame_clock(self):
        from .wda import frame_clock
        return frame_clock

    def ios_slice(self, snapshot, request, driver):
        from .ios.guide import turn_slice
        return turn_slice(snapshot, request, driver)

    def screen_text(self):
        from .screen_text import reader
        return reader()

    def pixel_screen(self):
        from . import perception
        return perception

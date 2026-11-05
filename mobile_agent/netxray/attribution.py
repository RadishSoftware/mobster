"""Which app a process is, from the executables the phone's installed apps name. No match never becomes a guess."""

APP, IOS, PROCESS, UNATTRIBUTED_KIND = "app", "ios", "process", "unattributed"
IOS_LABEL = "iOS"
UNATTRIBUTED = "Unattributed"
UNATTRIBUTED_NOTE = ("Mobster couldn't tell which app sent this traffic, so it isn't counted for any app. "
                     "The capture had no process name for it.")
PROCESS_NOTE = "Mobster couldn't match this process to an app on the phone."
NAME_LIMIT = 16            # pcapd's process name is 16 characters; a longer executable name arrives cut

SYSTEM_PROCESSES = frozenset("""
accountsd apsd assistantd cloudd configd containermanagerd dasd duetexpertd geod identityservicesd itunescloudd
keybagd locationd mDNSResponder mobileassetd nehelper nesessionmanager networkserviceproxy nsurlsessiond nsurlstoraged
parsecd rapportd searchpartyd sharingd softwareupdated symptomsd timed trustd wifid
amsaccountsd appleaccountd appstored destinationd devicecheckd findmylocated fitcored identityservices itunesstored
mstreamd promotedcontentd rtcreportingd storekitd com.apple.webkit com.apple.webkit.networking
""".lower().split())


class Names:
    """The phone's apps by executable name. ``executables`` is {executable: the app's name on the phone}."""

    def __init__(self, executables=None):
        self.executables = dict(executables or {})

    def __bool__(self):
        return bool(self.executables)

    def app_name(self, process):
        if process in self.executables:
            return self.executables[process]
        if len(process) >= NAME_LIMIT:
            cut = {name for executable, name in self.executables.items() if executable.startswith(process)}
            if len(cut) == 1:
                return cut.pop()
        return None

    def label(self, process):
        """(label, kind) for the process pcapd named; None or "" is Unattributed."""
        if not process:
            return UNATTRIBUTED, UNATTRIBUTED_KIND
        name = self.app_name(process)
        if name:
            return name, APP
        lowered = process.lower()
        if lowered in SYSTEM_PROCESSES or (len(process) >= NAME_LIMIT
                                           and len([d for d in SYSTEM_PROCESSES if d.startswith(lowered)]) == 1):
            return IOS_LABEL, IOS
        return process, PROCESS


def executables_from(listing):
    """{executable: name} from installed_apps rows ({"bundleId", "name", "executable"?})."""
    found, shared = {}, set()
    for row in listing or ():
        executable, name = row.get("executable"), row.get("name")
        if not executable or not name:
            continue
        if executable in found and found[executable] != name:
            shared.add(executable)
        found[executable] = name
    return {executable: name for executable, name in found.items() if executable not in shared}

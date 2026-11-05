"""The API behind `mobster testflight` for Mobster for Mac, registered by testkit/register.py under /api/tests."""

from pathlib import Path

from . import DevError

STATUS = {"outcome_unknown": 409, "needs_tap": 409}


def _fail(error):
    from ..api_routes import Response
    from . import asc
    code = "outcome_unknown" if isinstance(error, asc.UnknownOutcome) else \
        "needs_tap" if isinstance(error, asc.NeedsTap) else "testflight"
    return Response.error(error.line(), STATUS.get(code, 400), code)


def _project(value):
    """The project folder a request names, or None for the shared key. ValueError when it isn't a folder."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_dir():
        raise ValueError("project is the absolute path of a folder on this Mac.")
    from ..verify.checks import project_root
    return Path(project_root(Path(value))).resolve()


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} is text of at most {limit} characters.")
    return value.strip()


def _gate(request):
    gate = getattr(request.runtime, "plan_gate", None)
    if gate is not None:
        gate.require()


def _client(root, store=None):
    from . import asc
    return asc.Client(asc.load_key(store=store, project=root))


def builds(request):
    from ..api_routes import Response
    from . import testflight
    try:
        _gate(request)
        root = _project((request.query.get("project") or [None])[0])
        bundle = _text((request.query.get("bundle") or [None])[0], "bundle")
        app, rows = testflight.builds(_client(root), bundle)
    except DevError as error:
        return _fail(error)
    return Response.json({"app": app, "builds": rows})


def install(request):
    from ..api_routes import Response
    from ..phone_io import PhoneIOError, choose_device, wda
    from . import testflight
    body = request.body if isinstance(request.body, dict) else {}
    unknown = [key for key in body if key not in ("project", "bundle", "build", "version", "device")]
    if unknown:
        raise ValueError(f"Unknown field {unknown[0]}.")
    root = _project(body.get("project"))
    if root is None:
        raise ValueError("project is the absolute path of the project's folder.")
    bundle = _text(body.get("bundle"), "bundle")
    build = _text(str(body["build"]), "build", 40) if body.get("build") is not None else None
    version = _text(body["version"], "version", 40) if body.get("version") is not None else None
    try:
        _gate(request)
        record = choose_device(_text(body["device"], "device") if body.get("device") else None)
        done = testflight.install(_client(root), record, wda, bundle, root=root, build=build, version=version)
    except DevError as error:
        return _fail(error)
    except PhoneIOError as error:
        return Response.error(f"{error} {error.fix or ''}".strip(), 409, "no_device")
    row = done["row"]
    return Response.json({"result": done["result"], "device": record.get("name"),
                          "build": {"version": row["version"], "build": row["build"]}})


def key(request):
    from ..api_routes import Response
    from . import asc
    if request.method == "POST":
        body = request.body if isinstance(request.body, dict) else {}
        unknown = [name for name in body if name not in ("project", "keyId", "issuerId", "p8")]
        if unknown:
            raise ValueError(f"Unknown field {unknown[0]}.")
        root = _project(body.get("project"))
        try:
            saved = asc.save_key(_text(body.get("keyId"), "keyId", 20),
                                 _text(body["issuerId"], "issuerId", 40) if body.get("issuerId") else None,
                                 _text(body.get("p8"), "p8", 4000), project=root)
        except DevError as error:
            return _fail(error)
        return Response.json({"configured": True, **saved.public()})
    root = _project((request.query.get("project") or [None])[0])
    if request.method == "DELETE":
        try:
            asc.remove_key(project=root)
        except Exception:
            return Response.error("The Keychain didn't remove the key.", 503, "keychain")
        return Response.json({"configured": False})
    try:
        return Response.json({"configured": True, **asc.load_key(project=root).public()})
    except DevError:
        return Response.json({"configured": False})

"""Built-in check recipes: `mobster test add <recipe>` writes one as a normal check in .mobster/checks."""

import re
from dataclasses import dataclass
from typing import Callable, Optional

__all__ = ["Param", "Recipe", "RecipeError", "BUILTIN", "get", "names", "build", "SHARED"]

PLACEHOLDER = re.compile(r"\$\{([a-z][a-z0-9_]*)\}")
SHARED = ("bundle", "app", "name")


class RecipeError(Exception):
    """A usage problem with a recipe: an unknown recipe or param, or a missing required one."""


@dataclass(frozen=True)
class Param:
    name: str
    help: str
    required: bool = False
    default: Optional[str] = None


@dataclass(frozen=True)
class Recipe:
    name: str
    summary: str                       # one line, lowercase start, no final period
    template: dict
    params: tuple = ()
    finish: Optional[Callable] = None
    notes: str = ""                    # printed after the file is written: what to do next


_DROP = object()


def _fill(value, params):
    """``value`` with every ${param} replaced; _DROP when it names an unset param or empties."""
    if isinstance(value, str):
        missing = False

        def sub(match):
            nonlocal missing
            got = params.get(match.group(1))
            if got is None:
                missing = True
                return ""
            return got
        text = PLACEHOLDER.sub(sub, value)
        return _DROP if missing else text
    if isinstance(value, list):
        items = [_fill(item, params) for item in value]
        items = [item for item in items if item is not _DROP]
        return items if items or not value else _DROP
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            filled = _fill(item, params)
            if filled is not _DROP:
                out[key] = filled
        return out if out or not value else _DROP
    return value


def get(name):
    for recipe in BUILTIN:
        if recipe.name == name:
            return recipe
    raise RecipeError(f"there is no recipe {name!r}; the recipes are {', '.join(names())} "
                      f"(mobster test add --list)")


def names():
    return [recipe.name for recipe in BUILTIN]


def build(name, given):
    """(recipe, check data) for recipe ``name`` and the params in ``given`` (a dict of text). Raises RecipeError."""
    recipe = get(name)
    known = {param.name for param in recipe.params} | set(SHARED)
    for key in given:
        if key not in known:
            raise RecipeError(f"recipe {recipe.name} has no param {key!r}; it takes "
                              f"{', '.join(sorted(known))}")
    params = {param.name: param.default for param in recipe.params}
    params.update({key: value for key, value in given.items() if value not in (None, "")})
    for param in recipe.params:
        if param.required and params.get(param.name) is None:
            raise RecipeError(f"recipe {recipe.name} needs {param.name}: {param.help} "
                              f"(--set {param.name}=VALUE)")
    data = _fill(recipe.template, params)
    if recipe.finish is not None:
        data = recipe.finish(params, data)
    if params.get("name"):
        data["name"] = params["name"]
    app = data.setdefault("app", {})
    if params.get("bundle"):
        app["bundle"] = params["bundle"]
    if params.get("app"):
        app["path"] = params["app"]
    if not app.get("bundle") and not app.get("path"):
        raise RecipeError(f"recipe {recipe.name} needs the app: --bundle com.example.app")
    from ..verify.checks import CheckError, check_from_dict
    try:
        check_from_dict(data)
    except CheckError as error:
        raise RecipeError(f"recipe {recipe.name} wrote a check that doesn't load ({error}); "
                          "fix the params") from None
    return recipe, data


def _numbers(*keys):
    """A finish that turns the text of these keys into numbers (a template can only hold text)."""
    def finish(params, data):
        for item in data.get("expect", ()):
            for key in keys:
                if isinstance(item.get(key), str) and item[key].isdigit():
                    item[key] = int(item[key])
        return data
    return finish


BUILTIN = (
    Recipe(
        name="smoke",
        summary="the app launches and shows the text you name",
        template={"version": 1, "name": "The app launches and shows ${text}", "tags": ["smoke"], "app": {},
                  "launch": {"url": "${url}"}, "expect": [{"text": "${text}"}]},
        params=(Param("text", "text your first screen shows", required=True),
                Param("url", "a link to open after launch, such as yourapp://home")),
        notes="A launch-only check needs no key."),
    Recipe(
        name="paywall",
        summary="the paywall shows its plans and a restore button",
        template={"version": 1, "name": "The paywall shows ${plans} plans", "tags": ["smoke", "paywall"],
                  "app": {}, "launch": {"url": "${url}", "args": ["${launch_arg}"]},
                  "expect": [{"count": {"id": "/^plan_/"}, "equals": "${plans}"},
                             {"visible": {"label": "${restore}", "role": "button"}}]},
        params=(Param("url", "the link to your paywall, such as yourapp://paywall", required=True),
                Param("plans", "how many plans the paywall shows", default="3"),
                Param("restore", "the restore button's label", default="Restore Purchases"),
                Param("launch_arg", "a launch argument that makes your app show its local product list")),
        finish=_numbers("equals"),
        notes="Mobster checks everything up to the purchase. Edit expect to add each plan's price."),
)

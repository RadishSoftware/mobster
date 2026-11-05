# Icons

Mobster's one icon set. The Mac app, mobster.dev and Remote draw these and nothing else (`dashboard/DESIGN.md` § 10). The docs (Mintlify) name the same Lucide drawings, which Mintlify draws itself: it shows an icon file as an image that can't take the text colour.

- `svg/<name>.svg`: one drawing per icon on a 24 unit grid, `viewBox="0 0 24 24"`. `svg/<name>.16.svg`, where there is one, is drawn for 16 px and under, where the full drawing's details close up. Lines only say where they are: no colour, stroke width or class. A filled glyph says `data-fill` on its root. A part that moves on its own is a `<g data-part>`, with `data-origin="x y"` where it turns or scales; an icon whose motion moves it whole needs none.
- `icons.json`: each icon's motion, the motions' kinds (`hold`, `once`, `loop`), the line (`0.5 + size / 16` px) and the icons mobster.dev and Remote carry.
- `python3 scripts/gen_icons.py` writes the copies: `dashboard/src/components/ui/icons.gen.ts`, and mobster.dev's and Remote's when `icons.json` lists icons for them. `--check` fails when one is stale (`mobile_agent/tests/test_icons.py`).
- `/qa/icons.html` on the dashboard's dev server shows every icon at 12, 14, 16, 20 and 24 px with its motion.

## Adding an icon

1. Draw it on the 24 grid, or take Lucide's (lucide.dev, ISC) as the rest were, and save it as `svg/<name>.svg`.
2. Add it to `icons.json`, with a motion if it sits in a control and one fits. Group what moves in a `data-part` the motion names (`dashboard/src/components/ui/icon.css`).
3. Run `python3 scripts/gen_icons.py`, then check it at 400% on `/qa/icons.html` in both appearances.

## Sources

| Files | Source |
| --- | --- |
| Every icon not listed below | Lucide 0.577.0 (ISC, lucide.dev), unchanged except for motion parts |
| `iphone.svg`, `ipad.svg`, `mac.svg` | Apple's SF Symbols `iphone`, `ipad` and `laptopcomputer`, medium weight, traced by `scripts/trace_sf_symbol.py` (15 Oct 2026, macOS 26.5.1), then simplified to .03 of a grid unit (16 Oct 2026) |
| `spinner.svg`, `play-fill.svg`, `pause-fill.svg`, `stop-fill.svg`, every `*.16.svg` | Drawn for Mobster (16 Oct 2026) |

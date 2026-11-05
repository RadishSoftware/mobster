# Brand art

How Mobster's art looks: blog covers, share images, event covers, use-case cards, feature cards and upsell cards. The tokens in `docs/brand/tokens.json` set the colours and type; this file sets how they become pictures. The renderer in `product/brand-art/` makes the grounds, and `product/brand-art/SPEC.md` is its input contract.

## The system

A picture has two layers:

1. **The ground:** poured glaze in one category's palette, sometimes cut into the Swipe's three bands. It's abstract: no objects, no 3D marks, no characters, no landscapes.
2. **The type:** the Swipe wordmark lockup, the title, the category and the date, set as live text over the ground, never painted into it.

The Swipe appears only as the flat wordmark lockup. It's never an object in the art.

Type sits on a calm part of the ground: the plate's calm side, a calm zone the renderer quiets, a notch the cut leaves, or paper. It never sits on a bright streak, and it stays readable at thumbnail size.

## Grounds

### Glaze plates

Generated macro photographs of smooth, wet, glossy stoneware glaze poured on a tile, one per category, in `generated/smooth-<category>/` (18 Oct 2026; `smooth-<category>` in `plates.json`). Each plate is a calm pool: the lower-left 60% is an almost flat deep tone, and one or two broad pours sit in the upper right, each with one crisp rim. The surface is mirror-smooth. Pits, pinholes, specks, crawl, crust and mottling fail at 400%; that is why the first set (`generated/hires-<category>/`, pitted satin, 16 Oct) was retired. The silhouettes vary (a lobe, a waved ribbon, a diagonal pour, two lobes, a curl), and the categories that share a page never share a shape. Highlights are wide and soft. No objects, text, horizons or vortexes.

Every plate is kept next to the exact prompt that made it, so it can be made again.

**Resolution.** The renderer never enlarges a plate: each plate is a 3840 × 2160 master, which covers a 1600 × 900 cover at 2x with room for pour-cut's 1.2 zoom, and is sampled on whole texels. The image generator returns about 1672 × 941, so today's masters are that native image enlarged once, with Lanczos, outside the renderer. We tried super-resolution (Real-ESRGAN x4plus): it looked sharper but Codex found it waxy at 100% and 400%, with invented pits and fissures, so it isn't used. A cover shown at full width on a 2x screen therefore shows the plate's real detail enlarged about 1.9 times; true 2x detail needs a generator that returns 3840 × 2160 natively. Never stretch a plate to an aspect it wasn't made at, never blur it to hide texture, and keep `plate.flow` at 0 where a cover must be sharp, because flow resamples every pixel.

### The cut

Three bands in the Swipe's proportions. The caps are 77.46% of the box's width, the slabs 29.7% of its height, and the gaps 5.45%. The middle band slides right by 22.54% of the box, which is the Swipe's own slide: 29% of the body's width. The row's piece of the glaze slides with it, so the image visibly breaks where the swipe is.

- Outer cap corners: 52% of a slab's height, never more than 24% of the box's width. Row corners: 26% of a slab's height.
- Outside the bands: Paper (the light page token, `#F8F8F8`), or the category's deep tone.
- The slide leaves three notches: left of the row, and right of each cap. Notches hold type.

### Calm zones, grain and fades

- **Calm zone:** under a title, the renderer blurs the plate and pulls it toward the deep tone in a feathered rectangle. It's not frost: no panel, no edge, no highlight.
- **Grain:** monochrome, one value per pixel at export size, fixed seed per image. 0.045 on dark, 0.035 on mid tones, 0.022 on paper. It fades over highlights.
- **Fade:** a ground can fade into a page's background colour by its bottom edge. The last row is exactly the token, and the grain falls to zero there, so there's no seam.

## Category palettes

Every picture belongs to one category. The saturated tone comes from the tokens (violet and `data.dark`). The deep tone is the saturated tone mixed 20% into Night (the dark page token, `#121212`); the glow is the saturated tone mixed 35% toward white.

| Category | Use it for | Saturated | Deep | Glow |
|---|---|---|---|---|
| Phone work | the agent, launches, the company | `#6E56F6` | `#242040` | `#A191F9` |
| Developers | the CLI, MCP, testing | `#3987E5` | `#1A293C` | `#80B0EE` |
| Connectors | Slack, Drive, sending results | `#D95926` | `#3A2016` | `#E89273` |
| Profiles | accounts, several phones | `#D55181` | `#391F28` | `#E48DAD` |
| Events | launches, talks, triggers | `#C98500` | `#37290E` | `#DDAE59` |
| Releases | version posts, changelogs | `#199E70` | `#132E25` | `#6CC0A2` |

Art uses the UI's neutral tokens, with no warm tones of its own. The cut's paper is Paper `#F8F8F8`. On dark grounds, type is Night's ink `#F2F2F2` with secondary lines in `#B3B3B3`; on paper, it's ink `#181818` with secondary lines in `#505050`.

In art, colour fills grounds. In the app's UI, violet stays a line and never a fill. An upsell card in the app is where art meets UI, so its buttons follow the UI rules: an ink or paper button, never a violet one.

## Type lockups

- **Title:** Mobster Sans, weight 580, tracking −2.5%, line height 1.04, sentence case.
- **Meta:** Mobster Code, weight 400. Two items, such as the category and the date, separated by space: no middle dots, no slashes, no capitals.
- **Lockup:** `docs/brand/logo/lockup-dark.svg` on dark grounds, `lockup-light.svg` on paper, in one colour, 2.8% to 3.4% of the frame's width tall. It sits wholly on calm ground, at least 3% of the width from any band edge or streak. Any type in a notch keeps 20 px of display clearance from the bands, and a title keeps 20 px below its descenders. On use-case cards it heads the bottom stack.
- **Minimums:** meta at least 2.8% of the frame's width on a cover, so it's 12 px where a cover shows 432 px wide and 4.2% on a use-case card; a title at least 5%. Below that, it doesn't read at thumbnail size, so leave it off the image and set it below.
- **Colour:** light type on dark grounds and ink on paper. Never violet, never a category colour.
- **Margins:** 6% of the frame's width on every side (4.5% around a full-frame cut). Snap to an 8 px unit at 1600 wide, scaled with the frame.

| Layout | Lockup | Title | Meta |
|---|---|---|---|
| Pour | top left | bottom left, over the calm zone, 6% of the width on a blog cover | above the title |
| Paper cut | in the top-right notch | below the cut, on paper, 5% of the width | above the title |
| Night cut | in the top-right notch | below the cut on Night, light type, 5% of the width; on a banner, left of the cut | above the title |
| Pour cut | in the top-right notch | on the bottom band's calm left, 5% of the width, at most two lines, at least 6% of the width above the frame's bottom | in the bottom-right notch, right-aligned |

In a news grid, the cover carries the lockup and the title; the grid sets the title again below it in Mobster Sans 560 at 17 px, then the meta line. A version post's cover sets only the version number, large.

## Variants

Different posts use different styles, and new styles arrive over time. Each style is a **variant**: a named preset of ground, plate, palette, cut or no cut, calm zones, grain and lockup layout. The registry is `variants.json`, next to this file.

| Variant | What it is | Approved |
|---|---|---|
| `pour` | The category's glaze, full bleed, with the type over its calm lower left | 16 Oct 2026, by the owner |
| `paper-cut` | Warm paper, the glaze in the cut above, the type in ink below | 16 Oct 2026, by the owner |
| `night-cut` | Paper cut's bands on neutral Night (`#121212`) instead of paper, with light type beside or below the cut. The social kit's banners and cards (`docs/brand/social/`) | 16 Oct 2026, by the owner, on the LinkedIn cover |
| `pour-cut` | The glaze cut into bands that bleed off the edges, over Night, with the lockup and the meta in the notches | Not yet |

### Use a variant

A page names its cover in `site/content/covers.json`: a plate from `plates.json` (below), an approved variant as its layout and, at most, one real element.

```json
"blog/what-is-mobster": { "plate": "glaze-cobalt-drip", "layout": "paper-cut" },
"blog/claude-code-real-iphone": { "plate": "pour-connectors", "layout": "pour", "marks": ["claude"] }
```

`product/brand-art/variants.mjs` turns the layout into the variant's preset. Production builds (`--production`, `NODE_ENV=production` or `VERCEL_ENV=production`) refuse a variant that isn't approved, and say so:

```
art: variant "pour-cut" isn't approved, so production builds refuse it. Show it to the owner, then set approved, approvedOn and approvedBy in docs/brand/art/variants.json
```

`site/tools/make-covers.mjs` always renders as production does. Previews of the renderer itself render any variant.

### Add a variant

1. Add an entry to `variants.json` with `approved: false`, `approvedOn: null` and `approvedBy: null`.
2. If it needs a new plate, generate it and keep its prompt next to it, one folder per plate.
3. Render the full set in the variant: a blog cover, an OG image, an event cover, use-case cards and a news grid of real posts.
4. Have Codex critique the set against this file: type on calm ground, readable at thumbnail size, "copied?" and "same product?".
5. Show the set to the owner. When the owner approves it, set `approved: true`, `approvedOn` to the date, `approvedBy: "owner"` and `seenAs` to where they saw it, in the same pull request as the art that first uses it.

`mobile_agent/tests/test_brand.py` checks that approval is all or nothing and that the table above names every variant.

## Covers and share cards

Every blog post, comparison, use case and docs tab, and the press page, has a cover. Its share card is drawn on the same plate.

- **The cover** carries no type. It's the plate in its layout's `cover` form (`variants.json`, each approved variant's `cover` block): pour full bleed, or the cut filling the frame inside its margins. The page sets the title as live type beside or below it. It's 800 x 450 CSS px, exported at 2x (1600 x 900 AVIF) for the site and at 2x and 3x as masters in the brand team's folder.
- **One real element at most:** a partner's mark (`docs/brand/marks`) or, on a comparison, Mobster's icon beside the compared tool's own mark. Each sits on one tile, Paper with the app icon's corners in a hairline of glass, 13% of the cover's width alone or 12% as a pair. The marks' tile copies already set every mark at one optical size.
- **The card** is 1200 x 630. Its ground is the same plate in the variant's own form, with pour's calm zones or the cut over the type. `site/lib/og/card.tsx` sets the lockup, the address and the page's line over it in the variant's type lockup, in the neutral ink tokens for the ground's theme. The calm zone under pour's type is computed, not tuned. `site/tools/make-covers.mjs` measures the ground under the card's title and address (`site/lib/og/layout.ts`) and deepens the zone in steps of 0.05, from the variant's 0.42 up to 0.75, until both reach 4.5:1 against their ink. A page that can't reach it needs another plate, and the covers test fails until it has one. A light plate rarely can under light type, so it goes where the type sits on Night or Paper: in a cut.
- **Variety.** The cut layouts are kept to eight across the set, for pages where the Swipe means something: the product, the company and /press. No two pages share both a plate and a layout, and no two neighbours in an index share a plate or a plate's family (two wet pools side by side, or two block edges): `plates.json` gives each plate its `family`. A crop's zoom stays at or under 1.05, because a 1600 px cover has only the plate's 1672 native pixels to show. To vary a crop, change the plate, the region or the layout, never the zoom. `site/tests/covers.test.mjs` checks all of this.

## Plates

`plates.json` lists the plates covers and cards are made from: generated macro photographs of glaze or of glass (thick smoked, frosted, reeded or cast slabs with crisp bevels, as a material, never a UI frame). Each entry gives:
- the category whose palette it carries and its material;
- the date, the generator (the local Codex CLI's image tool) and its native size;
- where its master and the exact prompt live in the brand team's folder.

A plate joins only after review at 100% and 400% (no mush, seams or marks) and the neutral rule (a neutral plate has no warm or violet cast). `tone` (saturation and dim) quiets a plate whose colour is too loud beside Night and violet. The masters are each native image enlarged once with Lanczos to 3840 px wide.

## Do

- Let the ground be the picture, and keep the type to the lockup, the title, the category and the date.
- Keep most of the frame in the deep tone or paper. Bright passages are few, broad and soft.
- Put the title where the plate is calm, and check it at thumbnail size.
- Use real Mobster UI, real app logos and real bezels when a picture shows the product.
- Keep each plate's prompt and each export's scene JSON.

## Don't

- Don't render the Swipe as an object, in 3D, in glass or in porcelain. Use the flat lockup.
- Don't add characters, faces, eyes, mascots or hands.
- Don't set type on a streak, on frost, or with an outline or shadow to rescue it. Move it to a calm place.
- Don't use landscapes, planets, horizons, vortexes or stock 3D shapes.
- Don't put a saturated tile beside a dark title panel as a fixed card format, and don't centre a white logo on a pastel gradient: both read as another company's brand.
- Don't use more than one category's palette in one picture.
- Don't ship a variant the owner hasn't approved.

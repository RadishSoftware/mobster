# Social kit

Mobster's profile pictures, banners, share cards and launch images for every network, plus the Stripe product images and the email marks. Every file is drawn by `product/social-kit/build.mjs` from the brand-art renderer (`product/brand-art/`) and the logo SVGs (`docs/brand/logo/`). Nothing is exported by hand.

## How it looks

- **Banners, covers and cards use `night-cut`.** The ground is neutral Night (`#121212`). The phone-work glaze shows only inside the Swipe's three bands, and the type is light, beside or below the cut, never on the glaze. The owner picked it on 16 Oct 2026 with the LinkedIn cover (`docs/brand/art/variants.json`).
- **The tagline is "Test and automate apps on real iPhones."** It's on the LinkedIn cover, the X header, the YouTube banner, the GitHub preview, the Discord invite background, the Reddit banners and the first Product Hunt image.
- **mobster.dev draws its own share cards,** for every page, post, integration and comparison, at build time (`site/lib/og/card.tsx`). The kit gives it the shared ground, `site/lib/og/ground.jpg`: the cut on Night, with the type left to the site. A page with a cover (`site/content/covers.json`) has its card drawn on its own ground instead, the cover's plate from `site/tools/make-covers.mjs` (`docs/brand/art/README.md`, Covers and share cards). Each card says its page's line from `site/content/seo.ts` under the page's address, in the same layout as the docs card here. A news post gets its card the same way.
- **Avatars are the flat Swipe on Night,** 58% of the frame's height, so any circle a network cuts keeps it whole. It's the same art as the LinkedIn logo and the Stripe icon. The owner picked it on 16 Oct 2026. The GitHub organization keeps Radish's picture: Radish is the parent company.
- **The Product Hunt gallery shows the real app.** Images 2 to 5 frame real screens from `docs/images/` (the test report, a conversation, an approval and setup) in a frosted glass rim over the cut.
- **Stripe:** the icon is the mark on Night. The logo is the lockup on a Night badge, so it reads on the white invoice and disappears into the dark Checkout header. Set the brand and accent colours to `#121212`. Each product image names its plan.
- **Email:** the header lockup and the footer mark are at 3x the size the templates show them, light and dark, on a transparent ground.
- **Formats:** files with art are JPEG at quality 92 with full chroma (4:4:4), which every network takes. The grain makes a PNG of the same image four to five times larger (30 MB for the kit against 7 MB), with no difference at 100% or 400%. The avatars, the Stripe icon and logo, and the email marks are flat or transparent, so they stay PNG.

## Copy

| Field | Text |
|---|---|
| Tagline (Product Hunt's 60 characters, X, LinkedIn) | Test and automate apps on real iPhones |
| Product Hunt thumbnail | `producthunt/thumbnail-240.png` |

## Rebuild

You need Node 20 or later, `npm ci` in `product/brand-art`, and Python 3 with Pillow for the JPEG files. The glaze plates aren't in the repository. Pass the folder that holds `generated/smooth-<category>/plate-3840-lanczos.png` with `--plates`, or set `BRAND_ART_PLATES`.

```sh
node product/social-kit/build.mjs --plates ~/mobster-work/brand/art                 # every file, then this table
node product/social-kit/build.mjs --plates ~/mobster-work/brand/art --only x-header  # one file
node product/social-kit/build.mjs --list                                            # ids, sizes and files
```

`build.mjs` prints each file as it writes it:

```
docs/brand/social/x/header-1500x500.jpg  1500x500
```

The build refuses to enlarge a plate (`docs/brand/art/README.md`, Resolution). `--approved-only` also refuses a variant the owner hasn't approved. `product/social-kit/assets.mjs` holds each file's size, layout and destination. After the images, the build rewrites the table below and the contact sheet's manifest (`product/social-kit/manifest.json`).

## Review

The contact sheet shows every file at the size each platform displays it, inside that platform's own profile layout, with its crop and the parts the platform covers.

```sh
node product/social-kit/build.mjs --serve 3128
```

```
contact sheet: http://127.0.0.1:3128/product/social-kit/sheet.html
```

## Files

<!-- assets:start -->
| File, under `docs/brand/` unless it starts with `/` | Size (px) | Where it goes | Rebuild with |
|---|---|---|---|
| [`social/x/avatar-400.png`](x/avatar-400.png) | 400 × 400 | X profile: Edit profile, photo (a circle) | `--only x-avatar` |
| [`social/linkedin/logo-400.png`](linkedin/logo-400.png) | 400 × 400 | LinkedIn company page: Edit page, Header, Logo (a square) | `--only linkedin-logo` |
| [`social/youtube/avatar-800.png`](youtube/avatar-800.png) | 800 × 800 | YouTube Studio: Customization, Branding, Picture (a circle) | `--only youtube-avatar` |
| [`social/discord/server-icon-1024.png`](discord/server-icon-1024.png) | 1024 × 1024 | Discord: Server Settings, Overview, Server icon (a circle) | `--only discord-icon` |
| [`social/instagram/avatar-1080.png`](instagram/avatar-1080.png) | 1080 × 1080 | Instagram: Edit profile, picture; Threads shows the same one (a circle) | `--only instagram-avatar` |
| [`social/tiktok/avatar-720.png`](tiktok/avatar-720.png) | 720 × 720 | TikTok: Edit profile, Change photo (a circle) | `--only tiktok-avatar` |
| [`social/bluesky/avatar-1000.png`](bluesky/avatar-1000.png) | 1000 × 1000 | Bluesky: Edit profile, avatar (a circle) | `--only bluesky-avatar` |
| [`social/reddit/icon-500.png`](reddit/icon-500.png) | 500 × 500 | Reddit community: Mod tools, Community appearance, Avatar (a circle) | `--only reddit-icon` |
| [`social/producthunt/thumbnail-240.png`](producthunt/thumbnail-240.png) | 240 × 240 | Product Hunt launch: Thumbnail (a rounded square) | `--only producthunt-thumb` |
| [`social/crunchbase/logo-800.png`](crunchbase/logo-800.png) | 800 × 800 | Crunchbase organization: Edit, Logo (a rounded square) | `--only crunchbase-logo` |
| [`social/linkedin/cover.jpg`](linkedin/cover.jpg) | 1128 × 191 | LinkedIn company page: Edit page, Header, Cover image | `--only linkedin-cover` |
| [`social/linkedin/cover@2x.jpg`](linkedin/cover@2x.jpg) | 2256 × 382 | LinkedIn company page: Edit page, Header, Cover image; the same at twice the pixels, sharper on Retina screens | `--only linkedin-cover-2x` |
| [`social/x/header-1500x500.jpg`](x/header-1500x500.jpg) | 1500 × 500 | X profile: Edit profile, Header photo | `--only x-header` |
| [`social/youtube/banner-2560x1440.jpg`](youtube/banner-2560x1440.jpg) | 2560 × 1440 | YouTube Studio: Customization, Branding, Banner image | `--only youtube-banner` |
| [`social/github/social-preview-1280x640.jpg`](github/social-preview-1280x640.jpg) | 1280 × 640 | github.com/RadishSoftware/mobster: Settings, General, Social preview | `--only github-preview` |
| [`social/discord/banner-960x540.jpg`](discord/banner-960x540.jpg) | 960 × 540 | Discord: Server Settings, Overview, Server banner background (boost level 2) | `--only discord-banner` |
| [`social/discord/invite-splash-1920x1080.jpg`](discord/invite-splash-1920x1080.jpg) | 1920 × 1080 | Discord: Server Settings, Overview, Invite background (boost level 1) | `--only discord-splash` |
| [`social/reddit/banner-1920x384.jpg`](reddit/banner-1920x384.jpg) | 1920 × 384 | Reddit community: Mod tools, Community appearance, Banner (desktop) | `--only reddit-banner` |
| [`social/reddit/banner-mobile-1600x480.jpg`](reddit/banner-mobile-1600x480.jpg) | 1600 × 480 | Reddit community: Mod tools, Community appearance, Mobile banner | `--only reddit-banner-mobile` |
| [`/site/lib/og/ground.jpg`](../../../site/lib/og/ground.jpg) | 1200 × 630 | mobster.dev: the ground site/lib/og/card.tsx sets each page’s lockup, address and line over | `--only site-og-ground` |
| [`social/og/docs-1200x630.jpg`](og/docs-1200x630.jpg) | 1200 × 630 | docs.mobster.dev: og:image and twitter:image (docs/docs.json, seo.metatags) | `--only og-docs` |
| [`social/producthunt/gallery-1-1270x760.jpg`](producthunt/gallery-1-1270x760.jpg) | 1270 × 760 | Product Hunt launch: Gallery, image 1 | `--only producthunt-cover` |
| [`social/producthunt/gallery-2-1270x760.jpg`](producthunt/gallery-2-1270x760.jpg) | 1270 × 760 | Product Hunt launch: Gallery, image 2 | `--only producthunt-gallery-2` |
| [`social/producthunt/gallery-3-1270x760.jpg`](producthunt/gallery-3-1270x760.jpg) | 1270 × 760 | Product Hunt launch: Gallery, image 3 | `--only producthunt-gallery-3` |
| [`social/producthunt/gallery-4-1270x760.jpg`](producthunt/gallery-4-1270x760.jpg) | 1270 × 760 | Product Hunt launch: Gallery, image 4 | `--only producthunt-gallery-4` |
| [`social/producthunt/gallery-5-1270x760.jpg`](producthunt/gallery-5-1270x760.jpg) | 1270 × 760 | Product Hunt launch: Gallery, image 5 | `--only producthunt-gallery-5` |
| [`stripe/product-personal-monthly-1024.jpg`](../stripe/product-personal-monthly-1024.jpg) | 1024 × 1024 | Stripe product Personal, monthly: Image | `--only stripe-personal-monthly` |
| [`stripe/product-personal-yearly-1024.jpg`](../stripe/product-personal-yearly-1024.jpg) | 1024 × 1024 | Stripe product Personal, yearly: Image | `--only stripe-personal-yearly` |
| [`stripe/product-personal-once-1024.jpg`](../stripe/product-personal-once-1024.jpg) | 1024 × 1024 | Stripe product Personal, once: Image | `--only stripe-personal-once` |
| [`stripe/product-team-seat-1024.jpg`](../stripe/product-team-seat-1024.jpg) | 1024 × 1024 | Stripe product Team, per seat: Image | `--only stripe-team-seat` |
| [`stripe/product-team-extra-mac-1024.jpg`](../stripe/product-team-extra-mac-1024.jpg) | 1024 × 1024 | Stripe product Team, extra mac: Image | `--only stripe-team-extra-mac` |
| [`stripe/product-enterprise-1024.jpg`](../stripe/product-enterprise-1024.jpg) | 1024 × 1024 | Stripe product Enterprise, for your company: Image | `--only stripe-enterprise` |
| [`stripe/icon-512.png`](../stripe/icon-512.png) | 512 × 512 | Stripe: Settings, Branding, Icon | `--only stripe-icon` |
| [`stripe/logo-badge-1233x358.png`](../stripe/logo-badge-1233x358.png) | 1233 × 358 | Stripe: Settings, Branding, Logo (light and dark Checkout, invoices) | `--only stripe-logo` |
| [`email/lockup-light-330x72.png`](../email/lockup-light-330x72.png) | 330 × 72 | Email header logo in light mode, shown at 110 x 24 | `--only email-header-light` |
| [`email/mark-light-60.png`](../email/mark-light-60.png) | 60 × 60 | Email footer mark in light mode, shown at 20 x 20 | `--only email-footer-light` |
| [`email/lockup-dark-330x72.png`](../email/lockup-dark-330x72.png) | 330 × 72 | Email header logo in dark mode, shown at 110 x 24 | `--only email-header-dark` |
| [`email/mark-dark-60.png`](../email/mark-dark-60.png) | 60 × 60 | Email footer mark in dark mode, shown at 20 x 20 | `--only email-footer-dark` |
<!-- assets:end -->

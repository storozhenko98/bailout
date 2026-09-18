# Link previews

The homepage, field guide and demo provide page-specific Open Graph and X card metadata
in the initial HTML. Link crawlers do not need JavaScript, cookies, a login, or
access to the model API. Canonical URLs, the public sitemap and robots file remain
consistent. No analytics SDK or third-party script is added.

General previews use a 1200 × 630 PNG illustration with no small text, following
[Apple's Messages guidance](https://developer.apple.com/documentation/technotes/tn3156-create-rich-previews-for-messages/).
X has a separate 1200 × 600 PNG with the headline and `summary_large_image` tags.
The image descriptions are available as alt metadata. All four images are below
100 KB; SVG is retained for browser icons, with 32 px and 512 px PNG fallbacks and
a 180 px Apple touch icon.

Artwork uses the existing brand colors, logo and locally hosted licensed fonts.
Regenerate the share images with `python3 scripts/render-social.py` (Pillow is a
development-only prerequisite). Icon PNGs are raster exports of `site/favicon.svg`;
the Apple touch icon uses an opaque white background. Assets have versioned names
and long cache lifetimes: use a new filename and update metadata when changing
published artwork. The HTML revalidates on requests.

Run `python3 scripts/check-social.py` locally, or add `--live` after deployment to
verify the public HTML under X, Apple, Facebook, LinkedIn, Slack, Discord and
WhatsApp crawler user agents and download every referenced PNG. This proves
resource access and metadata consistency, not an actual published post in each app.
Platforms choose their own presentation and may retain old cached previews.

Do not invent an X account for `twitter:site` or `twitter:creator`. Add those tags
only when there is a verified project or author handle to associate with the site.
Preview metadata improves the shared link's presentation; it does not guarantee
distribution, ranking, clicks, or an immediate refresh of old shared messages.

Metadata follows the [Open Graph protocol](https://ogp.me/).

The `/demo/` page hosts a silent MP4 of a real public-service terminal session,
with a GIF preview in the GitHub README. It reuses the homepage's share artwork.
Video waits are shortened and labeled; the original asciicast retains the full
timing. See [recording and verification](../demo/README.md). No social post is
created by building or deploying these assets.

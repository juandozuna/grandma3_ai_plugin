# Landing page design notes

Static HTML, CSS and a small vanilla JS file (tabs, copy buttons). No build step. Deployed from `docs/` with GitHub Pages.

## Mobbin references that shaped the layout

Looked up with the Mobbin connector (section search):

- Hermes Agent hero (install command next to a strong visual): https://mobbin.com/sites/sections/e9c68c38-072b-403d-8888-26042d246b35
- Giga "pip install is open" (single copyable install pill in a dark section): https://mobbin.com/sites/sections/a2004615-5fee-4e7d-9f84-602f3f1ab1f0
- Overmind "Start working with..." (stepped install with tabs and per-block copy buttons): https://mobbin.com/sites/sections/2f558d9c-c7c4-4d03-bb19-b512ad91a7c2
- Cloudflare Workers "Run any AI model..." (tabbed code panel with side list): https://mobbin.com/sites/sections/acc34c30-3b97-4dac-9d53-92aa20c9a60b
- Linear "Meet your command line" (dark UI panel as the hero object): https://mobbin.com/sites/sections/038755ae-4b0f-4e25-a7df-dca35d71d6bc
- GSAP FAQs (hairline-separated accordion with +/- toggle): https://mobbin.com/sites/sections/7638983f-e6fa-41e0-9f9a-a9da77c97d9a

What was taken: dark near-black ground, a prominent download call to action beside the install path, numbered install steps with tabs and copy buttons, a hairline accordion FAQ.

## Aesthetic

A lighting console at night: near-black panels, amber (tungsten) as the primary accent, cyan and magenta as "gel" accents, monospace labels like console legends, status LEDs on feature cards. Hero and architecture visuals are original SVG (fixture beams, faders, a message strip), animated slowly and disabled under `prefers-reduced-motion`.

## Images

No raster images are used. All visuals are hand-written SVG/CSS.

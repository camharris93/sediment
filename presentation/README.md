# sediment — presentation

A self-contained, single-file scrollytelling site for the **talk portion** of a demo,
before you switch to the live terminal. It is **not** the Streamlit dashboard — it's a
presentation artifact that tells the story (problem → thesis → architecture → trust →
demo).

## Run it

Just open the file — no server, no build, no network:

```
presentation/index.html      ← double-click, or drag into a browser
```

It works fully offline from `file://` (every style, script, and graphic is embedded).

## Present with it

| Key | Action |
|---|---|
| `↓` `→` `Space` | next section |
| `↑` `←` | previous section |
| `Home` / `End` | first / last section |
| click a dot (right edge) | jump to a section |
| `F` or `?` | keyboard help |

Tip: put your browser in fullscreen (F11) for the cleanest look on a projector.

## The 13 sections

intro · the tension · the thesis · the boundary · the layers · cold start ·
AI at the seams · the trust pipeline (L1→L7) · question→infra · why trust it ·
what it unlocks · takeaways · let's demo.

## Editing

Everything is in `index.html`. Copy lives in the `<section>` blocks; the look is the
`<style>` block at the top (colors are CSS variables in `:root`); behavior is the small
`<script>` at the bottom. No toolchain required.

## Publishing (optional)

This folder is committed to the repo, so it travels with every clone. To also serve it as a
**shareable URL**:

1. Enable **GitHub Pages** (Settings → Pages → deploy from `main`) — or host `index.html`
   anywhere static.
2. Keep the content public-safe: no internal system names; keep any internal examples in your
   spoken notes, not in the page.

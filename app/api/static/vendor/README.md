# Vendored front-end dependencies

## motion.min.js — Motion 13.1.1 (MIT)

The library formerly published as Framer Motion. `motion.min.js` is the
package's own `dist/motion.js` UMD build, copied verbatim from npm; it
attaches to `window.Motion` and needs no bundler.

**Vendored rather than loaded from a CDN, deliberately.** This console is a
window onto a private CRM. A `<script src="https://cdn…">` would mean every
operator's browser announcing to a third party, on every page load, that this
tool is being used — and it would put a network dependency between an
internal user and an internal service. The file is 137 KB and changes when we
choose, not when a CDN does.

To update: `npm pack motion@<version>`, unpack, and copy `dist/motion.js`
here. Check `window.Motion` still exposes `animate`, `scroll`, `inView`,
`stagger`, `spring`, `hover` and `press`, which are what index.html uses.

Licence text is in `motion.LICENSE.md`, as MIT requires.

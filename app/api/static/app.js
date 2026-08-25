/* ==================================================================
   The MarQ Communities — agent console, behaviour

   [claude] Split out of index.html on 21 August 2026, alongside app.css.

   Two things about how this file is loaded, both of which it depends on:

   *  **It runs after `motion.min.js`.** `MOTION` is read from
      `window.Motion` at the top of this file, and index.html loads the
      vendored library immediately before this one. Classic scripts execute
      in document order, so that ordering holds — but it means this file
      must not be given `type="module"` or `async` without also changing
      how the library is obtained. (`defer` would be safe; both tags sit at
      the end of `<body>` already, so it would change nothing.)

   *  **It runs after the DOM is parsed**, for the same reason: both tags
      are the last thing in `<body>`. There is no DOMContentLoaded guard
      anywhere below, and the `$("…")` lookups at the top level would
      return null if this ever moved into `<head>`.

   Not a module, deliberately. Modules would mean either a build step or a
   waterfall of network round-trips on a page that is otherwise two files.
   ================================================================== */

const $ = (id) => document.getElementById(id);
let threadId = null, busy = false;

const SUGGESTIONS = [
  "How many deals are there in total?",
  "Which franchise has the highest cancellation rate?",
  "Among qualified leads, how many have gone stale?",
  "What is the total unit area across all contracted deals?",
];

/* ==================================================================
   Motion

   [claude] A thin layer over the vendored library rather than calls to it
   scattered through the file, for two reasons.

   The first is that motion here has to be optional. `motion.min.js` is one
   more file that can 404, be blocked, or fail to parse, and an interface
   whose messages are invisible because an animation library did not load
   is far worse than one that never animated. So every helper below either
   animates or does nothing — it never leaves an element mid-transition,
   and it never sets a starting style the library would have had to clear.
   Animations run *from* a value *to* the element's natural state, so the
   resting state is what CSS already says.

   The second is `prefers-reduced-motion`. The CSS honours it for the
   stylesheet's own transitions, but a JavaScript animation is invisible to
   that rule and would keep moving — the one place the setting is most
   likely to matter. It is checked here, once, for everything.
   ------------------------------------------------------------------
   Durations follow the same idea the rest of the file does: entrances are
   slower than exits, because a thing arriving should be noticed and a
   thing leaving should get out of the way.
   ================================================================== */
const MOTION = window.Motion || null;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const canAnimate = () => !!(MOTION && MOTION.animate) && !reduceMotion.matches;

const EASE_OUT = [0.22, 0.8, 0.3, 1];
const SPRING_SOFT = { type: "spring", stiffness: 320, damping: 34, mass: 0.9 };
const SPRING_SNAP = { type: "spring", stiffness: 520, damping: 38, mass: 0.7 };

/* Animate, or hand the element back to CSS.
 *
 * [claude] The `return null` case has to *undo*, not merely skip.
 *
 * A finished Motion animation commits its end value as an inline style, so
 * an element that has been hidden once carries `opacity: 0` on the element
 * itself. That inline value outranks any stylesheet rule. When animation
 * is then switched off — reduced motion turned on mid-session, or the
 * library failing to load on a later page — the show path had nothing to
 * clear it, and the control stayed invisible with every class correctly
 * applied. The jump-to-latest button did exactly this.
 *
 * So the no-animation branch strips the inline properties this call would
 * have written, which drops the element back to whatever CSS says its
 * resting state is. That is the same contract the animating branch keeps;
 * it just gets there in one frame. */
function anim(el, keyframes, options) {
  if (!el) return null;

  if (!canAnimate()) {
    Object.keys(keyframes || {}).forEach(prop => el.style.removeProperty(prop));
    return null;
  }

  try { return MOTION.animate(el, keyframes, options); }
  catch (e) { console.warn("motion failed", e); return null; }
}

/* Run `after` once an exit animation has had time to finish.
 *
 * [claude] On a timer, not on the animation's own promise, and that is
 * load-bearing. Motion returns controls carrying a `finished` promise, and
 * measured here it does not reliably settle — a 120ms tween still had an
 * unresolved `finished` after 700ms. Anything hanging off it therefore
 * never ran.
 *
 * That was not a cosmetic failure. `showJump(false)` set `hidden = true`
 * inside that callback, so the button never actually hid; the next call to
 * show it hit the "already in this state" guard and returned without doing
 * anything, and jump-to-latest silently stopped working for the rest of
 * the session.
 *
 * So whichever comes first wins: the promise if it resolves, the timer if
 * it does not. State changes are never left waiting on a library's
 * bookkeeping. */
function afterExit(controls, ms, after) {
  if (!controls) { after(); return; }

  let done = false;
  const finish = () => { if (!done) { done = true; after(); } };

  Promise.resolve(controls.finished).then(finish).catch(finish);
  setTimeout(finish, ms + 60);
}

/* An entrance. Safe by construction: the element's resting state is the
   end of every keyframe list, so a missing library means "already there". */
const enter = (el, opts = {}) =>
  anim(el, { opacity: [0, 1], transform: ["translateY(7px)", "translateY(0px)"] },
       { duration: 0.34, ease: EASE_OUT, ...opts });

/* A list, arriving in sequence. Capped because a stagger over 40 rows is
   not choreography, it is a wait — past about ten the eye reads it as the
   interface being slow. */
function enterList(nodes, each = 0.035) {
  const list = Array.from(nodes || []);
  if (!list.length || !canAnimate()) return;
  list.forEach((el, i) =>
    enter(el, { delay: Math.min(i * each, 0.36), duration: 0.3 }));
}

/* ==================================================================
   Announcements

   Spoken state, never spoken tokens — see the note on #announce in the
   markup.
   ================================================================== */
let lastSaid = "";
function announce(text) {
  const box = $("announce");
  if (!box || text === lastSaid) return;
  lastSaid = text;
  box.textContent = text;
}

/* ==================================================================
   Scrolling

   [claude] The old rule was "after anything changes, go to the bottom",
   which meant scrolling up to re-read something during a turn was undone
   by the next token. This keeps the same behaviour while you are at the
   bottom — which is almost always — and gets out of the way the moment
   you are not.

   `pinned` is a state, not a guess: it is set by the scroll event and only
   re-armed when you return to the bottom yourself, or press the button.
   ================================================================== */
const PIN_SLACK = 64;   // px from the bottom that still counts as "at the bottom"
let pinned = true;

function atBottom() {
  const el = $("log");
  return el.scrollHeight - el.scrollTop - el.clientHeight <= PIN_SLACK;
}

// [claude] Tracked in a variable rather than read back off `hidden`.
// Deriving "am I already showing?" from the attribute meant one missed
// write left the guard permanently wrong — see afterExit.
let jumpShown = false;

function showJump(show) {
  const b = $("jump");
  if (!b || show === jumpShown) return;
  jumpShown = show;

  if (show) {
    b.hidden = false;
    b.classList.add("show");
    anim(b, { opacity: [0, 1], transform: ["translate(-50%, 8px)", "translate(-50%, 0px)"] },
         { duration: 0.24, ease: EASE_OUT });
  } else {
    b.classList.remove("show");
    const out = anim(b, { opacity: [1, 0], transform: ["translate(-50%, 0px)", "translate(-50%, 6px)"] },
                     { duration: 0.16, ease: "linear" });
    afterExit(out, 160, () => { if (!jumpShown) b.hidden = true; });
  }
}

/* Only scrolls when the reader has not taken over. */
function scrollDown(force = false) {
  const el = $("log");
  if (!el) return;
  if (force) { pinned = true; showJump(false); }
  if (!pinned) return;
  el.scrollTop = el.scrollHeight;
}

$("log").addEventListener("scroll", () => {
  const bottom = atBottom();
  if (bottom !== pinned) {
    pinned = bottom;
    showJump(!bottom);
  }
}, { passive: true });

$("jump").onclick = () => { scrollDown(true); $("input").focus(); };

/* ==================================================================
   The sidebar as a drawer

   Below 900px the panel is off canvas. Opening it is a focus change as
   much as a visual one: a drawer that traps nothing and returns focus
   nowhere is a keyboard dead end.
   ================================================================== */
const isDrawer = () => window.matchMedia("(max-width: 900px)").matches;
let drawerOpen = false, drawerReturn = null;

function setDrawer(open) {
  if (!isDrawer() && open) return;
  const aside = $("sidebar"), scrim = $("scrim"), btn = $("menu");

  drawerOpen = open;
  btn.setAttribute("aria-expanded", String(open));
  aside.classList.toggle("open", open);

  if (open) {
    drawerReturn = document.activeElement;
    scrim.hidden = false;
    scrim.classList.add("open");
    anim(scrim, { opacity: [0, 1] }, { duration: 0.2, ease: "linear" });
    anim(aside, { transform: ["translateX(-100%)", "translateX(0px)"] }, SPRING_SNAP);
    // The first thing in the panel, so the panel is where you are.
    ($("new") || aside).focus({ preventScroll: true });
  } else {
    scrim.classList.remove("open");
    const fade = anim(scrim, { opacity: [1, 0] }, { duration: 0.16, ease: "linear" });
    afterExit(fade, 160, () => { if (!drawerOpen) scrim.hidden = true; });
    anim(aside, { transform: ["translateX(0px)", "translateX(-100%)"] },
         { duration: 0.22, ease: EASE_OUT });
    if (drawerReturn && document.contains(drawerReturn)) drawerReturn.focus({ preventScroll: true });
    drawerReturn = null;
  }
}

$("menu").onclick = () => setDrawer(!drawerOpen);
$("scrim").onclick = () => setDrawer(false);

/* Keep the two layouts from contradicting each other: growing past the
   threshold leaves the panel in the column, where "open" is meaningless. */
window.matchMedia("(max-width: 900px)").addEventListener("change", (e) => {
  if (!e.matches && drawerOpen) {
    drawerOpen = false;
    $("sidebar").classList.remove("open");
    $("sidebar").style.transform = "";
    $("scrim").hidden = true;
    $("scrim").classList.remove("open");
    $("menu").setAttribute("aria-expanded", "false");
  }
});

/* A drawer is modal, so Tab must not walk out the back of it. */
function trapTab(e, container) {
  if (e.key !== "Tab") return;
  const focusable = container.querySelectorAll(
    'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'
  );
  const items = Array.from(focusable).filter(el => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ---------------- token ---------------- */
/* [claude] A token from the server, when the deployment offers one.
 *
 * `DEV_UI_TOKEN` in the environment file is served by `/app-config.js`,
 * which exists only outside production — so on a production host this is
 * `undefined` and the console behaves exactly as it did before.
 *
 * A token already saved here wins. Someone who pasted a specific identity
 * to test something is not expecting a page reload to sign them back in as
 * somebody else.
 */
$("token").value =
  localStorage.getItem("marq_token") || window.MARQ_DEV_TOKEN || "";

if (!localStorage.getItem("marq_token") && window.MARQ_DEV_TOKEN) {
  localStorage.setItem("marq_token", window.MARQ_DEV_TOKEN);
}
$("token").addEventListener("change", () => {
  localStorage.setItem("marq_token", $("token").value.trim());
  renderIdentity();
  refreshAll();
  if ($("token").value.trim()) toggleToken(false);
});

function toggleToken(open) {
  const panel = $("tokenEdit"), btn = $("whoBtn");
  const next = open === undefined ? !panel.classList.contains("open") : open;
  panel.classList.toggle("open", next);
  btn.classList.toggle("open", next);
  if (next) $("token").focus();
}
$("whoBtn").onclick = () => toggleToken();
const auth = () => {
  const t = $("token").value.trim();
  return t ? { Authorization: "Bearer " + t } : {};
};

/* ------------------------------------------------------------------
   Identity, read from the token.

   [claude] A JWT payload is base64, not encrypted — the subject is right
   there, so the sidebar can say who you are signed in as instead of
   showing a password field forever. That is the difference between a
   product and a config screen: credentials are setup, used once.

   Decoding is for display only. Nothing here is trusted — the server
   verifies the signature, and a tampered token simply fails there.
   ------------------------------------------------------------------ */
function tokenSubject(token) {
  try {
    const [, payload] = token.split(".");
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const claims = JSON.parse(json);
    const exp = claims.exp ? new Date(claims.exp * 1000) : null;
    return { subject: claims.sub || null, exp };
  } catch { return { subject: null, exp: null }; }
}

function renderIdentity() {
  const raw = $("token").value.trim();
  const { subject, exp } = raw ? tokenSubject(raw) : { subject: null, exp: null };
  const expired = exp && exp.getTime() < Date.now();

  if (!subject) {
    $("avatar").textContent = "—";
    $("whoName").textContent = "Not signed in";
    $("whoHint").textContent = "Add an access token";
    return;
  }

  $("avatar").textContent = subject.trim()[0] || "?";
  $("whoName").textContent = subject;
  $("whoHint").textContent = expired
    ? "Token expired"
    : (exp ? "Valid to " + exp.toLocaleDateString(undefined,
        { day: "numeric", month: "short" }) : "Signed in");
}
const hasToken = () => !!$("token").value.trim();

/* ---------------- rendering ---------------- */
function bubble(role, text) {
  $("empty")?.remove();
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.innerHTML = `<div class="who"><span class="name">${role === "user" ? "You" : "The MarQ"}</span></div>
                  <div class="bubble"></div>`;
  const b = el.querySelector(".bubble");
  if (role === "agent" && text) b.innerHTML = renderMarkdown(text);
  else b.textContent = text || "";
  $("log").querySelector(".wrap").appendChild(el);

  if (role === "user" && text) addUserActions(el, text);

  anim(el, { opacity: [0, 1], transform: ["translateY(8px)", "translateY(0px)"] },
       SPRING_SOFT);

  scrollDown();
  return el;
}

/* ------------------------------------------------------------------
   Everything technical, revealed only after the answer settles.

   Nothing about tools, routes or SQL appears while the answer streams. It
   is how the machine works, not what was asked, and a conversation that
   narrates its own plumbing reads as a console rather than an assistant.

   It is not discarded though: the provenance feature exists so a number can
   be checked rather than trusted, and that only works if the query stays
   reachable. So it moves here — one quiet control under the finished
   answer, closed by default.
   ------------------------------------------------------------------ */
const SPECIALIST_NAMES = {
  deals: "Deals", leads: "Leads", workspace: "Your files",
  research: "Web research", general: "General",
};
const labelFor = (r) => SPECIALIST_NAMES[r] || r;

function pill(text, cls) {
  const s = document.createElement("span");
  s.className = "pill " + cls;
  s.textContent = text;
  return s;
}


/* ==================================================================
   What you can do with a message that has already been said

   [claude] Copy was the only one, and the two obvious neighbours were
   missing: asking the same thing again when an answer routed badly, and
   fixing a typo without retyping the sentence.

   Neither rewrites history, and that is deliberate rather than lazy.
   Every turn is recorded server-side in `conversation_turns` with the SQL
   behind it, and this codebase's whole provenance argument is that the
   record can be checked rather than trusted. A UI that quietly replaced a
   question with a better one would be showing a transcript that never
   happened. So Retry asks again as a new turn, and Edit loads the text
   back into the composer — the thread gains a turn, it never loses one.
   ================================================================== */
function actionButton(label, mark, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.innerHTML = `<span class="gem" aria-hidden="true">${mark}</span> <span class="t"></span>`;
  b.querySelector(".t").textContent = label;
  b.onclick = onClick;
  return b;
}

function addActions(el, { answer, question }, bar) {
  const own = !bar;
  bar = bar || document.createElement("div");
  if (own) bar.className = "after";

  el.dataset.answer = answer || "";

  const copy = actionButton("Copy", "◇", async () => {
    const t = copy.querySelector(".t");
    try {
      await navigator.clipboard.writeText(el.dataset.answer || "");
      t.textContent = "Copied";
      anim(copy, { transform: ["scale(.94)", "scale(1)"] }, SPRING_SNAP);
      setTimeout(() => { t.textContent = "Copy"; }, 1600);
    } catch { toast("Could not copy to the clipboard", true); }
  });
  bar.appendChild(copy);

  const q = question || el.dataset.question;
  if (q) {
    bar.appendChild(actionButton("Retry", "↻", () => {
      if (busy) { toast("Still answering — stop that turn first.", true); return; }
      send(q);
    }));
  }

  if (own) { el.appendChild(bar); enter(bar, { duration: 0.28 }); }
  return bar;
}

/* Editing a question puts it back in the composer rather than in place —
   see the note above on why the transcript is not rewritten. */
function addUserActions(el, text) {
  const bar = document.createElement("div");
  bar.className = "after";
  bar.appendChild(actionButton("Edit", "✎", () => {
    if (busy) { toast("Still answering — stop that turn first.", true); return; }
    const box = $("input");
    box.value = text;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 170) + "px";
    anim(box, { transform: ["scale(.995)", "scale(1)"] }, SPRING_SNAP);
    announce("Question loaded into the composer.");
  }));
  bar.appendChild(actionButton("Ask again", "↻", () => {
    if (busy) { toast("Still answering — stop that turn first.", true); return; }
    send(text);
  }));
  el.appendChild(bar);
}

function attachDetails(el, { specialists, tools, provenance, answer, charts }) {
  // Charts sit with the answer, above the technical detail: they are part
  // of what was said, not part of how it was worked out.
  (charts || []).forEach(spec => {
    try { el.appendChild(renderChart(spec)); }
    catch (e) { console.warn("chart failed to render", e); }
  });

  const bar = document.createElement("div");
  bar.className = "after";

  addActions(el, { answer }, bar);

  const rows = [];
  if (specialists && specialists.length) rows.push(["Answered by", specialists.map(labelFor), "who"]);
  if (tools && tools.length) rows.push(["Steps", tools.map(t => t.replace(/_/g, " ")), "tool"]);

  let panel = null;

  if (rows.length || (provenance && provenance.length)) {
    panel = document.createElement("div");
    panel.className = "details";

    rows.forEach(([cap, items, cls]) => {
      const row = document.createElement("div");
      row.className = "row";
      const c = document.createElement("span");
      c.className = "cap"; c.textContent = cap;
      row.appendChild(c);
      items.forEach(t => row.appendChild(pill(t, cls)));
      panel.appendChild(row);
    });

    (provenance || []).forEach((rec, i) => {
      const q = document.createElement("div");
      q.className = "q";
      const head = document.createElement("div");
      head.className = "qh";
      head.textContent = rec.sql
        ? (provenance.length > 1 ? `Query ${i + 1}` : "The query")
        : "No query ran";
      q.appendChild(head);

      if (rec.sql) {
        const pre = document.createElement("pre");
        pre.innerHTML = sqlHtml(rec.sql);
        q.appendChild(pre);
        const m = document.createElement("div");
        m.className = "meta" + (rec.truncated ? " warn" : "");
        m.textContent = (rec.rows_available ?? "?") + " row(s) matched"
          + (rec.truncated ? " · truncated — a total from this would be wrong" : "");
        q.appendChild(m);
      } else if (rec.refused) {
        const d = document.createElement("div");
        d.className = "declined"; d.textContent = rec.refused;
        q.appendChild(d);
      }
      panel.appendChild(q);
    });

    const sep = document.createElement("span");
    sep.className = "sep";
    sep.setAttribute("aria-hidden", "true");
    bar.appendChild(sep);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.innerHTML = '<span class="gem" aria-hidden="true">▸</span> Details';
    toggle.setAttribute("aria-expanded", "false");
    toggle.onclick = () => {
      const open = panel.classList.toggle("open");
      toggle.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", String(open));
      if (open) { enter(panel, { duration: 0.26 }); scrollDown(); }
    };
    bar.appendChild(toggle);
  }

  el.appendChild(bar);
  enter(bar, { duration: 0.28 });
  if (panel) el.appendChild(panel);
  scrollDown();
}

/* ------------------------------------------------------------------
   Chart rendering.

   Built to fixed mark specs rather than taste: bars capped at 24px with a
   4px rounded data-end square at the baseline, 2px lines, 8px markers, a
   2px surface gap between touching marks, hairline solid axes one step off
   the surface, and text in ink tokens — never in the series colour, which
   is illegible as type.

   Labels are selective: a value rides each bar because there are few of
   them, but a line labels only its endpoint. Every value is in the table
   view regardless, so nothing is readable only as a length.
   ------------------------------------------------------------------ */
const CHART_COLORS = ["#A82F48", "#9E7410", "#00795F", "#4A4FA8"];
const SURFACE = "#ffffff";

// [claude] toLocaleString with a max, not toFixed: toFixed(2) rendered
// 34.6 as "34.60" and 13 as "13.00", which reads as false precision — a
// rate quoted to two decimals it does not have.
const fmtNum = (n) =>
  Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
const fmtVal = (n, suffix) => fmtNum(n) + (suffix || "");

function svgEl(name, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/* A rounded-at-one-end bar: square on the baseline, 4px round at the tip. */
function barPath(x, y, w, h, r, vertical) {
  const rr = Math.max(0, Math.min(r, vertical ? h : w));
  if (vertical) {
    return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y}` +
           ` L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr}` +
           ` L${x + w},${y + h} Z`;
  }
  return `M${x},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr}` +
         ` L${x + w},${y + h - rr} Q${x + w},${y + h} ${x + w - rr},${y + h}` +
         ` L${x},${y + h} Z`;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  return Math.ceil(v / mag * 2) / 2 * mag;
}


/* ==================================================================
   Telling series apart without colour

   [claude] The palette was computed to clear 3:1 against both surfaces,
   which makes each series *visible*. It does not make them
   *distinguishable*: to a reader with deuteranopia the burgundy and the
   green in this palette converge, and every chart here identified its
   series by colour and nothing else. That is the one chart anti-pattern
   worth fixing before any other.

   So each series after the first also carries a texture. Applied by
   walking the finished SVG and swapping any fill that matches a palette
   entry, rather than by threading a pattern id through four different
   geometry builders — the mark specs in those functions are measured, and
   this needs to not touch them.

   Series 0 stays solid on purpose. A chart where everything is hatched is
   noisier than one where the texture means "this is the other one".
   ================================================================== */
let patternSeq = 0;

function patternFor(color, i, uid) {
  const id = `hatch-${uid}-${i}`;
  const p = svgEl("pattern", {
    id, width: 6, height: 6, patternUnits: "userSpaceOnUse",
  });
  if (i === 3) p.setAttribute("patternTransform", "rotate(45)");
  p.appendChild(svgEl("rect", { width: 6, height: 6, fill: color }));

  if (i === 1) {
    // Diagonal rule.
    p.appendChild(svgEl("path", {
      d: "M0 6 L6 0 M-1 1 L1 -1 M5 7 L7 5",
      stroke: SURFACE, "stroke-width": 1.4, opacity: .78,
    }));
  } else if (i === 2) {
    // Dotted.
    p.appendChild(svgEl("circle", { cx: 3, cy: 3, r: 1.25, fill: SURFACE, opacity: .82 }));
  } else if (i === 3) {
    // Grid, rotated 45° by the pattern transform above.
    p.appendChild(svgEl("path", {
      d: "M0 3 H6 M3 0 V6",
      stroke: SURFACE, "stroke-width": 1.1, opacity: .7,
    }));
  }
  return p;
}

function applyPatterns(svg) {
  const uid = ++patternSeq;

  // [claude] The swap happens BEFORE the defs are inserted, and the order
  // is the entire correctness argument.
  //
  // Done the other way round, this walk reaches inside the <defs> it just
  // added and rewrites each pattern's own background rect — which is
  // filled with the very palette colour being matched — into a reference
  // to the pattern it belongs to. A self-referencing pattern is not an
  // error: it renders as nothing at all, so the second series simply
  // vanished while every fill attribute still looked correct in the DOM.
  svg.querySelectorAll("[fill]").forEach(el => {
    const i = CHART_COLORS.indexOf(el.getAttribute("fill"));
    if (i > 0) el.setAttribute("fill", `url(#hatch-${uid}-${i})`);
  });

  const defs = svgEl("defs");
  CHART_COLORS.forEach((c, i) => { if (i > 0) defs.appendChild(patternFor(c, i, uid)); });
  svg.insertBefore(defs, svg.firstChild);

  return uid;
}

/* The legend has to carry the same texture, or it explains a chart that
   no longer looks like it. */
function swatchStyle(swatch, i, uid) {
  swatch.style.background = CHART_COLORS[i % CHART_COLORS.length];
  const idx = i % CHART_COLORS.length;
  if (idx === 1) {
    swatch.style.backgroundImage =
      `repeating-linear-gradient(45deg, ${SURFACE} 0 1.4px, transparent 1.4px 4.2px)`;
  } else if (idx === 2) {
    swatch.style.backgroundImage = `radial-gradient(${SURFACE} 1.1px, transparent 1.2px)`;
    swatch.style.backgroundSize = "4px 4px";
  } else if (idx === 3) {
    swatch.style.backgroundImage =
      `repeating-linear-gradient(0deg, ${SURFACE} 0 1px, transparent 1px 4px),`
      + `repeating-linear-gradient(90deg, ${SURFACE} 0 1px, transparent 1px 4px)`;
  }
}

/* ==================================================================
   Taking a chart out of the browser

   [claude] Two exits, because they answer different questions. PNG is for
   putting a picture in a deck; the values are for checking the arithmetic
   somewhere else, which is the same argument the provenance panel makes
   about SQL.

   The PNG path inlines computed styles before serialising. Axis ticks,
   category text and value labels take their fill and font from the
   stylesheet, and a serialised SVG carries none of it — exported without
   this the chart came out with black Times labels on a transparent
   ground, which looks like a rendering bug rather than an export.
   ================================================================== */
const INLINE_PROPS = ["fill", "stroke", "stroke-width", "font-size",
                      "font-family", "font-weight", "opacity"];

function inlineStyles(source, clone) {
  const from = source.querySelectorAll("*");
  const to = clone.querySelectorAll("*");
  for (let i = 0; i < from.length; i++) {
    const cs = getComputedStyle(from[i]);
    let css = "";
    INLINE_PROPS.forEach(p => {
      const v = cs.getPropertyValue(p);
      if (v) css += `${p}:${v};`;
    });
    to[i].setAttribute("style", css);
  }
}

async function exportPng(svg, title) {
  const clone = svg.cloneNode(true);
  const rect = svg.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect.width));
  const h = Math.max(1, Math.round(rect.height));

  inlineStyles(svg, clone);
  clone.setAttribute("width", w);
  clone.setAttribute("height", h);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");

  const blob = new Blob([new XMLSerializer().serializeToString(clone)],
                        { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);

  try {
    const img = new Image();
    await new Promise((ok, fail) => {
      img.onload = ok;
      img.onerror = () => fail(new Error("the chart could not be rasterised"));
      img.src = url;
    });

    // 2x, so it is not soft on the screen it will be shown on.
    const scale = 2;
    const canvas = document.createElement("canvas");
    canvas.width = w * scale;
    canvas.height = h * scale;
    const ctx = canvas.getContext("2d");
    // The charts are drawn for a light surface; a transparent PNG dropped
    // on a dark slide would lose every label.
    ctx.fillStyle = "#FFFFFF";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

    const png = await new Promise(r => canvas.toBlob(r, "image/png"));
    if (!png) throw new Error("the image could not be encoded");

    const href = URL.createObjectURL(png);
    const a = document.createElement("a");
    a.href = href;
    a.download = (title || "chart").replace(/[^\w\- ]+/g, "").trim().slice(0, 60) + ".png";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 2000);
    toast("Chart saved as a PNG.");
  } catch (e) {
    toast("Could not export this chart: " + e.message, true);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* Tab-separated, so it pastes straight into a spreadsheet cell grid. */
function chartTsv(spec) {
  const head = ["Category", ...spec.series.map(s => s.name)].join("\t");
  const rows = spec.labels.map((label, i) =>
    [label, ...spec.series.map(s => fmtVal(s.values[i], spec.value_suffix))].join("\t"));
  return [head, ...rows].join("\n");
}

function renderChart(spec) {
  const box = document.createElement("div");
  box.className = "chart";

  const h = document.createElement("h5");
  h.textContent = spec.title;
  box.appendChild(h);

  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = spec.series.map(s => s.name).join(" · ");
  box.appendChild(sub);

  const rel = document.createElement("div");
  rel.className = "rel";
  box.appendChild(rel);

  const tip = document.createElement("div");
  tip.className = "tip";
  rel.appendChild(tip);

  const showTip = (evt, text) => {
    tip.textContent = text;
    const r = rel.getBoundingClientRect();
    tip.style.left = (evt.clientX - r.left) + "px";
    tip.style.top = (evt.clientY - r.top) + "px";
    tip.classList.add("show");
  };
  const hideTip = () => tip.classList.remove("show");

  const svg = spec.kind === "donut" ? donutSvg(spec, showTip, hideTip)
            : spec.kind === "line"  ? lineSvg(spec, showTip, hideTip)
            : spec.kind === "bar"   ? barSvg(spec, showTip, hideTip)
            :                         columnSvg(spec, showTip, hideTip);
  const uid = applyPatterns(svg);

  // [claude] The picture is decorative to a screen reader — the table
  // below carries every value — but it should still say what it is rather
  // than announcing an unlabelled graphic.
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    `${spec.title}. ${spec.labels.length} categories. The values follow in a table.`);
  rel.appendChild(svg);

  // Legend for two or more series; one series needs none — the title says it.
  if (spec.series.length > 1) {
    const leg = document.createElement("div");
    leg.className = "legend";
    spec.series.forEach((se, i) => {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatchStyle(swatch, i, uid);
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(se.name));
      leg.appendChild(item);
    });
    box.appendChild(leg);
  }

  // Table view — the accessibility floor, and how a number gets checked.
  //
  // [claude] Wrapped in its own scroll box. A chart with long category
  // names produced a table wider than a phone, and the *page* scrolled
  // sideways to accommodate it — which breaks every other column on the
  // screen to show one table.
  const tableBox = document.createElement("div");
  tableBox.className = "scroll-x";

  const table = document.createElement("table");
  const cap = document.createElement("caption");
  cap.textContent = spec.title;
  table.appendChild(cap);

  const head = document.createElement("tr");
  head.innerHTML = '<th scope="col">Category</th>' +
    spec.series.map(se =>
      `<th class="n" scope="col" style="text-align:right">${mdEscape(se.name)}</th>`).join("");
  table.appendChild(head);
  spec.labels.forEach((label, i) => {
    const tr = document.createElement("tr");
    const td = document.createElement("th");
    td.setAttribute("scope", "row");
    td.style.fontWeight = "400";
    td.style.textTransform = "none";
    td.style.letterSpacing = "normal";
    td.style.fontSize = "11.5px";
    td.style.color = "var(--ink)";
    td.textContent = label;
    tr.appendChild(td);
    spec.series.forEach(se => {
      const cell = document.createElement("td");
      cell.className = "n";
      cell.textContent = fmtVal(se.values[i], spec.value_suffix);
      tr.appendChild(cell);
    });
    table.appendChild(tr);
  });

  tableBox.appendChild(table);

  const foot = document.createElement("div");
  foot.className = "foot";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = "Show values";
  toggle.setAttribute("aria-expanded", "false");
  toggle.onclick = () => {
    const open = table.classList.toggle("open");
    toggle.textContent = open ? "Hide values" : "Show values";
    toggle.setAttribute("aria-expanded", String(open));
    if (open) { enter(tableBox, { duration: 0.24 }); scrollDown(); }
  };
  foot.appendChild(toggle);

  const copyData = document.createElement("button");
  copyData.type = "button";
  copyData.textContent = "Copy data";
  copyData.onclick = async () => {
    try {
      await navigator.clipboard.writeText(chartTsv(spec));
      copyData.textContent = "Copied";
      setTimeout(() => { copyData.textContent = "Copy data"; }, 1600);
    } catch { toast("Could not copy to the clipboard", true); }
  };
  foot.appendChild(copyData);

  const png = document.createElement("button");
  png.type = "button";
  png.textContent = "Save PNG";
  png.onclick = () => exportPng(svg, spec.title);
  foot.appendChild(png);

  box.appendChild(foot);
  box.appendChild(tableBox);

  return box;
}

/* ---- horizontal bars: best when category names are long ---- */
function barSvg(spec, showTip, hideTip) {
  const n = spec.labels.length, series = spec.series;
  const rowH = 30, gap = 2, padL = 128, padR = 62, padT = 6;
  const height = n * rowH + padT + 8;
  const width = 640;
  const plotW = width - padL - padR;
  const max = niceMax(Math.max(...series.flatMap(s => s.values.map(Math.abs)), 0));
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });

  svg.appendChild(svgEl("line", {
    x1: padL, y1: padT, x2: padL, y2: height - 8, class: "axis",
  }));

  spec.labels.forEach((label, i) => {
    const top = padT + i * rowH;
    const t = svgEl("text", { x: padL - 10, y: top + rowH / 2 + 3,
                              "text-anchor": "end", class: "cat" });
    t.textContent = label.length > 20 ? label.slice(0, 19) + "…" : label;
    svg.appendChild(t);

    const band = rowH - 8;
    const thick = Math.min(24, (band - (series.length - 1) * gap) / series.length);

    series.forEach((se, k) => {
      const v = se.values[i];
      const w = Math.max(1, (Math.abs(v) / max) * plotW);
      const y = top + (rowH - (thick * series.length + gap * (series.length - 1))) / 2
                + k * (thick + gap);
      const path = svgEl("path", {
        d: barPath(padL, y, w, thick, 4, false),
        fill: CHART_COLORS[k % CHART_COLORS.length],
      });
      svg.appendChild(path);

      if (series.length === 1 && n <= 10) {
        const lab = svgEl("text", { x: padL + w + 8, y: y + thick / 2 + 3.5, class: "val" });
        lab.textContent = fmtVal(v, spec.value_suffix);
        svg.appendChild(lab);
      }

      const hit = svgEl("rect", { x: padL, y, width: Math.max(w, 8), height: thick, class: "hit" });
      hit.addEventListener("mousemove", e =>
        showTip(e, `${label} · ${se.name} ${fmtVal(v, spec.value_suffix)}`));
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
  });

  return svg;
}

/* ---- vertical columns: time, or few categories ---- */
function columnSvg(spec, showTip, hideTip) {
  const n = spec.labels.length, series = spec.series;
  // [claude] The band is capped as well as the bar. Bars cap at 24px, and
  // with four categories across a 640-wide plot that leaves 160px bands —
  // technically correct and visually sparse, the marks lost in their own
  // whitespace. Capping the band keeps the air proportionate.
  const BAND_MAX = 92;
  const padL = 46, padR = 14, padT = 22, padB = 40;
  const width = Math.min(640, padL + padR + n * BAND_MAX);
  const height = 260;
  const plotW = width - padL - padR, plotH = height - padT - padB;
  const max = niceMax(Math.max(...series.flatMap(s => s.values.map(Math.abs)), 0));
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });

  [0, 0.5, 1].forEach(f => {
    const y = padT + plotH - f * plotH;
    svg.appendChild(svgEl("line", { x1: padL, y1: y, x2: width - padR, y2: y, class: "axis" }));
    const t = svgEl("text", { x: padL - 8, y: y + 3.5, "text-anchor": "end", class: "tick" });
    t.textContent = fmtNum(max * f);
    svg.appendChild(t);
  });

  const band = plotW / n, gap = 2;
  spec.labels.forEach((label, i) => {
    const thick = Math.min(24, (band * 0.62 - (series.length - 1) * gap) / series.length);
    const groupW = thick * series.length + gap * (series.length - 1);
    const x0 = padL + band * i + (band - groupW) / 2;

    series.forEach((se, k) => {
      const v = se.values[i];
      const bh = Math.max(1, (Math.abs(v) / max) * plotH);
      const x = x0 + k * (thick + gap);
      const y = padT + plotH - bh;
      svg.appendChild(svgEl("path", {
        d: barPath(x, y, thick, bh, 4, true),
        fill: CHART_COLORS[k % CHART_COLORS.length],
      }));

      // Selective labelling: past eight categories the values crowd each
      // other and the axis plus the tooltip carry them instead.
      if (series.length === 1 && n <= 8) {
        const lab = svgEl("text", { x: x + thick / 2, y: y - 7,
                                    "text-anchor": "middle", class: "val" });
        lab.textContent = fmtVal(v, spec.value_suffix);
        svg.appendChild(lab);
      }

      const hit = svgEl("rect", { x, y: padT, width: thick, height: plotH, class: "hit" });
      hit.addEventListener("mousemove", e =>
        showTip(e, `${label} · ${se.name} ${fmtVal(v, spec.value_suffix)}`));
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });

    const t = svgEl("text", { x: padL + band * i + band / 2, y: height - padB + 18,
                              "text-anchor": "middle", class: "cat" });
    t.textContent = label.length > 12 ? label.slice(0, 11) + "…" : label;
    svg.appendChild(t);
  });

  return svg;
}

/* ---- line: change over time. Labels only the endpoint. ---- */
function lineSvg(spec, showTip, hideTip) {
  const n = spec.labels.length, series = spec.series;
  const width = 640, height = 250;
  const padL = 46, padR = 54, padT = 20, padB = 38;
  const plotW = width - padL - padR, plotH = height - padT - padB;
  const max = niceMax(Math.max(...series.flatMap(s => s.values.map(Math.abs)), 0));
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });

  [0, 0.5, 1].forEach(f => {
    const y = padT + plotH - f * plotH;
    svg.appendChild(svgEl("line", { x1: padL, y1: y, x2: width - padR, y2: y, class: "axis" }));
    const t = svgEl("text", { x: padL - 8, y: y + 3.5, "text-anchor": "end", class: "tick" });
    t.textContent = fmtNum(max * f);
    svg.appendChild(t);
  });

  const xAt = (i) => padL + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v) => padT + plotH - (Math.abs(v) / max) * plotH;

  series.forEach((se, k) => {
    const color = CHART_COLORS[k % CHART_COLORS.length];
    const d = se.values.map((v, i) => `${i ? "L" : "M"}${xAt(i)},${yAt(v)}`).join(" ");
    svg.appendChild(svgEl("path", {
      d, fill: "none", stroke: color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));

    se.values.forEach((v, i) => {
      // 2px surface ring so markers stay legible where they overlap.
      svg.appendChild(svgEl("circle", {
        cx: xAt(i), cy: yAt(v), r: 4, fill: color,
        stroke: SURFACE, "stroke-width": 2,
      }));
      const hit = svgEl("circle", { cx: xAt(i), cy: yAt(v), r: 12, class: "hit" });
      hit.addEventListener("mousemove", e =>
        showTip(e, `${spec.labels[i]} · ${se.name} ${fmtVal(v, spec.value_suffix)}`));
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });

    const last = se.values.length - 1;
    const lab = svgEl("text", { x: xAt(last) + 10, y: yAt(se.values[last]) + 3.5, class: "val" });
    lab.textContent = fmtVal(se.values[last], spec.value_suffix);
    svg.appendChild(lab);
  });

  spec.labels.forEach((label, i) => {
    if (n > 8 && i % 2) return;
    const t = svgEl("text", { x: xAt(i), y: height - padB + 18,
                              "text-anchor": "middle", class: "cat" });
    t.textContent = label.length > 10 ? label.slice(0, 9) + "…" : label;
    svg.appendChild(t);
  });

  return svg;
}

/* ---- donut: share of one whole ---- */
function donutSvg(spec, showTip, hideTip) {
  const values = spec.series[0].values;
  const total = values.reduce((a, b) => a + b, 0) || 1;
  const size = 260, cx = 130, cy = 130, rOuter = 100, rInner = 62;
  const svg = svgEl("svg", { viewBox: `0 0 ${size + 300} ${size}`, role: "img" });

  let angle = -Math.PI / 2;
  const GAP = 0.016;   // the 2px surface gap, in radians at this radius

  values.forEach((v, i) => {
    const frac = v / total;
    const a0 = angle + GAP / 2, a1 = angle + frac * Math.PI * 2 - GAP / 2;
    angle += frac * Math.PI * 2;
    if (a1 <= a0) return;

    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (r, a) => `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
    const d = `M${p(rOuter, a0)} A${rOuter},${rOuter} 0 ${large} 1 ${p(rOuter, a1)}`
            + ` L${p(rInner, a1)} A${rInner},${rInner} 0 ${large} 0 ${p(rInner, a0)} Z`;

    const seg = svgEl("path", { d, fill: CHART_COLORS[i % CHART_COLORS.length] });
    seg.addEventListener("mousemove", e => showTip(e,
      `${spec.labels[i]} · ${fmtVal(v, spec.value_suffix)} (${(frac * 100).toFixed(1)}%)`));
    seg.addEventListener("mouseleave", hideTip);
    svg.appendChild(seg);
  });

  // Direct labels beside the ring rather than on the slices — a label that
  // does not fit inside a segment is never clipped.
  values.forEach((v, i) => {
    const y = 26 + i * 22;
    svg.appendChild(svgEl("rect", {
      x: size + 6, y: y - 9, width: 10, height: 10, rx: 2,
      fill: CHART_COLORS[i % CHART_COLORS.length],
    }));
    const t = svgEl("text", { x: size + 24, y, class: "cat" });
    t.textContent = `${spec.labels[i]} — ${fmtVal(v, spec.value_suffix)}`
                    + ` (${((v / total) * 100).toFixed(1)}%)`;
    svg.appendChild(t);
  });

  return svg;
}

function toast(text, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = text;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => t.remove(), 400);
  }, 4200);
}

/* ------------------------------------------------------------------
   Making the stream visible.

   The transport genuinely streams — measured in the browser, 250 tokens
   across 147 distinct arrival times. The problem is perceptual: a short
   answer's tokens land inside ~20ms, which is faster than anyone can see,
   so a real stream rendered raw looks exactly like a blob.

   So text is drained at a readable pace rather than painted the instant it
   arrives. The rate is adaptive: it clears whatever is queued within about
   a second, so a long answer never lags behind the network — this slows
   the *first* characters into view, never the last.
   ------------------------------------------------------------------ */
const TYPE_FLOOR = 55;      // characters per second, minimum
const CATCH_UP   = 0.9;     // seconds allowed to clear a backlog

class Typer {
  constructor(el) {
    this.el = el; this.queue = ""; this.shown = "";
    this.ended = false; this.last = null; this.raf = null; this.onDone = null;
    Typer.active = this;
  }
  push(text) { this.queue += text; this.kick(); }
  end(cb) { this.ended = true; this.onDone = cb; this.kick(); }
  flush() {                       // error, or the user moved on
    this.shown += this.queue; this.queue = "";
    this.paint(false); this.settle();
  }
  kick() {
    if (this.raf !== null) return;

    // [claude] requestAnimationFrame does not fire in a hidden tab.
    //
    // Found the hard way: with the tab backgrounded the typer stalled
    // completely — no text, and because `settle()` runs from inside a
    // frame, the completion callback never fired either, so the finished
    // answer and its details were never rendered at all. Someone who
    // switched tabs mid-answer came back to an empty bubble and a timer
    // still counting.
    //
    // Nobody is watching a hidden tab animate, so there is nothing to pace:
    // paint the lot and settle.
    if (document.hidden) { this.flush(); return; }

    this.raf = requestAnimationFrame(t => this.tick(t));
  }
  tick(now) {
    this.raf = null;
    const dt = this.last === null ? 1 / 60 : Math.min((now - this.last) / 1000, 0.25);
    this.last = now;

    if (this.queue.length) {
      const rate = Math.max(TYPE_FLOOR, this.queue.length / CATCH_UP);
      const take = Math.max(1, Math.round(rate * dt));
      this.shown += this.queue.slice(0, take);
      this.queue = this.queue.slice(take);
      this.paint(true);
    }
    if (this.queue.length) this.kick();
    else if (this.ended) { this.paint(false); this.settle(); }
  }
  paint(caret) {
    this.el.textContent = this.shown;
    if (caret) this.el.appendChild(document.createElement("span")).className = "caret";
    scrollDown();
  }
  settle() {
    if (Typer.active === this) Typer.active = null;
    const cb = this.onDone; this.onDone = null;
    if (cb) cb(this.shown);
  }
}

// The typer for the turn in flight, so hiding the tab can finish it.
Typer.active = null;

document.addEventListener("visibilitychange", () => {
  if (document.hidden && Typer.active) Typer.active.flush();
});



/* The agent writes light markdown — bold, bullets, the occasional heading.
   Rendering it here rather than showing raw asterisks. Deliberately tiny and
   escape-first: the answer contains CRM data and, in a reconciliation, text
   that came out of a file somebody uploaded, so nothing is ever trusted as
   HTML. Only the four patterns the agent actually produces are handled. */
function mdEscape(t) {
  return t.replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
}
/* [claude] Inline spans. Escaped first, always — the text is CRM data and,
   in a reconciliation, content out of a file a third party wrote. */
const mdInline = (l) =>
  l.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
   .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
   .replace(/`([^`]+)`/g, "<code>$1</code>");

/* A row of a pipe table, minus the outer pipes. */
const tableCells = (line) =>
  line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());

/* The `| :--- | ---: |` line under a header row. Also carries alignment. */
const ALIGN_ROW = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function alignmentsFrom(line) {
  return tableCells(line).map(spec => {
    const left = spec.startsWith(":");
    const right = spec.endsWith(":");

    if (left && right) return "center";
    if (right) return "right";
    return left ? "left" : "";
  });
}

/* [claude] A column of numbers is right-aligned and tabular whatever the
   markdown said.
 *
 * Agents emit `| :--- |` for every column out of habit, so honouring the
 * spec alone left figures ragged-left in proportional digits — which is
 * the single thing that makes a table of numbers look unconsidered. A
 * column is treated as numeric when every populated cell in it is one. */
const NUMERIC = /^[-+]?[\d,]+(\.\d+)?\s*%?$|^[-+]?\d+(\.\d+)?\s*(sqm|m²|EGP|USD)?$/i;

function numericColumns(rows) {
  const width = Math.max(...rows.map(r => r.length));

  return Array.from({ length: width }, (_, i) => {
    // [claude] The first column is the row's label, never a quantity —
    // even when it is made of digits.
    //
    // "Franchise" holds 9, 5 and 12, which are identifiers rather than
    // amounts. Right-aligning them puts the thing you scan down the
    // column for against the wrong edge, away from the header it belongs
    // under, and lines it up with figures it should not be compared to.
    // A table's stub column is left-aligned; that convention exists
    // because it is what makes the rows readable.
    if (i === 0) return false;

    const cells = rows.map(r => (r[i] || "").trim()).filter(Boolean);

    return cells.length > 0 && cells.every(c => NUMERIC.test(c));
  });
}

function renderTable(header, alignments, body) {
  const numeric = numericColumns(body);

  const align = (i) =>
    alignments[i] && alignments[i] !== "left"
      ? alignments[i]
      : (numeric[i] ? "right" : "left");

  const cell = (tag, text, i) =>
    `<${tag} class="${numeric[i] ? "n" : ""}" style="text-align:${align(i)}">`
    + mdInline(text) + `</${tag}>`;

  const head = "<tr>" + header.map((c, i) => cell("th", c, i)).join("") + "</tr>";
  const rows = body
    .map(r => "<tr>" + r.map((c, i) => cell("td", c, i)).join("") + "</tr>")
    .join("");

  // Its own scroll box: a wide table must not make the page scroll
  // sideways, which breaks every other column on the screen to show one.
  return `<div class="scroll-x"><table class="md">${head}${rows}</table></div>`;
}

/*
 * The agent writes light markdown. Rendering it here rather than showing
 * raw asterisks — and deliberately tiny and escape-first, because the text
 * contains CRM data and, in a reconciliation, content out of a file
 * somebody uploaded. Nothing is ever trusted as HTML.
 *
 * [claude] Tables were the gap worth closing. Asked to compare franchises
 * the agent replies with a real markdown table, and the previous renderer
 * matched neither its bullet nor its heading pattern — so every row became
 * a paragraph of raw pipe characters. That is the most visible thing in an
 * answer and it read as broken.
 *
 * Also handled now: ordered lists, and a bold line on its own, which is
 * what the agent writes where it means a subheading.
 */
function renderMarkdown(text) {
  const lines = mdEscape(text).split("\n");
  const out = [];

  let list = null;   // "ul" | "ol" | null

  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = null; }
  };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];

    // ---- table: a header row followed by an alignment row ----
    if (
      raw.includes("|") &&
      i + 1 < lines.length &&
      ALIGN_ROW.test(lines[i + 1]) &&
      lines[i + 1].includes("-")
    ) {
      closeList();

      const header = tableCells(raw);
      const alignments = alignmentsFrom(lines[i + 1]);
      const body = [];

      let j = i + 2;

      while (j < lines.length && lines[j].includes("|") && lines[j].trim()) {
        body.push(tableCells(lines[j]));
        j++;
      }

      out.push(renderTable(header, alignments, body));
      i = j - 1;
      continue;
    }

    // ---- lists ----
    const bullet = raw.match(/^\s*[-*•]\s+(.*)$/);

    if (bullet) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push("<li>" + mdInline(bullet[1]) + "</li>");
      continue;
    }

    const numbered = raw.match(/^\s*\d+[.)]\s+(.*)$/);

    if (numbered) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push("<li>" + mdInline(numbered[1]) + "</li>");
      continue;
    }

    closeList();

    if (!raw.trim()) continue;

    // ---- headings ----
    const hash = raw.match(/^#{1,6}\s+(.*)$/);

    if (hash) { out.push("<h4>" + mdInline(hash[1]) + "</h4>"); continue; }

    // A line that is entirely bold is a subheading, not a paragraph that
    // happens to shout. This is what the agent writes above a breakdown.
    const boldOnly = raw.trim().match(/^\*\*([^*]+)\*\*:?$/);

    if (boldOnly) {
      out.push("<h4>" + mdInline(boldOnly[1]) + "</h4>");
      continue;
    }

    out.push("<p>" + mdInline(raw) + "</p>");
  }

  closeList();

  return out.join("");
}

/* Highlight SQL keywords — gold, matching the brand's accent role. */
const KEYWORDS = /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|LIMIT|JOIN|LEFT|INNER|ON|AND|OR|NOT|NULL|IS|AS|COUNT|SUM|AVG|MIN|MAX|FILTER|CAST|WITH|HAVING|DISTINCT|CASE|WHEN|THEN|ELSE|END|BETWEEN|IN|INTERVAL|CURRENT_DATE|DESC|ASC|EXISTS|NUMERIC|TRUE|FALSE)\b/gi;
function sqlHtml(sql) {
  const esc = sql.replace(/[&<>]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;" }[c]));
  return esc.replace(KEYWORDS, m => `<span class="kw">${m}</span>`);
}

/* ==================================================================
   Stopping a turn

   [claude] There was no way to stop one. A question that routed badly, or
   simply a mistyped one, had to be watched to completion — and a
   reconciliation is 28 steps, which is a long time to sit in front of an
   answer you already know is wrong.

   Abort is honest about what it does and does not do: it stops *this
   client reading the stream*. The server finishes the turn it started and
   records it, so the conversation history stays true — the partial text
   is kept on screen and labelled rather than deleted, because a turn that
   ran and cost money should not vanish just because nobody watched it
   finish.
   ================================================================== */
let currentAbort = null;

function stopTurn() {
  if (currentAbort) { currentAbort.abort(); currentAbort = null; }
}

function setSending(on) {
  busy = on;
  const b = $("send");
  b.textContent = on ? "Stop" : "Send";
  b.classList.toggle("stopping", on);
  b.setAttribute("aria-label", on ? "Stop generating" : "Send message");
  $("log").setAttribute("aria-busy", String(on));
}

/* ---------------- a turn, streamed ---------------- */
async function send(preset) {
  const text = (preset ?? $("input").value).trim();
  if (!text || busy) return;
  if (!hasToken()) { flashToken(); return; }

  setSending(true);
  announce("Working on it.");
  $("input").value = ""; $("input").style.height = "auto";
  // A new question is a return to the bottom — you asked it, you want to
  // see it answered.
  scrollDown(true);
  bubble("user", text);

  const el = bubble("agent", "");
  const body = el.querySelector(".bubble");

  // [claude] One calm line. No tool names, no route names, no jargon —
  // that is how the machine works rather than what was asked. It exists
  // only so the couple of seconds before text appears do not read as a
  // hang; everything technical waits for `attachDetails` afterwards.
  const stage = document.createElement("div");
  stage.className = "stage";
  stage.innerHTML = '<span class="orb"></span>'
                  + '<span class="stage-text">Thinking</span>'
                  + '<span class="elapsed">0.0s</span>';
  el.insertBefore(stage, body);

  const t0 = performance.now();
  const ticking = setInterval(() => {
    stage.querySelector(".elapsed").textContent =
      ((performance.now() - t0) / 1000).toFixed(1) + "s";
  }, 100);
  const setStage = (t) => { stage.querySelector(".stage-text").textContent = t; };
  const endStage = () => { clearInterval(ticking); stage.classList.add("gone"); };

  const typer = new Typer(body);
  let route = null, plan = [], tools = [], started = false, finished = null;

  // The question that produced this answer, so "Retry" has something to
  // retry without re-reading the DOM for it.
  el.dataset.question = text;

  const control = new AbortController();
  currentAbort = control;

  try {
    const res = await fetch("/v1/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...auth() },
      body: JSON.stringify({ message: text, ...(threadId ? { thread_id: threadId } : {}) }),
      signal: control.signal,
    });

    if (!res.ok) {
      let msg = res.status + " " + res.statusText;
      try { msg = (await res.json()).error.message; } catch {}
      endStage(); body.className = "bubble err"; body.textContent = msg;
      return;
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "", event = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();

      for (const line of lines) {
        if (line.startsWith("event:")) { event = line.slice(6).trim(); continue; }
        if (!line.startsWith("data:")) continue;
        let d; try { d = JSON.parse(line.slice(5).trim()); } catch { continue; }

        // route, plan and tool are recorded for the details panel and
        // deliberately change nothing on screen while the turn runs.
        if (event === "start") threadId = d.thread_id;
        else if (event === "plan") plan = d.specialists || [];
        else if (event === "route") route = d.route;
        else if (event === "tool") tools.push(d.name);

        else if (event === "token") {
          if (!started) { started = true; setStage("Writing"); }
          typer.push(d.text);
        }

        else if (event === "final") {
          finished = d;
          typer.end(() => {
            endStage();
            const answer = d.answer || typer.shown || "(no answer)";
            body.innerHTML = renderMarkdown(answer);
            announce("Answer ready.");
            attachDetails(el, {
              specialists: (d.specialists && d.specialists.length)
                ? d.specialists
                : (plan.length ? plan : (d.route || route ? [d.route || route] : [])),
              tools: d.tools_used && d.tools_used.length ? d.tools_used : tools,
              provenance: d.provenance,
              charts: d.charts,
              answer,
            });
            loadThreads();
          });
        }

        else if (event === "error") {
          typer.flush(); endStage();
          announce("That failed.");
          body.className = "bubble err";
          body.textContent = d.message + (d.request_id ? "  ·  " + d.request_id : "");
        }
      }
    }

    if (!finished) typer.end(() => endStage());

  } catch (e) {
    typer.flush(); endStage();

    if (e.name === "AbortError") {
      // Kept, not discarded. Whatever arrived is real output from a turn
      // the server ran; deleting it would misrepresent what happened.
      announce("Stopped.");

      // [claude] The partial gets the same rendering the finished answer
      // would have. Only the `final` event used to call renderMarkdown, so
      // a stopped turn left its text as raw source — asterisks and hashes
      // on screen, which reads as the agent having malfunctioned rather
      // than as an answer that was interrupted.
      if (typer.shown) body.innerHTML = renderMarkdown(typer.shown);
      const note = document.createElement("div");
      note.className = "after";
      note.innerHTML = '<span style="font-size:10px;letter-spacing:.16em;'
        + 'text-transform:uppercase;color:var(--ink-dim)">Stopped · '
        + 'the agent finished this turn on the server</span>';
      el.appendChild(note);
      enter(note);
      addActions(el, { answer: typer.shown, question: text });
    } else {
      announce("That failed.");
      body.className = "bubble err";
      body.textContent = "Could not reach the agent — is it running?  (" + e.message + ")";
    }
  } finally {
    currentAbort = null;
    setSending(false);
    if (!paletteOpen && !drawerOpen) $("input").focus();
  }
}

function flashToken() {
  // Open the drawer rather than flashing a field the user cannot see —
  // the token lives in the footer now, collapsed by default.
  toggleToken(true);
  toast("Add an access token to begin", true);
}

/* ---------------- threads ---------------- */
const SKELETON = '<div class="skel"><i></i><i></i><i></i></div>';

async function loadThreads() {
  if (!hasToken()) return;
  // [claude] Reserved space rather than an empty panel. A cold load used
  // to render nothing until the fetch resolved, which is indistinguishable
  // from "you have no conversations" — and then the rows appeared and the
  // panel jumped.
  if (!$("threads").children.length) $("threads").innerHTML = SKELETON;
  try {
    const r = await fetch("/v1/threads?limit=50", { headers: auth() });
    if (!r.ok) { $("threads").innerHTML = ""; return; }
    const rows = (await r.json()).conversations;
    threadsCache = rows;
    $("threads").innerHTML = "";
    rows.forEach(c => {
      const d = document.createElement("div");
      d.className = "thread" + (c.thread_id === threadId ? " active" : "");
      d.innerHTML = `<span class="t"></span><span class="n">${c.turn_count}</span><span class="kill">×</span>`;
      d.querySelector(".t").textContent = c.title || c.thread_id;
      const open = () => { openThread(c.thread_id); if (drawerOpen) setDrawer(false); };
      const title = d.querySelector(".t");
      title.onclick = open;
      title.tabIndex = 0;
      title.setAttribute("role", "button");
      title.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      };

      const kill = d.querySelector(".kill");
      kill.tabIndex = 0;
      kill.setAttribute("role", "button");
      kill.setAttribute("aria-label", "Delete this conversation");
      const remove = async (e) => {
        e.stopPropagation();
        // [claude] Animated out before the refetch, so the row leaving is
        // connected to the click that removed it. Exit is faster than
        // entry — a thing on its way out should not be waited for.
        const gone = anim(d, { opacity: [1, 0], transform: ["translateX(0px)", "translateX(-14px)"] },
                          { duration: 0.16, ease: "linear" });
        await new Promise(done => afterExit(gone, 160, done));
        await fetch("/v1/threads/" + encodeURIComponent(c.thread_id), { method: "DELETE", headers: auth() });
        if (c.thread_id === threadId) newChat();
        loadThreads();
      };
      kill.onclick = remove;
      kill.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); remove(e); }
      };
      $("threads").appendChild(d);
    });
    enterList($("threads").children);
  } catch { $("threads").innerHTML = ""; }
}
async function openThread(id) {
  const r = await fetch("/v1/threads/" + encodeURIComponent(id), { headers: auth() });
  if (!r.ok) return;
  const c = await r.json();
  threadId = id;
  $("log").querySelector(".wrap").innerHTML = "";
  c.messages.forEach(m => {
    const el = bubble(m.role === "user" ? "user" : "agent", m.content);
    // [claude] The SQL is stored per turn now, so a reopened conversation
    // is as checkable as a live one.
    if (m.role !== "user") {
      attachDetails(el, {
        specialists: [], tools: [],
        provenance: m.provenance, answer: m.content,
      });
    }
  });
  loadThreads();
}
function newChat() {
  threadId = null;
  $("log").querySelector(".wrap").innerHTML = "";
  renderEmpty();
  loadThreads();
}
function renderEmpty() {
  const w = $("log").querySelector(".wrap");
  w.innerHTML = `<div class="empty" id="empty">
    <svg class="mark" width="34" height="100" viewBox="0 0 34 100" fill="none" stroke="#CBA46E" stroke-width="1">
      <path d="M17 1 C 24 26, 30 40, 33 50 C 30 60, 24 74, 17 99 C 10 74, 4 60, 1 50 C 4 40, 10 26, 17 1 Z"/>
      <path d="M17 1 L17 99"/><path d="M1 50 L33 50"/>
      <path d="M17 14 C 22 31, 26 42, 28 50 C 26 58, 22 69, 17 86 C 12 69, 8 58, 6 50 C 8 42, 12 31, 17 14 Z" opacity=".55"/>
    </svg>
    <div class="big">Live Inspired</div>
    <div class="lead">Live Connected · Live Inspired</div>
    <div class="suggest" id="suggest"></div></div>`;
  mountSuggestions();
}
function mountSuggestions() {
  const s = $("suggest");
  if (!s) return;
  s.innerHTML = "";
  SUGGESTIONS.forEach(q => {
    const b = document.createElement("button");
    b.textContent = q;
    b.onclick = () => send(q);
    s.appendChild(b);
  });
}

/* ---------------- files ---------------- */
async function loadFiles() {
  if (!hasToken()) return;
  try {
    const r = await fetch("/v1/workspace/files", { headers: auth() });
    if (!r.ok) return;
    const files = (await r.json()).files;
    $("files").innerHTML = "";
    $("fileCount").textContent = files.length ? String(files.length) : "";
    if (files.length) $("fileCount").setAttribute("aria-label", files.length + " files");
    files.forEach(f => {
      const shape = f.sheets?.length ? f.sheets[0].row_count + " rows"
                  : (f.page_count ? f.page_count + " pp" : "");
      const d = document.createElement("div");
      d.className = "file-row";
      d.innerHTML = `<span class="fn"></span><span class="meta">${shape}</span><span class="kill">×</span>`;
      d.querySelector(".fn").textContent = f.filename;
      d.querySelector(".kill").onclick = async () => {
        await fetch("/v1/workspace/files/" + f.file_id, { method: "DELETE", headers: auth() });
        loadFiles();
      };
      $("files").appendChild(d);
    });
    enterList($("files").children, 0.03);
  } catch {}
}
async function upload(file) {
  if (!file) return;
  if (!hasToken()) { flashToken(); return; }
  const drop = $("drop");
  const original = drop.innerHTML;
  drop.innerHTML = '<span class="spin">◇</span><br>uploading…';
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/v1/workspace/files", { method: "POST", headers: auth(), body: fd });
    const d = await r.json();
    if (!r.ok) toast(d.error?.message || "Upload failed", true);
    else toast(d.warnings?.length ? d.warnings.join(" · ") : "File uploaded.");
  } catch (e) {
    toast("Upload failed: " + e.message, true);
  } finally {
    drop.innerHTML = original; loadFiles();
  }
}
$("drop").onclick = () => $("file").click();
$("file").onchange = (e) => { upload(e.target.files[0]); e.target.value = ""; };
["dragenter", "dragover"].forEach(ev =>
  $("drop").addEventListener(ev, e => { e.preventDefault(); $("drop").classList.add("over"); }));
["dragleave", "drop"].forEach(ev =>
  $("drop").addEventListener(ev, e => { e.preventDefault(); $("drop").classList.remove("over"); }));
$("drop").addEventListener("drop", e => upload(e.dataTransfer.files[0]));

/* ---------------- health ---------------- */
async function health() {
  try {
    const r = await fetch("/health/ready");
    const d = await r.json();
    $("health").className = "dot " + (d.ready ? "ok" : "bad");
    $("healthText").textContent = d.ready ? "ready" : "degraded";
    $("status").title = Object.entries(d)
      .filter(([, v]) => v && typeof v === "object")
      .map(([k, v]) => `${k}: ${v.ok ? "ok" : "FAIL"}${v.detail ? " — " + v.detail : ""}`)
      .join("\n");
  } catch {
    $("health").className = "dot bad";
    $("healthText").textContent = "offline";
  }
}

function refreshAll() { health(); loadThreads(); loadFiles(); }

$("send").onclick = () => (busy ? stopTurn() : send());
$("new").onclick = newChat;
$("refresh").onclick = refreshAll;
$("input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("input").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 170) + "px";
});

/* [claude] The full prompt is a sentence, and at 375px it wrapped out of a
   one-row textarea and was clipped mid-word — which reads as a broken
   field rather than a hint. CSS cannot shorten placeholder text, so it is
   swapped here. */
const PLACEHOLDER_FULL = "Ask about deals, leads, or a file you uploaded…";
const PLACEHOLDER_SHORT = "Ask about deals or leads…";
const narrow = window.matchMedia("(max-width: 560px)");

function fitPlaceholder() {
  $("input").placeholder = narrow.matches ? PLACEHOLDER_SHORT : PLACEHOLDER_FULL;
}
narrow.addEventListener("change", fitPlaceholder);
fitPlaceholder();


/* ==================================================================
   Command palette

   [claude] Actions in this console each had exactly one home on screen,
   and four of them lived in the sidebar — which, below 900px, is behind a
   drawer. That made "start a new conversation" a three-gesture operation
   on a phone and an unreachable one for anybody driving by keyboard.

   The palette is not a shortcut for power users so much as the second
   route to everything, which is what the drawer made necessary. It also
   searches conversations, because "the pipeline question from earlier" is
   a far more natural way to find a thread than scanning a list of
   truncated titles.
   ================================================================== */
let threadsCache = [];
let paletteOpen = false, paletteIndex = 0, paletteRows = [], paletteReturn = null;

const isMac = /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);
const CMD = isMac ? "⌘" : "Ctrl";

/* Subsequence match, so "nwc" finds "New conversation" — the way every
   palette worth using behaves. Scored so that a prefix beats a scatter. */
function fuzzy(needle, hay) {
  const n = needle.toLowerCase().trim(), h = hay.toLowerCase();
  if (!n) return 0;
  if (h.startsWith(n)) return 1000;
  const at = h.indexOf(n);
  if (at >= 0) return 700 - at;
  let i = 0, score = 0, run = 0;
  for (const ch of h) {
    if (i < n.length && ch === n[i]) { i++; run++; score += 8 + run * 2; }
    else run = 0;
  }
  return i === n.length ? score : -1;
}

function paletteActions() {
  const acts = [
    { group: "Actions", label: "New conversation", key: CMD + " ⇧ O",
      run: () => newChat() },
    { group: "Actions", label: "Upload a file", key: "",
      run: () => $("file").click() },
    { group: "Actions", label: "Refresh conversations, files and status", key: "",
      run: () => refreshAll() },
    { group: "Actions", label: "Focus the composer", key: "/",
      run: () => $("input").focus() },
    { group: "Actions", label: hasToken() ? "Change access token" : "Add an access token",
      key: "", run: () => { setDrawer(isDrawer()); toggleToken(true); } },
  ];

  const last = [...document.querySelectorAll(".msg.agent")].pop();
  if (last) {
    acts.push({
      group: "Actions", label: "Copy the last answer", key: "",
      run: async () => {
        const text = last.dataset.answer || last.querySelector(".bubble")?.textContent || "";
        try { await navigator.clipboard.writeText(text); toast("Answer copied."); }
        catch { toast("Could not copy to the clipboard", true); }
      },
    });
  }

  threadsCache.slice(0, 24).forEach(c => acts.push({
    group: "Conversations",
    label: c.title || c.thread_id,
    key: c.turn_count + (c.turn_count === 1 ? " turn" : " turns"),
    run: () => openThread(c.thread_id),
  }));

  SUGGESTIONS.forEach(q => acts.push({
    group: "Ask", label: q, key: "",
    run: () => send(q),
  }));

  return acts;
}

function renderPalette() {
  const q = $("paletteInput").value;
  const list = $("paletteList");

  paletteRows = paletteActions()
    .map(a => ({ ...a, score: fuzzy(q, a.label) }))
    .filter(a => a.score >= 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 40);

  if (paletteIndex >= paletteRows.length) paletteIndex = 0;
  list.innerHTML = "";

  if (!paletteRows.length) {
    const none = document.createElement("div");
    none.className = "palette-empty";
    none.textContent = "Nothing matches that.";
    list.appendChild(none);
    return;
  }

  let group = null;
  paletteRows.forEach((row, i) => {
    if (row.group !== group) {
      group = row.group;
      const h = document.createElement("div");
      h.className = "palette-group";
      h.textContent = group;
      list.appendChild(h);
    }
    const b = document.createElement("button");
    b.className = "palette-item";
    b.type = "button";
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", String(i === paletteIndex));
    b.innerHTML = '<span class="pi-mark" aria-hidden="true">◇</span>'
                + '<span class="pi-text"></span><span class="pi-key"></span>';
    b.querySelector(".pi-text").textContent = row.label;
    b.querySelector(".pi-key").textContent = row.key || "";
    b.onclick = () => runPalette(i);
    list.appendChild(b);
  });

  enterList(list.querySelectorAll(".palette-item"), 0.014);
}

function movePalette(delta) {
  if (!paletteRows.length) return;
  paletteIndex = (paletteIndex + delta + paletteRows.length) % paletteRows.length;
  const items = $("paletteList").querySelectorAll(".palette-item");
  items.forEach((el, i) => el.setAttribute("aria-selected", String(i === paletteIndex)));
  items[paletteIndex]?.scrollIntoView({ block: "nearest" });
}

function runPalette(i) {
  const row = paletteRows[i ?? paletteIndex];
  if (!row) return;
  setPalette(false);
  // After the overlay is gone, so focus lands where the action put it.
  setTimeout(() => row.run(), 0);
}

function setPalette(open) {
  const wrap = $("paletteWrap");
  paletteOpen = open;

  if (open) {
    paletteReturn = document.activeElement;
    wrap.hidden = false;
    wrap.classList.add("open");
    $("paletteInput").value = "";
    paletteIndex = 0;
    renderPalette();
    anim(wrap, { opacity: [0, 1] }, { duration: 0.14, ease: "linear" });
    anim(wrap.querySelector(".palette"),
         { opacity: [0, 1], transform: ["translateY(-10px) scale(.985)", "translateY(0px) scale(1)"] },
         SPRING_SOFT);
    $("paletteInput").focus();
  } else {
    wrap.classList.remove("open");
    const out = anim(wrap, { opacity: [1, 0] }, { duration: 0.12, ease: "linear" });
    afterExit(out, 120, () => { if (!paletteOpen) wrap.hidden = true; });
    if (paletteReturn && document.contains(paletteReturn)) {
      paletteReturn.focus({ preventScroll: true });
    }
    paletteReturn = null;
  }
}

$("paletteInput").addEventListener("input", () => { paletteIndex = 0; renderPalette(); });
$("paletteWrap").addEventListener("mousedown", (e) => {
  if (e.target === $("paletteWrap")) setPalette(false);
});
$("paletteWrap").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
  else if (e.key === "Enter") { e.preventDefault(); runPalette(); }
  else if (e.key === "Escape") { e.preventDefault(); setPalette(false); }
  else trapTab(e, $("paletteWrap"));
});

/* ==================================================================
   Keyboard

   One global listener rather than several, so the precedence between
   Escape's three meanings is written down in one place instead of
   emerging from listener registration order.
   ================================================================== */
document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || "");

  // Palette: the one shortcut that works from anywhere, including a field.
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    setPalette(!paletteOpen);
    return;
  }

  // New conversation.
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") {
    e.preventDefault();
    newChat();
    return;
  }

  // Send from anywhere in the composer, for people who expect ⌘⏎.
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && typing) {
    e.preventDefault();
    send();
    return;
  }

  if (e.key === "Escape") {
    // Most-nested first: palette, then drawer, then a turn in flight.
    if (paletteOpen) { setPalette(false); return; }
    if (drawerOpen) { setDrawer(false); return; }
    if (busy) { e.preventDefault(); stopTurn(); return; }
    return;
  }

  // `/` focuses the composer, but only when it is not a character someone
  // is trying to type.
  if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    $("input").focus();
  }
});

$("sidebar").addEventListener("keydown", (e) => {
  if (drawerOpen) trapTab(e, $("sidebar"));
});

mountSuggestions();
renderIdentity();
refreshAll();
setInterval(health, 15000);

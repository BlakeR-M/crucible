<!-- Design exploration. Written 2026-08-16; status honest as of 2026-08-20. -->

> **What this is.** A design direction that was explored in full and then set
> aside: a dark instrument theme, with colour tokens at verified contrast
> ratios, a type scale, layout, glyphs and motion rules. The interface that
> shipped went a different way, the light paper certificate in
> `web/static/app.css`, after three rebuilds landed on conversation-first over
> instrument-first. This document is kept as the record of the road that was
> considered, and as a bench of decisions to draw on if the instrument look is
> ever wanted.
>
> Two decisions here survived into the shipped design in spirit. Panels sit on
> hairlines with no radius and no shadow, because rounded cards floating on a
> background is the most reliable tell of a template. And the refuted pile
> keeps permanent screen space, because a system that shows how much of its
> own output it destroyed is making its argument structurally instead of in
> copy.

---

# Crucible: Visual Design Direction

**Positioning:** an instrument, not a dashboard. The visual language borrows from assay reports, flight test telemetry, and evidence chains rather than from developer-tool marketing. Every choice below is calibrated to one reaction: *the person who built this has done this before.*

**The metaphor, applied literally:** things go in and most are destroyed; what survives carries its scars. That means the destroyed pile gets permanent screen real estate. A product that displays how much of its own output it threw away is making the trust argument structurally instead of in copy. This is the single most important idea in the design.

---

## 1. Colour

Dark theme is the only theme. No light mode, no toggle. Committing to one look is itself a signal.

Surfaces are a cool graphite with a slight blue cast, so it reads as instrumentation rather than terminal. Heat is rationed: the ember accent occupies under 5% of coloured pixels on any screen.

### 1.1 Tokens

```css
:root {
  /* Ground and surfaces */
  --void:            #07090B;  /* page ground, outside all panels */
  --surface-base:    #0C0F12;  /* primary working surface */
  --surface-raised:  #12161A;  /* panels, lane bodies */
  --surface-inset:   #171C21;  /* code wells, strata, input fields */
  --surface-overlay: #1D2329;  /* menus, dialogs, tooltips */
  --surface-select:  #24303A;  /* text selection, row selection */

  /* Lines. Exactly three values in the entire application. */
  --line-hairline:   #1E252B;  /* time gridlines, internal dividers */
  --line:            #2A3238;  /* panel edges, table rules */
  --line-active:     #64707A;  /* meaningful boundaries, 3.13:1+ everywhere */

  /* Text */
  --text-primary:    #E6EAEE;
  --text-secondary:  #A7B1BA;
  --text-tertiary:   #848F99;  /* smallest permitted body value */
  --text-code:       #C9D2DA;

  /* Semantic status. One hue each. Five total. No sixth. */
  --st-pending:      #8C97A3;  /* inert slate. Deliberately hueless. */
  --st-running:      #E8933A;  /* ember. Heat is being applied. */
  --st-survived:     #35C48F;
  --st-refuted:      #F2555A;
  --st-refused:      #9E86FF;  /* policy refusal. Violet reads as governance. */

  /* Status tints, for 1px-inset backfills only. Never large fills. */
  --st-pending-dim:  #1A1E23;
  --st-running-dim:  #2A1C0C;
  --st-survived-dim: #0E2A20;
  --st-refuted-dim:  #2E1214;
  --st-refused-dim:  #1E1833;

  /* Interaction */
  --focus:           #9CC6E8;  /* focus ring only. Never decorative. */
  --steel:           #7FB0D9;  /* links, in-text references, ledger hashes */
}
```

### 1.2 Verified contrast (WCAG 2.1 relative luminance, computed)

Body text and all status colours against every surface they are permitted on:

| Foreground | on `--surface-base` | on `--surface-raised` | on `--surface-inset` | on `--surface-overlay` |
|---|---|---|---|---|
| `--text-primary` #E6EAEE | 15.89 | 15.03 | 14.18 | 13.11 |
| `--text-secondary` #A7B1BA | 8.82 | 8.34 | 7.87 | 7.28 |
| `--text-tertiary` #848F99 | 5.83 | 5.51 | 5.20 | 4.81 |
| `--st-pending` #8C97A3 | 6.47 | 6.12 | 5.77 | 5.34 |
| `--st-running` #E8933A | 7.93 | 7.50 | 7.08 | 6.54 |
| `--st-survived` #35C48F | 8.64 | 8.17 | 7.71 | 7.13 |
| `--st-refuted` #F2555A | 5.69 | 5.39 | 5.08 | 4.70 |
| `--st-refused` #9E86FF | 6.69 | 6.32 | 5.97 | 5.52 |
| `--steel` #7FB0D9 | 8.34 | 7.89 | 7.44 | 6.88 |
| `--text-code` #C9D2DA | 12.9 | 12.2 | 11.20 | 10.35 |

Every value clears **AA 4.5:1 for normal text** on every surface. The lowest is `--st-refuted` on `--surface-overlay` at 4.70:1.

Status colour on its own dim tint (used for inline chips):

| Pair | Ratio |
|---|---|
| `--st-running` on `--st-running-dim` | 6.82 |
| `--st-survived` on `--st-survived-dim` | 6.89 |
| `--st-refuted` on `--st-refuted-dim` | 5.13 |
| `--st-refused` on `--st-refused-dim` | 5.92 |
| `--st-pending` on `--st-pending-dim` | 5.64 |

Non-text contrast (**AA 3:1 for UI component boundaries and graphical objects**):

| Token | base | raised | inset | overlay |
|---|---|---|---|---|
| `--line-active` #64707A | 3.79 | 3.58 | 3.38 | 3.13 |
| `--focus` #9CC6E8 | 10.67 | 10.09 | 9.54 | 8.82 |

`--line` (#2A3238, 1.48:1) and `--line-hairline` (#1E252B, 1.24:1) are decorative separation only. Any boundary that carries meaning, such as a selected lane, an active field, or a status edge, uses `--line-active` or a status colour.

Dark ink on saturated fills, for the one solid button and for status chips: ink `#07090B` on `--st-running` = 8.23:1; on `--st-survived` = 8.97:1; on `--st-refuted` = 5.91:1; on `--st-refused` = 6.94:1.

### 1.3 Colour is never the only channel

Every status carries three redundant signals: a glyph, a text label, and colour. This survives deuteranopia, protanopia, projector washout, and a printed PDF of a screenshot, which is how half this audience will see it.

**The glyph set: five marks, all derived from one 7px square on a 12px grid, 1.25px stroke, butt caps, no rounded corners.**

| State | Mark |
|---|---|
| Pending | hollow square, stroke at 55% opacity |
| Running | square with the lower 3px filled, the crucible filling |
| Survived | filled square with a 1px keyline offset 2px outward, a sealed look |
| Refuted | hollow square with one 1.25px slash at 45 degrees, overshooting 1.5px both ends |
| Refused | hollow square with a horizontal 1.25px bar across the midline, extending 2px past both edges, a barrier |

Drawing these yourself rather than pulling an icon set is a five-glyph job and is one of the clearest expensive-versus-templated tells on the page.

---

## 2. Typography

No network fonts. Strict CSP, fully self-contained. The stacks below resolve to good faces on macOS, Windows 10/11, and Linux desktops, which covers the whole audience.

```css
:root {
  --f-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont,
            "Segoe UI Variable Text", "Segoe UI", system-ui,
            "Helvetica Neue", Arial, sans-serif;

  --f-serif: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino,
             "Book Antiqua", Georgia, "Times New Roman", serif;

  --f-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco,
            "Cascadia Mono", "Cascadia Code", Consolas,
            "DejaVu Sans Mono", "Liberation Mono", monospace;
}
```

**Serif is rationed and rule-bound.** It appears in exactly three places: the wordmark, the pre-run prompt line, and the title block of the final report. Never in the interface chrome, never on a number. Georgia and Palatino ship old-style figures on Windows, which look wrong in data, so the rule is also a defect guard.

**Mono carries all machine truth.** Timestamps, hashes, agent identifiers, tool names, durations, file paths, line numbers, counts. If a value came from the system rather than a person, it is mono. That single rule does more for the serious-engineering read than any colour choice.

### 2.1 Scale

| Role | Family | Size / line-height | Weight | Tracking |
|---|---|---|---|---|
| Display (wordmark, report title) | serif | 44 / 48px | 400 | -0.01em |
| Prompt line (pre-run) | serif | 28 / 38px | 400 | -0.005em |
| Section heading | sans | 20 / 28px | 600 | -0.006em |
| Subheading | sans | 15 / 22px | 600 | 0 |
| Body | sans | 14 / 21px | 400 | 0 |
| Body small | sans | 13 / 19px | 400 | 0 |
| Label (small caps) | sans | 11 / 12px | 600 | 0.09em, uppercase |
| Micro label | sans | 10 / 12px | 500 | 0.10em, uppercase |
| Data mono | mono | 12 / 16px | 400 | 0 |
| Code mono | mono | 12.5 / 19px | 400 | 0 |
| Micro mono (strata, gutters) | mono | 10.5 / 12px | 400 | 0.01em |

Weights used across the whole product: **400, 500, 600.** No 700, no 300. Two weights inside any single panel.

### 2.2 Numeric discipline

```css
.num, .mono, table, .lane-meta {
  font-variant-numeric: tabular-nums slashed-zero;
  font-feature-settings: "tnum" 1, "zero" 1, "ss01" 1;
}
```

Tabular figures everywhere, without exception. A counter whose digits shift width while it ticks is the single most common tell of a templated build.

Timestamps render as `14:22:07.418` at millisecond precision. Durations render as `1.284s` or `312ms`, never "about a second". Hashes render in 4-character groups: `9f3c a17b 84de 22c0`.

---

## 3. Layout

### 3.1 The two-state page

The pre-run and mid-run states are different pages by design. That transition is the demonstration.

**Idle.** Near-empty. Ground is `--void`. Centred column 720px wide sitting at 38% viewport height. The serif wordmark `CRUCIBLE` at 16px, uppercase, `letter-spacing: 0.22em`, in `--text-tertiary`. Below it, one serif line at 28px in `--text-secondary`: *"Submit a review task. Findings that cannot survive attack will not be reported."* Below that, one input. Below the input, a single line of mono at 11px naming the active ruleset and its version. Nothing else. No feature grid, no logos, no scroll.

The input is a 3-row textarea, `--surface-inset`, 1px `--line`, 2px radius, 13px mono at 12.5px. On focus the border goes `--line-active` and a 2px `--focus` outline appears at 2px offset. The submit control sits below-right: solid `--st-running` fill, ink `#07090B`, 11px small-caps 600, label **IGNITE**, 32px tall, 2px radius. It is the only saturated block on the idle page.

**Running.** The idle column does not fade out. It slides up and compresses into the masthead and the left brief over 320ms, and the three working panels wipe in from the ledger spine outward. The user watches the empty page become an instrument.

### 3.2 Running layout, 1440 x 900 reference

```
┌─┬────────────────────────────────────────────────────────────────────────────┐
│L│ CRUCIBLE   run 9f3c·a17b   T+ 00:04:17   PHASE: VERIFY    07 / 63 SURVIVED │ 48px masthead
│E├────────────────────────────────────────────────────────────────────────────┤
│D│ ══════════════════════════ heat bar, 2px ═════════════════════════════════ │
│G├──────────────┬───────────────────────────────────────┬─────────────────────┤
│E│ THE BRIEF    │  THE LATTICE                          │  THE ASSAY          │
│R│              │                                       │                     │
│ │ task text    │  ┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐│  SURVIVED       07  │
│S│ target ref   │  │core││core││core││core││core││core││  ─────────────────  │
│P│ ruleset id   │  │samp││samp││samp││samp││samp││samp││  ▣ finding card     │
│I│              │  │ le ││ le ││ le ││ le ││ le ││ le ││  ▣ finding card     │
│N│ ── counters  │  │    ││    ││    ││    ││    ││    ││  ▣ finding card     │
│E│ agents    12 │  └────┘└────┘└────┘└────┘└────┘└────┘│                     │
│ │ calls    847 │                                       │  ─────────────────  │
│4│ refusals   3 │  ─────────────────────────────────── │  REFUTED        54  │
│p│              │  THE CRUCIBLE                         │  ⊘ one-line entry   │
│x│ ── policy    │  ┌──────────────────────────────────┐│  ⊘ one-line entry   │
│ │ 14 rules     │  │ finding under attack, scar marks ││  ⊘ one-line entry   │
│ │ view →       │  └──────────────────────────────────┘│  ⊘ ...              │
│ │              │                                       │                     │
│ │              │                                       │  REFUSED         2  │
├─┴──────────────┴───────────────────────────────────────┴─────────────────────┤
│ connected · gpt-class ×8, claude-class ×4 · ledger root 9f3c a17b 84de 22c0  │ 28px footer
└──────────────────────────────────────────────────────────────────────────────┘
```

**Column widths:** ledger spine 4px fixed. Brief 288px fixed. Lattice fluid, 640px minimum. Assay 400px fixed.

**Panels are full-bleed and separated by 1px `--line` rules. They sit flat, with no radius and no shadow.** Rounded cards drifting on a background is the most reliable tell of a template. Edge-to-edge panels divided by hairlines is what expensive instrumentation looks like.

**Masthead, 48px.** Wordmark left. Then run identifier in mono, which is the first 8 hex characters of the ledger root, not a UUID. Then elapsed time in mono, updating every 100ms. Then phase in small-caps. Then, right-aligned and set at 20px mono 500 with the two figures in `--st-survived` and `--text-tertiary`, **the ratio**. It is the largest number on the page and it is the whole product thesis: `07 / 63 SURVIVED`.

**Heat bar, 2px, directly under the masthead.** Full width, `--st-running`. Present only while agents are executing. This is the only continuously animating element in the product.

**The Brief, left, 288px.** Holds the submitted task, verbatim, at 13px in `--text-secondary`, plus the target reference, the ruleset identifier with version, live counters, and a link into the policy drawer. Read-only during a run. The input is not still sitting there waiting to be re-typed. The task has been committed.

**The Lattice, centre-top, ~58% of centre height.** Parallel agents. Specified in section 4.

**The Crucible, centre-bottom, ~42%.** Where findings are attacked. Findings enter from the lattice above and either rise to the Assay or fall out of the panel.

**The Assay, right, 400px.** Three stacks, always visible, never all-collapsed. SURVIVED at top, expanded, cards. REFUTED below with a count in the header and one-line entries. REFUSED at the bottom. The refuted stack is scrollable but its header and first three entries are always on screen. **Never let the kill log collapse to zero height.** It is the evidence that the survivors mean something.

**Footer, 28px.** Connection state, model roster, and the ledger root hash truncated to 16 hex characters in `--steel` mono, always visible, click to copy.

**The ledger spine, 4px, full height, extreme left, always present.** It lives in the layout as a continuous vertical element rather than a tab or a modal, accruing 1px segments as entries are appended, coloured by entry class: tool call `--line`, finding raised `--line-active`, verdict `--st-survived` or `--st-refuted`, refusal `--st-refused`. Over a long run it becomes a legible seismogram of the entire session. Click anywhere on it to open a 480px full-height drawer showing the hash chain: sequence number, timestamp, entry hash, previous hash, payload digest, all in mono, plus a **VERIFY CHAIN** control that recomputes the chain client-side and prints the result with the elapsed time in milliseconds. Making tamper-evidence a permanent structural element of the layout rather than a page you navigate to is the design carrying the claim.

**Policy** lives in the same drawer on a second tab: the active ruleset, every rule with its identifier, and a count of how many times each fired. Every refusal event anywhere in the product cites its rule identifier as a `--steel` mono link into this list. For this audience, a refusal that names the rule it enforced reads as control, not failure. Refusals are styled as first-class events, never as errors.

### 3.3 Responsive

Below 1240px the Assay becomes a bottom drawer with a persistent 36px handle showing the ratio. Below 900px the lattice reduces to 3 lanes plus a reserve counter and the brief collapses to a masthead popover. The ledger spine never leaves.

### 3.4 Spacing and geometry

Scale: `2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 56, 72`. Panel padding 16px, section gaps 20px, tight rows 8px.

Radius: `--r-sm: 2px` on inputs, chips, buttons, cards. `0` on panels and lanes. Nothing above 2px anywhere.

Row heights: dense table 28px, comfortable list 32px, lane header 40px. Not 48px. Density signals that the product expects a competent operator.

Shadows: none in the base layer. Exactly one, reserved for the ledger drawer and dialogs: `0 24px 64px -12px rgba(0,0,0,0.72)`.

---

## 4. The live agent visualisation

This is where the product either impresses or looks like a toy. Three approaches were considered and two rejected.

**Rejected: force-directed node graph.** Illegible past 6 nodes, moves constantly, tells you nothing quantitative, and is the single most common way a serious system is made to look like a screensaver.

**Rejected: unified log firehose.** Honest but unreadable, and it destroys the parallelism, which is the thing worth showing.

**Adopted: the core sample lattice.**

Each agent occupies a **fixed-position vertical lane**, 168px wide, 8px gap, allocated on spawn and **never reordered, never reflowed, never resized.** Positional stability is worth more than optimal packing. An operator learns "the reader is third from the left" within ten seconds and keeps that for the whole run. Up to 8 lanes render at desktop width; beyond that, additional agents queue into a 56px reserve strip on the right showing a count and a stacked micro-bar, and promote into a lane as one retires.

**Lane anatomy, top to bottom:**

```
┌──────────────────────────┐
│ ▣ A-04    VERIFIER       │  40px header: glyph, mono id 11px, role in
│ claude-class · 41 calls  │  10px micro caps, then model + call count
├──────────────────────────┤
│ ░░░░░░░░░░░░░░░░░░░░░░░░ │  compressed core, 1px per call, oldest at top
│ ░░░░░░░░░░░░░░░░░░░░░░░░ │
│ ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ │  refusal, full-bleed 5px bar + 1px keyline
│ ░░░░░░░░░░░░░░░░░░░░░░░░ │
├──────────────────────────┤
│ ▪ read  auth/session.ts  │  the recent 12, at 16px rows, 10.5px mono
│ ▪ grep  "validateToken"  │  truncated with a middle ellipsis
│ ▪ read  auth/verify.ts   │
│ ▸ exec  tsc --noEmit     │  current call, ember left rule, live duration
└──────────────────────────┘
```

**The core sample is the idea.** The most recent 12 tool calls are legible text rows at 16px. Everything older compresses upward into strata: 2px bands per call while under 60 calls, then 1px per call beyond that, no text, colour only. After 400 calls a lane shows a dense visible geology of the work done. The mass is the impressiveness, and it is honest mass, one pixel per real event. Hovering any stratum opens a tooltip on `--surface-overlay` with the full call, arguments, duration, and result size. Clicking pins it and highlights the corresponding ledger segment in the spine.

Stratum colours by tool class, all on `--surface-inset`:

| Class | Colour |
|---|---|
| read / fetch | `#2A3238` |
| search / grep | `#3A444C` |
| analyse / reason | `#4A555E` |
| execute | `--steel` at 70% opacity |
| write / mutate | `--st-running` |
| policy refusal | `--st-refused`, 5px tall, full lane bleed, 1px `--line-active` keyline above and below |

**Parallelism is made legible by a shared time axis.** A 1px `--line-hairline` gridline is drawn straight across every lane at fixed wall-clock intervals, 5 seconds by default, with the timestamp in 10.5px mono in a 44px left gutter. Because every lane shares the axis, simultaneous work aligns horizontally and you can see at a glance that eleven agents were reading at second 40 and only two were still running at second 95. That is the visual proof of the fleet, and it needs no animation to land.

**Lane state is on the left edge, 2px, full lane height:** `--line` for pending, `--st-running` for running, `--line-active` at 40% for retired. Plus the glyph in the header. Plus the role text.

**A refusal is never hidden and never styled as an error.** The full-bleed violet bar interrupts the strata, and the corresponding row in the legible section reads `⊟ refused  POL-07  scope:credential-read` with the rule identifier as a `--steel` link into the policy drawer.

### 4.1 The Crucible: findings under attack

The centre-bottom panel. A finding produced by a producer agent descends into it as a card on `--surface-raised`, 1px `--line`, 2px radius.

**The card carries its attacks on its left edge as scar marks.** Each verifier assigned to it reserves a 3px x 10px vertical mark, stacked with 2px gaps, in `--st-pending`. As each attack resolves, its mark resolves:

- Attack failed to refute: mark fills `--line-active`. A scar. Permanent.
- Attack succeeded: mark fills `--st-refuted` and the card dies.

A finding that survived five attacks displays five grey scars for the rest of the session **and in the exported report.** That is the artefact of trust. It is a small graphic that answers "how do I know this finding is real" without a paragraph of copy, and it is the detail this audience will remember and repeat to a colleague.

While a card is under attack, the verifier's current refutation attempt renders below the finding text at 12px in `--text-tertiary`, prefixed with the verifier identifier in mono. One line, replaced as attempts progress.

**Verdicts:**

- **Refuted.** The card's fill snaps to `--st-refuted-dim`, its text goes `--text-tertiary` with a 1px `--st-refuted` strikethrough, and 90ms later it collapses to a single 24px row and moves to the REFUTED stack. Instant and unceremonious.
- **Survived.** The left border thickens from 1px to 2px in `--st-survived`, the card lifts to `--surface-overlay`, and it eases across into the SURVIVED stack over 320ms. This is the only transition in the product permitted to feel considered.

---

## 5. Motion

```css
:root {
  --dur-instant:   0ms;
  --dur-quick:     120ms;
  --dur-base:      200ms;
  --dur-considered:320ms;
  --dur-seal:      480ms;
  --ease-out:      cubic-bezier(0.2, 0, 0, 1);
  --ease-inout:    cubic-bezier(0.4, 0, 0.2, 1);
}
```

No springs. No bounce. No overshoot. Nothing scales above 1.0.

### 5.1 What animates

| Element | Treatment |
|---|---|
| New stratum band | opacity 0 to 1, `--dur-quick`, `--ease-out`. No height animation. It appears at full height. |
| Current-call line | crossfade `--dur-quick` with `translateY(2px)` on the incoming line only |
| Legible row entering | opacity 0 to 1 over `--dur-quick`; the rows above shift by exactly one row height in the same frame, no stagger |
| Finding descending into the Crucible | `translateY` plus opacity, `--dur-considered`, `--ease-out` |
| Scar mark resolving | colour transition `--dur-base` |
| Survived verdict | border-width, background, and cross-panel move, `--dur-considered`, `--ease-out` |
| Refuted verdict | **`--dur-instant`.** Colour snaps, no transition. Then a 90ms height collapse. |
| Ledger segment appended | opacity 0 to 1, `--dur-quick` |
| Ledger seal on run completion | the root hash sets with a `--dur-seal` opacity and 1px letter-spacing settle, once per run |
| Drawer open | `translateX`, `--dur-base`, `--ease-inout` |
| Idle-to-running page transition | `--dur-considered`, panels wipe left to right from the spine, 40ms stagger between the three columns |

**The heat bar is the only continuous animation in the product.** 2px, full width, `--st-running`, opacity oscillating 0.55 to 1.0 over 2600ms `ease-in-out`, infinite, while and only while agents are executing. Everything else on the page is still. One breathing element in an otherwise motionless interface reads as a pilot light. Twelve of them reads as a toy.

### 5.2 What must not animate, ever

- **Numbers.** No count-up, no odometer, no rolling digits. Counters replace their value in one frame. Tabular figures make that clean.
- **Refutation.** Killing is instant. Making destruction elegant undercuts the entire premise.
- **Lane order.** Lanes never re-sort, never re-pack, never resize when siblings retire.
- **Spinners.** There is not a single spinner in this product. Progress is shown by determinate strata accumulating, or by an elapsed duration in mono, or not at all.
- **Skeleton shimmer.** A loading placeholder is a static 1px dashed `--line` rule. No sweeping gradient.
- **Glow, pulse, halo, or breathing** on anything other than the heat bar.
- **Scroll hijack.** Panels auto-follow only while the user is within 40px of the bottom. The instant they scroll up, following stops and a 24px `SCROLL TO LIVE` pill appears bottom-right. It stays until clicked.
- **Hover lift, scale, or shadow growth** on cards. Hover changes background one surface step and border to `--line-active`. That is all.
- **Route transitions**, parallax, blur reveals, typewriter text, particle effects, confetti on completion.

### 5.3 Reduced motion

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 1ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 1ms !important;
  }
}
```

The heat bar becomes a static `--st-running` bar at 0.85 opacity. The design loses nothing, because the information was never carried by the motion.

### 5.4 Announcements

`aria-live="polite"` fires on verdicts and refusals only, throttled to one announcement per 800ms with coalescing. Tool calls are not announced. A screen reader user gets "Finding 12 survived 5 attacks" rather than eight hundred tool names.

---

## 6. What makes it expensive rather than templated

Concrete, checkable rules. A build that follows all of these will read as expensive even before anyone reads a word of the copy.

1. **One accent, rationed.** `--st-running` covers under 5% of coloured pixels on any screen. Ember appears on the ignite button, the heat bar, write-class strata, and the running glyph. Nowhere else.
2. **Three line values in the entire application.** Not eleven greys chosen ad hoc per component. The restraint is visible even when the individual line is not.
3. **Radius maxes at 2px, panels at 0.** Nothing is pill-shaped. Nothing is a rounded floating card.
4. **Zero shadows in the base layer.** Elevation is expressed only by surface value and a 1px border. One shadow token exists, for the drawer and dialogs.
5. **Full-bleed panels divided by hairlines.** No gutter, no background showing between cards. This alone separates instrument from template.
6. **Tabular, slashed-zero figures everywhere,** with millisecond timestamps and 4-character hash grouping. Precision the user did not ask for reads as competence.
7. **A five-glyph icon set drawn on a 12px grid from one square,** optically aligned rather than mathematically centred. No Lucide, no Heroicons, no Font Awesome.
8. **Row height 28px.** Density communicates that the product expects an expert.
9. **Content-derived identifiers.** The run identifier is the first 8 hex of the ledger root, not `run_a8f2`. It means something and it can be verified.
10. **A VERIFY CHAIN button that actually recomputes the hash chain client-side** and prints `chain verified · 4,812 entries · 61ms`. The audience will click it. It must be real.
11. **Copy written in complete sentences with no exclamation marks anywhere in the product.** The empty Assay does not say "No findings yet!" It says "No claims have been raised. Producer agents are still reading." Errors name the failure and the next action.
12. **The kill log is never collapsible to zero** and the survived-to-raised ratio is the largest number on the page. A product that leads with how much it destroyed is making a claim no template makes.
13. **Exactly one gradient in the application:** a 1px-tall horizontal hairline at the top edge of the Crucible panel, `linear-gradient(90deg, transparent, #E8933A 50%, transparent)` at 40% opacity. One gradient is a decision. Six is a theme.
14. **No illustration, no 3D, no glass, no backdrop-blur, no noise texture.** If a texture is wanted at all, a single 128px monochrome grain tile at 1.5% opacity on `--void` only, and it is optional.
15. **Scars persist into the exported report.** The PDF and the shareable permalink carry the same scar marks, the same ratio, and the same ledger root. Consistency between the live surface and the artefact the decision maker forwards to their director is the last mile most products skip.

---

## 7. Implementation starters

```css
/* Lane */
.lane {
  width: 168px; flex: 0 0 168px;
  background: var(--surface-raised);
  border-left: 2px solid var(--line);          /* status edge */
  border-right: 1px solid var(--line-hairline);
  display: flex; flex-direction: column;
}
.lane[data-state="running"] { border-left-color: var(--st-running); }
.lane[data-state="retired"] { border-left-color: color-mix(in srgb, var(--line-active) 40%, transparent); }

.lane__header {
  height: 40px; padding: 0 8px;
  display: flex; align-items: center; gap: 6px;
  border-bottom: 1px solid var(--line);
}
.lane__id   { font: 400 11px/12px var(--f-mono); color: var(--text-secondary); }
.lane__role { font: 500 10px/12px var(--f-sans); letter-spacing: .10em;
              text-transform: uppercase; color: var(--text-tertiary); }

.lane__core { flex: 1 1 auto; background: var(--surface-inset); overflow: hidden;
              display: flex; flex-direction: column; justify-content: flex-end; }
.stratum    { height: 2px; width: 100%; transition: opacity var(--dur-quick) var(--ease-out); }
.stratum--dense   { height: 1px; }
.stratum--refusal { height: 5px; background: var(--st-refused);
                    box-shadow: 0 -1px 0 var(--line-active), 0 1px 0 var(--line-active); }

.lane__recent { flex: 0 0 auto; border-top: 1px solid var(--line); }
.call { height: 16px; padding: 0 8px; display: flex; gap: 6px; align-items: center;
        font: 400 10.5px/12px var(--f-mono); color: var(--text-tertiary);
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.call--current { color: var(--text-primary); box-shadow: inset 2px 0 0 var(--st-running); }

/* Time gridline, drawn across the whole lattice above the lanes */
.lattice__gridline { position: absolute; left: 44px; right: 0; height: 1px;
                     background: var(--line-hairline); pointer-events: none; }
.lattice__gridline-label { position: absolute; left: 0; width: 40px; text-align: right;
                     font: 400 10.5px/12px var(--f-mono); color: var(--text-tertiary); }

/* Finding card with scars */
.finding { position: relative; background: var(--surface-raised);
           border: 1px solid var(--line); border-radius: 2px;
           padding: 12px 12px 12px 18px; }
.finding__scars { position: absolute; left: 5px; top: 12px;
                  display: flex; flex-direction: column; gap: 2px; }
.scar { width: 3px; height: 10px; background: var(--st-pending);
        transition: background var(--dur-base) var(--ease-out); }
.scar[data-result="held"]   { background: var(--line-active); }
.scar[data-result="killed"] { background: var(--st-refuted); }

.finding[data-verdict="survived"] { border-left: 2px solid var(--st-survived);
                                    background: var(--surface-overlay); }
.finding[data-verdict="refuted"]  { background: var(--st-refuted-dim);
                                    color: var(--text-tertiary);
                                    text-decoration: line-through;
                                    text-decoration-color: var(--st-refuted);
                                    transition: none; }

/* Heat bar, the only perpetual motion in the product */
.heatbar { height: 2px; background: var(--st-running);
           animation: heat 2600ms ease-in-out infinite; }
@keyframes heat { 0%,100% { opacity: .55 } 50% { opacity: 1 } }

/* Focus, one treatment everywhere */
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 2px; }
::selection { background: var(--surface-select); color: var(--text-primary); }
```

---

## 8. The one thing to get right

If the build has to compromise, protect these three in order: **the ratio in the masthead**, **the permanent refuted stack**, and **the scar marks on surviving findings**. They are the same argument stated three times at three scales, and that argument is the entire reason a defence technical decision maker would treat this as serious engineering rather than another agent demo. Everything else in this document is craft supporting them.
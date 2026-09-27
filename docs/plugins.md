# Writing plugins

Plugins are plain files in `~/.config/claudify`:

- `css/<name>.css` is added to every Claude window. Saving the file updates it live.
- `js/<name>.js` runs inside claude.ai's page. Return a cleanup function so the script can be
  turned off or updated without a reload.
- `settings.json` holds the values people pick for each plugin's settings.
- A `.off` suffix (`theme.css.off`) turns a plugin off.

Add one with `claudify add my-plugin.css`, the Import button in the plugins window, or by asking
Claude through the Claudify connector.

## Header and settings

The header comment names the plugin and declares its settings. The plugins window builds a
control for each setting.

```css
/*
 * @name My theme
 * @description What it does.
 * @author you
 * @version 1.0.0
 * @setting accent color #d97757 "Accent color"
 * @setting radius range 12 "Corner radius" min=0 max=24 step=1 unit=px
 * @setting layout select wide "Layout" options="narrow:Narrow|wide:Wide"
 * @setting compact toggle false "Compact spacing"
 * @setting title text "Hello" "Title"
 */
:root { --cds-clay: {{accent}} !important; --cds-radius: {{radius}}px !important; }
/* @if compact */ /* only while "Compact spacing" is on */ /* @endif */
/* @if layout=narrow */ /* ... */ /* @endif */
```

A setting line is `@setting <key> <type> <default> "<label>" [options]`. Quote any value that
contains spaces. The types:

| type | value | options |
| --- | --- | --- |
| `color` | `#rrggbb` | |
| `range`, `number` | a number | `min= max= step= unit=` |
| `select` | one of the options | `options="value:Label\|value:Label"` |
| `toggle` | `true` or `false` | |
| `text` | a string | |
| `file` | a picked file | `accept=image/*,.woff2 max=<MB>` (default 8, at most 32) |

A picked file is copied into `~/.config/claudify/files/` and reaches the plugin as a `data:` URL,
or `""` while nothing is picked, so `url("{{image}}")` works in CSS. Chromium ignores URLs over
2 MB, so images in a stylesheet are limited to 1.5 MB. The plugins window shrinks larger stills to
512 px when you pick them.

## Using values in CSS

- `{{key}}` inserts the value.
- `{{key|hsl}}` turns a color into an HSL triplet, the format of Claude's `--cds-hsl-*` variables.
- `{{key|hsl:+4}}` and `{{key|hex:-8}}` shift the lightness. The shift can use another setting:
  `{{bg|hsl:+lift*1.5}}`. A `~` shift (`{{bg|hsl:~lift}}`) goes lighter on dark colors and darker
  on light ones.
- `{{key|ink:bg}}` moves the color lighter or darker until it is readable on the color in `bg`.
- Filters chain from left to right: `{{text|ink:bg|hsl:~22}}`. `hsl` has to come last.
- `/* @if key */ ... /* @endif */` keeps a block only while the setting is on or non-empty.
  `@if key=value` and `@if key!=value` compare it. Blocks can nest.

## Scripts

A `.js` plugin is the body of a function that receives `settings` and returns a cleanup function:

```js
// @name Hello
// @match ^https://claude\.ai/
// @setting greeting text "Hello" "Greeting"
const el = document.createElement("div");
el.textContent = settings.greeting;
document.body.append(el);
return () => el.remove();
```

`@match` is a regular expression for the page URL. A stylesheet can carry a script too, inside a
`/* @script ... @endscript */` comment. The styles apply from the first paint and the script
runs with the same settings.

## Organizing the settings page

These only change the plugins window:

- `@group "Title"` puts a heading above the settings after it. `@group "Title" toggle=<key>`
  moves that toggle into the heading and folds the group while it is off.
- `if=key`, `if=key=value` and `if=key!=value` (join several with `&`) hide a setting unless the
  condition holds.
- `for=<file key>` puts a range under a file setting as its size, with a preview.
- `empty="..."` says what an unset file falls back to.
- `frames=<file keys>` lets someone pick a single frame of a GIF from those settings or from disk.
  `source=<file key>` stores the GIF the frame came from.
- `share=<file keys>` adds a button that copies a file and its size into those settings.
- `hidden=true` keeps a setting out of the window.

## Finding what to style

Claude's colors come from CSS variables such as `--cds-hsl-gray-850`, `--cds-clay` and
`--cds-text-primary`. Claude sets many of them again on `.cds-root` and deeper elements, so set
yours with `!important` on `*` when a `:root` rule doesn't stick.

To look around the live app, run `claudify inspect '<js>'`, which evaluates the code in every
Claude window. In a chat, Claude can use the connector's `query_dom` tool for the same purpose.
The loader writes its log to `~/.config/claudify/loader.log`.

## How it works

`claudify install` adds one line to the start of Claude's main script inside `app.asar`, which
loads `lib/loader.js`, and packs the plugins window next to it. The original is kept as
`app.asar.orig`. `lib/asar.py` does the repacking in Python, so Node isn't needed. The loader
inserts styles and runs scripts in each window, watches the config folder for changes, and serves
the plugins window over a single IPC channel that only that window can use.

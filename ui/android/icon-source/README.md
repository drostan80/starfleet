# App icon source

`ic_launcher_source.png` — 1024×1024, transparent background, content
centered within the inner ~66% (Android's adaptive-icon safe zone).
User-designed (2026-09-20); the original upload had a baked-in checkerboard
instead of real alpha transparency and was post-processed to fix that and
add safe margin before this version was derived.

To regenerate the launcher icons after a design change, resize this file
(LANCZOS) into each `res/mipmap-<density>/ic_launcher_foreground.png` at
108/162/216/324/432px (mdpi/hdpi/xhdpi/xxhdpi/xxxhdpi), and flatten it onto
`@color/ic_launcher_background` (`res/values/ic_launcher_background.xml`)
at 48/72/96/144/192px for the legacy `ic_launcher.png`/`ic_launcher_round.png`
(the latter circle-masked) — same ratio as the adaptive foreground (48/108).

## Objective
- Fix the arrow "jumps from its place" bug in `I:\PDF preflight\zsenszhoszig-PRESS.pdf` that occurred ONLY when Overprint Preview was toggled ON while viewing Separation Channels / main page (user, Hungarian: "az egyik nyíl elugrik a helyéről, csak akkor ha az overprint preview be van kapcsolva").

## Important Details
- ROOT CAUSE (final, two parts):
  1. Double-overprint smear: In `src/viewer/main_window.py`, the `render_one_data` base render and `_render_full_cmyk` only called `_disable_overprint(doc)` `if not simulate_overprint`. When simulating, the CMYK base was rendered with native overprint ENABLED, then `simulate_overprint_on_cmyk` added overprint AGAIN. The detail/clipped high-res tile is disabled when overprint is ON (main_window.py ~3690-3693), so the main view used the full-page path; the smear visibly displaced the arrow. FIX: both now ALWAYS call `_disable_overprint` (always knockout base; sim reconstructs overprint).
  2. Spurious overprint on fills: In `src/preview/overprint.py`, `simulate_overprint_on_cmyk` (and `build_overprint_position_map`) used `is_overprint = bool(op_fill or op_stroke)` for ALL path types. zsenszhoszig's only overprint flags are STROKE-overprint (/op true) applied to FILL path operations (op#6, op#15: path_fill with op_fill=False, op_stroke=True). A fill is only overprint when its FILL flag (/OP) is true; the stroke-overprint flag is irrelevant for a fill. So these filled shapes were wrongly repainted, nudging the arrow ~2.2pt. FIX: per-type check — path_fill uses op_fill, path_stroke uses op_stroke, path_fs uses either. Also gated the fg color by the matching flag.
- VERIFIED:
  - With the real ICC path (`get_cmyk_icc_path`), zsenszhoszig overprint ON vs OFF: CMYK display diff = 0 px, RGB diff = 0 px. Arrow no longer moves. (The earlier 176711 px "diff" was a test artifact of passing `icc_path=None`, which triggered an inconsistent MuPDF-csRGB vs PIL-convert fallback path; in the real app ICC is always set so both ON/OFF use the same ImageCms transform.)
  - Clipped/detail path matches full path (0 pt displacement) after the knockout fix.
  - `overprint.pdf`: correctly shows NO overprint. Its only op is a path_fill with /OP false, /op true; GS0=(/OP false,/op true), GS1=(/SMask /None, no overprint flags). native==knockout (0 px). The OLD code's "diff" on overprint.pdf was itself a spurious overprint (painting a fill via the stroke-overprint flag) — now correctly gone.
  - `uj.pdf`: renders without crash, overprint ON==OFF consistent.
  - Both files `py_compile` OK. `render_one_data` / main_window import OK.
- PRE-EXISTING LIMITATION (discovered, NOT caused by this fix, out of scope for the reported bug): MuPDF's `get_pixmap` ignores /OP (native==knockout, 0 px on synthetic fill-overprint PDF). The overprint simulation starts from a FULL knockout base, which already painted overprint objects opaquely, so `max(base, fg)` cannot reveal underlying ink for opaque overprint objects. The sim only reveals overprint when the overprint object is lighter than the knockout backdrop. This was true before and after the change. A correct reveal would require rendering the knockout of non-overprint objects only, then compositing overprint objects via max — a larger architectural change. Flag as a follow-up, do not implement now.

## Work State
### Completed
- Double-overprint smear fixed: `render_one_data` base (~669-677) and `_render_full_cmyk` (~635) now ALWAYS call `_disable_overprint` (always knockout base; sim reconstructs overprint). Helpers `_crop_cmyk_to_clip` (~614), `_render_full_cmyk` (~627); clipped overprint/separation reroute; detail tile disabled when overprint ON (~3690-3693).
- Spurious-overprint-on-fills fixed: per-op-type overprint check in `simulate_overprint_on_cmyk` (~651-662) and fg color gating (~719-723); same logic in `build_overprint_position_map` (~918-929). path_fill uses op_fill, path_stroke uses op_stroke, path_fs uses either.
- `overprint.py` earlier `_parse_content_sequence` Do/xobject fixes and SyntaxWarning fix remain.
- `render_engine.py`: `_RENDER_LOCK`, `_disable_overprint`, `_restore_overprint` in place.
- Verified: zsenszhoszig ON vs OFF CMYK/RGB diff = 0 px (real ICC path); clipped==full (0 pt); overprint.pdf native==knockout (0 px, no spurious overprint); uj.pdf no crash, ON==OFF consistent; py_compile OK; imports OK.

### Active
- (none)

### Blocked
- (none)

## Next Move
1. Ask user to close the running instance and relaunch via the .bet script, then visually confirm the arrow stays put when toggling Separation Channels WITH Overprint Preview ON (and that the gross smear/clip-edge displacement is gone).
2. Optionally offer to address the pre-existing overprint-reveal limitation as a separate task.

## Relevant Files
- `src/viewer/main_window.py`: always `_disable_overprint` in `render_one_data` base (~669-677) and `_render_full_cmyk` (~635); helpers `_crop_cmyk_to_clip` (~614), `_render_full_cmyk` (~627); clipped overprint/separation reroute; detail tile disabled when overprint ON (~3690-3693).
- `src/preview/overprint.py`: per-op-type overprint check in `simulate_overprint_on_cmyk` (~651-662) and fg color gating (~719-723); same logic in `build_overprint_position_map` (~918-929); earlier `_parse_content_sequence` Do/xobject fixes and SyntaxWarning fix remain.
- `src/viewer/render_engine.py`: `_RENDER_LOCK`, `_disable_overprint`, `_restore_overprint`.
- `I:\PDF preflight\zsenszhoszig-PRESS.pdf` (problem file), `overprint.pdf` (reference, no genuine overprint), `uj.pdf` (regression smoke test).

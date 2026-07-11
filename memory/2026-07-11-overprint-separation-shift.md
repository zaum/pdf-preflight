## Objective
- Javítani az overprint/separation preview nézet hibáit a PDF Preflight Viewerben: (1) a "Simulate Overprint"/separation gombokra 1 pixelt csúszik a kép/viewport jobbra, (2) overprint bekapcsolásakor a fekete szöveg téglalapként jelenik meg, (3) a kör/görbe alakzatok négyzetté válása.

## Important Details
- MuPDF `get_pixmap` (PyMuPDF 1.26.6) **teljesen figyelmen kívül hagyja** az overprint-ot → saját szimuláció szükséges (`simulate_overprint_on_cmyk`).
- `render_one_data(doc, page_num, zoom, mode, channels, icc_path, sim_profile, simulate_overprint)` a központi render függvény (`src/viewer/main_window.py:520`).
- Separation UI gombok: `chk_c/m/y/k` (`main_window.py`), `channels` dict kerül átadásra.
- Separation mód (akár 4 csatorna is) `simulate_overprint_on_cmyk`-on megy keresztül ha `simulate_overprint` be van kapcsolva (alapértelmezett ON) → így a separation gomb is aktiválja a szimulációt.
- MuPDF `get_pixmap` **kerekít** (round) a PDF-koordinátákból pixelbe; a régebbi kód `int()`-tel (floor) számolta a régió origóját → 1px elcsúszás.
- A maszkot (`_drawing_coverage_mask`) ugyanazzal a `Matrix(zoom, zoom)`-mal kell renderelni, mint a fő pixmap-et; a bbox arányos átméretezése (`target_w/bw`) egész pixelnyi eltolást okoz nagy rajzoknál (pl. 3px overprint.pdf-nél).
- Valós teszt PDF-ek: `overprint.pdf`, `uj.pdf` (mindkettő `detect_overprint=True`). Kézzel épített minimális PDF-ek (kör+overprint, fekete kör + sárga alatta, szöveg) a verifikációhoz.
- `run.bat` csak elindítja a viewert, nem nyit auto recent fájlt.

## Work State
### Completed
- Adat-szintű 1px horizontális elcsúszás javítva: `simulate_overprint_on_cmyk` régió origója `int()`→`round()` (`x0/y0/x1/y1 = int(round(...))`, clampolva w/h-ra, `overprint.py:620-623`); szöveg-overprint origó is `round()` (`overprint.py:668-671`).
- Szöveg-overprint téglalap hiba javítva: a szöveg ág már nem tölti ki a bboxot, hanem a valódi knockout-glyph-eket használja a `cmyk_arr`-ből (`np.maximum(region, src)`, `overprint.py:683-692`).
- Overprint objektumok: pontos útmaszk (`_drawing_coverage_mask`) + per-csatorna `max` kompozíció → körök megmaradnak körnek.
- **Non-overprint (knockout) square bug MEGOLDVA:** minden festett objektum a pontos maszkjával kompozitálódik (non-overprint = knockout/replace a maszkban, overprint = max). Korábban a non-overprint ág szilárd bboxot írt ki (`cov=1.0`), ami négyzetté tette az alakzatokat és eltolta a látszólagos éleket. (`overprint.py:628-661`)
- **3px mask-shift MEGOLDVA:** `_drawing_coverage_mask` most `Matrix(zoom, zoom)`-mal renderel (nem `target_w/bw` átméretezéssel), így a maszk pixelrácsa azonos a `cmyk_arr`-ével; a hívó (`simulate_overprint_on_cmyk`) a régióra crop-polja a maszkot (pad ha 1px kisebb). (`overprint.py:528-536` + crop `overprint.py:640-657`)
- `np.maximum(result, cmyk_arr)` megőrzi a képek/ICC/spot tartalmat; `active_channels` maszkolás megmaradt (`overprint.py` vége).
- Ellenőrzés valós PDF-eken (`overprint.pdf`, `uj.pdf`, zoom=2): normal vs separation(sim ON) minden csatornára **best shift = 0**. A C/M/Y eltérések valós overprint színkülönbségek (helyes), nem eltolás. Separation (csak K) is shift 0, C/M/Y maskolva 0-ra.
- Szintetikus teszt (sárga kör + fekete kör overprint, zoom=2): K shift 0, overprint blend (sárga a fekete alatt) 28813 px megőrződik, bbox sarok fehér (nincs négyzet). Másik teszt (fekete kör + zöld kör overprint): zöld a fekete alatt 45252 px, K=255 megőrződik (helyes overprint).
- `py_compile` OK.

### Active
- (none)

### Blocked
- (none)

## DISPLAY-OLDALI CSÚSZÁS (viewport) — MEGOLDVA
- A felhasználó 1-2px jobbra csúszást látott separation/overprint gombokra. Adatszinten a render 0px eltolású (azonos zoom/dimenzió minden módban: `render_one_data` normal vs separation vs overprint shape `(1684,1191,3)`, shift 0).
- **Valódi ok (headless Qt reprodukcióval igazolva):** `_display_render_result` (`main_window.py:2899`) újra-középre igazított `centerOn(old_pdf_center * zoom)`-al, ahol `old_pdf_center = mapToScene(vp.center()) / _last_render_zoom`. `vp.center()` **egész számú** widget-koordináta, és a nézet skálázva van (pl. zoom 2 → scale 0.5), így 1 widget px = 2 scene px. A kerekítési hiba felnagyítódik és **MINDEN re-rendernél azonos irányba halmozódik** (~2px/jobbra). Headless teszt: top-left scene (-200 → -202 → -204) minden rendernél.
- **Első (téves) gyanú:** a detail overlay `item_scale = base_zoom/detail_zoom` helyett tényleges csempe méret; kijavítva (`_on_detail_ready` + `set_detail_overlay` QTransform, origó snappolás). Ez is javítva, de ÖNMAGÁBAN nem oldotta meg a csúszást.
- **Tényleges javítás:** ha a render zoom változatlan (`abs(zoom-_last_render_zoom)<0.001`), a nézet pozíciója a **scrollbar értékek mentésével/visszaállításával** őrződik meg (`main_window.py` `_display_render_result`: `preserve_scroll`, `saved_sx/saved_sy`, `setValue`), nem centerOn-nal. Zoomváltáskor továbbra is a régi `old_pdf_center` recenter logika fut (ott a kis eltérés elfogadható). Headless teszt utána: top-left stabil (-200, 246) minden rendernél, 0 drift.

## Next Move
- Opcionális: vizuális/GUI ellenőrzés (futtatható viewer) hogy separation/overprint gomboknál a viewport tényleg nem csúszik. Headless Qt teszt igazolja a 0 drift-ot.
- Opcionális teljesítmény: `_drawing_coverage_mask` minden festett objektumhoz maszkot renderel (zoom-szinten, cache-elve `_MASK_CACHE` kulccsal). Nagy oldalaknál több temp-page render; elfogadható.

## Relevant Files
- `I:\PDF preflight\src\preview\overprint.py` — `simulate_overprint_on_cmyk` (round origó:620-623; szöveg glyph-maszk:683-692; maszk crop+knockout/replace:628-661; mask zoom-render:528-536), `_drawing_coverage_mask` (cache, zoom render), `_resize_to`, `OverprintPreview.detect_overprint`, `_parse_content_sequence`.
- `I:\PDF preflight\src\viewer\main_window.py` — `render_one_data` (520), overprint/separation ág, `_display_render_result` (old_pdf_center, centerOn), `_on_sep_changed` (3324), `_on_overprint_sim_toggled`, detail overlay (`set_detail_overlay`, cx0/cy0 * base_zoom).
- `I:\PDF preflight\src\preview\separation.py` — `SeparationPreview.composite` (csatorna maszkolás).
- `I:\PDF preflight\src\viewer\page_widget.py` — `set_pixmap`, `set_detail_overlay` (scene pozicionálás).
- `I:\PDF preflight\overprint.pdf`, `I:\PDF preflight\uj.pdf` — valós teszt PDF-ek (detect_overprint=True).

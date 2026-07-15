import numpy as np
import re

import fitz


class OverprintPreview:
    def detect_overprint(self, page):
        try:
            pag_obj = page.parent.xref_object(page.xref)
            if '/ExtGState' in pag_obj:
                egc = pag_obj.split('/ExtGState')[1]
                if re.search(r'/(OP|op)\s+true(?![A-Za-z])', egc):
                    return True
        except Exception:
            pass
        try:
            for xref_i in range(1, page.parent.xref_length()):
                obj = page.parent.xref_object(xref_i)
                if '/Type' in obj and '/ExtGState' in obj:
                    if re.search(r'/(OP|op)\s+true(?![A-Za-z])', obj):
                        return True
        except Exception:
            pass
        return False

    def simulate(self, cmyk_arr, page):
        if cmyk_arr is None:
            return None, False
        has_op = self.detect_overprint(page)
        return cmyk_arr.copy(), has_op


def _get_extgstate_overprints(doc, page):
    """Parse page /Resources -> /ExtGState and return {name: (has_OP, op_val, has_op, op_val)}.

    Returns a dict where each value is (has_OP, OP_value, has_op, op_value).
    has_OP/has_op are True if the ExtGState explicitly sets /OP or /op.
    OP_value/op_value are the boolean values.

    This is important: in PDF, if an ExtGState doesn't include /OP, the current
    overprint fill flag is UNCHANGED. Same for /op and stroke."""
    try:
        page_obj = doc.xref_object(page.xref)
    except Exception:
        return {}

    resources_str = None
    m_res = re.search(r'/Resources\s+<<(.*?)>>', page_obj, re.DOTALL)
    if m_res:
        resources_str = m_res.group(1)
    else:
        m_ref = re.search(r'/Resources\s+(\d+)\s+0\s+R', page_obj)
        if m_ref:
            res_xref = int(m_ref.group(1))
            try:
                res_obj = doc.xref_object(res_xref)
            except Exception:
                return {}
            m_res2 = re.search(r'/ExtGState\s*<<(.*?)>>', res_obj, re.DOTALL)
            if m_res2:
                resources_str = m_res2.group(1)

    if not resources_str:
        all_text = page_obj
        m_ref = re.search(r'/Resources\s+(\d+)\s+0\s+R', page_obj)
        if m_ref:
            try:
                all_text += doc.xref_object(int(m_ref.group(1)))
            except Exception:
                pass
        m_eg = re.search(r'/ExtGState\s*<<(.*?)>>', all_text, re.DOTALL)
        if m_eg:
            resources_str = m_eg.group(1)
        else:
            return {}

    entries = re.findall(r'/(\w+)\s+(\d+)\s+0\s+R', resources_str)
    result = {}
    for name, xref_str in entries:
        xref = int(xref_str)
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        # IMPORTANT: /OP must NOT match /OPM (Overprint *Mode*), which is a
        # different, very common key that does NOT enable overprint.
        has_OP = bool(re.search(r'/OP(?![A-Za-z])', obj))
        has_op = bool(re.search(r'/op(?![A-Za-z])', obj))
        if has_OP or has_op:
            op_fill = bool(re.search(r'/OP\s+true(?![A-Za-z])', obj)) if has_OP else None
            op_stroke = bool(re.search(r'/op\s+true(?![A-Za-z])', obj)) if has_op else None
            result[name] = (has_OP, op_fill, has_op, op_stroke)
    return result


def _parse_content_sequence(doc, page):
    """Parse the page content stream(s) and return a list of operations in order.
    
    Each operation is a dict with keys:
      type: 'path_fill', 'path_stroke', 'path_fs', 'text'
      fill_color: DeviceCMYK tuple (c,m,y,k) 0-1, or None
      stroke_color: DeviceCMYK tuple or None  
      overprint_fill: bool
      overprint_stroke: bool
      index: sequence number for correlation with get_cdrawings
    """
    from preview.content_stream import _tokenize

    op_gs = _get_extgstate_overprints(doc, page)
    if not op_gs:
        return []

    page_height = page.rect.height
    xrefs = page.get_contents()
    if not xrefs:
        return []

    gs = {
        'fill_cs': 'DeviceGray',
        'fill_color': (0,),
        'stroke_cs': 'DeviceGray',
        'stroke_color': (0,),
        'overprint_fill': False,
        'overprint_stroke': False,
    }
    gs_stack = []

    tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    font_size = 0.0
    leading = 0.0

    operations = []

    for xri in xrefs:
        stream = doc.xref_stream(xri)
        if not stream:
            continue
        tokens = _tokenize(stream)
        pos = 0
        n = len(tokens)

        def _pop(count):
            nonlocal pos
            r = []
            j = pos - 1
            found = 0
            while j >= 0 and found < count:
                t = tokens[j]
                if isinstance(t, (int, float)):
                    r.insert(0, float(t))
                    found += 1
                elif isinstance(t, str) and t.startswith('/'):
                    r.insert(0, t)
                    found += 1
                elif t == ']':
                    depth = 1
                    j -= 1
                    while j >= 0 and depth > 0:
                        if tokens[j] == ']':
                            depth += 1
                        elif tokens[j] == '[':
                            depth -= 1
                        j -= 1
                    continue
                elif t == '[':
                    j -= 1
                    continue
                else:
                    break
                j -= 1
            return r

        def _cmyk_color(fill_color, fill_cs):
            """Convert to DeviceCMYK tuple (c,m,y,k) 0-1."""
            if fill_cs == 'DeviceCMYK' and len(fill_color) >= 4:
                return tuple(float(v) for v in fill_color[:4])
            elif fill_cs == 'DeviceGray' and len(fill_color) >= 1:
                return (0.0, 0.0, 0.0, 1.0 - float(fill_color[0]))
            elif fill_cs == 'DeviceRGB' and len(fill_color) >= 3:
                r, g, b = float(fill_color[0]), float(fill_color[1]), float(fill_color[2])
                k = 1.0 - max(r, g, b)
                if k >= 1.0:
                    return (0.0, 0.0, 0.0, 1.0)
                c = (1.0 - r - k) / (1.0 - k) if (1.0 - k) > 0 else 0.0
                m = (1.0 - g - k) / (1.0 - k) if (1.0 - k) > 0 else 0.0
                y = (1.0 - b - k) / (1.0 - k) if (1.0 - k) > 0 else 0.0
                return (c, m, y, k)
            return None

        while pos < n:
            tok = tokens[pos]

            if tok == 'q':
                gs_stack.append(dict(gs))
                pos += 1
                continue
            if tok == 'Q':
                if gs_stack:
                    gs = gs_stack.pop()
                pos += 1
                continue

            # ExtGState
            if tok == 'gs':
                ops = _pop(1)
                if ops and isinstance(ops[0], str) and ops[0].startswith('/'):
                    name = ops[0][1:]
                    if name in op_gs:
                        has_OP, op_fill, has_op, op_stroke = op_gs[name]
                        if has_OP:
                            gs['overprint_fill'] = op_fill
                        if has_op:
                            gs['overprint_stroke'] = op_stroke
                    # If name not in op_gs, ExtGState has no /OP or /op — keep current flags
                pos += 1
                continue

            # XObject (image / Form XObject) placement.
            # get_cdrawings() reports these as drawings too: an image becomes a
            # single rect drawing, and a Form XObject is expanded into its own
            # path drawings. The operation<->drawing index correlation in
            # simulate_overprint_on_cmyk() would otherwise drift by the number
            # of XObjects, painting every later object's ink onto the wrong
            # geometry -- which made vector elements (e.g. arrows) "jump" to the
            # wrong position. Record the placement so the index stays aligned.
            if tok == 'Do':
                operations.append({
                    'type': 'xobject',
                    'overprint_fill': gs['overprint_fill'],
                    'overprint_stroke': gs['overprint_stroke'],
                })
                pos += 1
                continue

            # Fill color
            if tok == 'k':
                ops = _pop(4)
                if len(ops) >= 4 and all(isinstance(o, (int, float)) for o in ops[:4]):
                    gs['fill_cs'] = 'DeviceCMYK'
                    gs['fill_color'] = tuple(ops[:4])
                pos += 1
                continue
            if tok == 'rg':
                ops = _pop(3)
                if len(ops) >= 3 and all(isinstance(o, (int, float)) for o in ops[:3]):
                    gs['fill_cs'] = 'DeviceRGB'
                    gs['fill_color'] = tuple(ops[:3])
                pos += 1
                continue
            if tok == 'g':
                ops = _pop(1)
                if len(ops) >= 1 and isinstance(ops[0], (int, float)):
                    gs['fill_cs'] = 'DeviceGray'
                    gs['fill_color'] = (ops[0],)
                pos += 1
                continue
            # sc / scn — set fill color in current colorspace
            if tok in ('sc', 'scn'):
                if gs['fill_cs'] == 'DeviceGray':
                    ops = _pop(1)
                elif gs['fill_cs'] == 'DeviceRGB':
                    ops = _pop(3)
                elif gs['fill_cs'] == 'DeviceCMYK':
                    ops = _pop(4)
                else:
                    # Spot color or unknown — mark as spot, skip for overprint
                    gs['fill_cs'] = 'Spot'
                    gs['fill_color'] = None
                    pos += 1
                    continue
                if ops and all(isinstance(o, (int, float)) for o in ops):
                    gs['fill_color'] = tuple(ops)
                pos += 1
                continue

            # Stroke color
            if tok == 'K':
                ops = _pop(4)
                if len(ops) >= 4 and all(isinstance(o, (int, float)) for o in ops[:4]):
                    gs['stroke_cs'] = 'DeviceCMYK'
                    gs['stroke_color'] = tuple(ops[:4])
                pos += 1
                continue
            if tok == 'RG':
                ops = _pop(3)
                if len(ops) >= 3 and all(isinstance(o, (int, float)) for o in ops[:3]):
                    gs['stroke_cs'] = 'DeviceRGB'
                    gs['stroke_color'] = tuple(ops[:3])
                pos += 1
                continue
            if tok == 'G':
                ops = _pop(1)
                if len(ops) >= 1 and isinstance(ops[0], (int, float)):
                    gs['stroke_cs'] = 'DeviceGray'
                    gs['stroke_color'] = (ops[0],)
                pos += 1
                continue
            # SC / SCN — set stroke color in current colorspace
            if tok in ('SC', 'SCN'):
                if gs['stroke_cs'] == 'DeviceGray':
                    ops = _pop(1)
                elif gs['stroke_cs'] == 'DeviceRGB':
                    ops = _pop(3)
                elif gs['stroke_cs'] == 'DeviceCMYK':
                    ops = _pop(4)
                else:
                    gs['stroke_cs'] = 'Spot'
                    gs['stroke_color'] = None
                    pos += 1
                    continue
                if ops and all(isinstance(o, (int, float)) for o in ops):
                    gs['stroke_color'] = tuple(ops)
                pos += 1
                continue

            # Colorspace names
            if tok == 'cs':
                ops = _pop(1)
                if ops and isinstance(ops[0], str):
                    if ops[0] == '/DeviceCMYK':
                        gs['fill_cs'] = 'DeviceCMYK'
                    elif ops[0] == '/DeviceRGB':
                        gs['fill_cs'] = 'DeviceRGB'
                    elif ops[0] == '/DeviceGray':
                        gs['fill_cs'] = 'DeviceGray'
                    else:
                        gs['fill_cs'] = 'Spot'
                        gs['fill_color'] = None
                pos += 1
                continue
            if tok == 'CS':
                ops = _pop(1)
                if ops and isinstance(ops[0], str):
                    if ops[0] == '/DeviceCMYK':
                        gs['stroke_cs'] = 'DeviceCMYK'
                    elif ops[0] == '/DeviceRGB':
                        gs['stroke_cs'] = 'DeviceRGB'
                    elif ops[0] == '/DeviceGray':
                        gs['stroke_cs'] = 'DeviceGray'
                    else:
                        gs['stroke_cs'] = 'Spot'
                        gs['stroke_color'] = None
                pos += 1
                continue

            # Text state
            if tok == 'BT':
                tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
                tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
                pos += 1
                continue
            if tok == 'ET':
                pos += 1
                continue
            if tok == 'Tm':
                ops = _pop(6)
                if len(ops) >= 6:
                    tm = list(ops[:6])
                    tlm = list(ops[:6])
                pos += 1
                continue
            if tok == 'Td':
                ops = _pop(2)
                if len(ops) >= 2:
                    tlm[4] += ops[0]
                    tlm[5] += ops[1]
                    tm = list(tlm)
                pos += 1
                continue
            if tok == 'TD':
                ops = _pop(2)
                if len(ops) >= 2:
                    leading = -ops[1]
                    tlm[4] += ops[0]
                    tlm[5] += ops[1]
                    tm = list(tlm)
                pos += 1
                continue
            if tok == 'T*':
                tlm[5] = tlm[5] - leading
                tm = list(tlm)
                pos += 1
                continue
            if tok == 'Tf':
                ops = _pop(2)
                if len(ops) >= 2:
                    font_size = float(ops[1])
                pos += 1
                continue

            # Text showing
            if tok in ('Tj', 'TJ', "'", '"'):
                cmyk = _cmyk_color(gs['fill_color'], gs['fill_cs'])
                if cmyk:
                    operations.append({
                        'type': 'text',
                        'fill_color': cmyk,
                        'overprint_fill': gs['overprint_fill'],
                        'overprint_stroke': gs['overprint_stroke'],
                        'x': tm[4],
                        'y': tm[5],
                        'font_size': font_size,
                    })
                if tok == 'TJ':
                    tm[4] += font_size * 0.5
                elif tok == 'Tj':
                    tm[4] += font_size * 0.5
                pos += 1
                continue

            # Path painting – correlate with get_cdrawings by order
            if tok in ('f', 'F', 'f*'):
                cmyk = _cmyk_color(gs['fill_color'], gs['fill_cs'])
                operations.append({
                    'type': 'path_fill',
                    'fill_color': cmyk,
                    'stroke_color': None,
                    'overprint_fill': gs['overprint_fill'],
                    'overprint_stroke': gs['overprint_stroke'],
                })
                pos += 1
                continue
            if tok in ('S', 's'):
                cmyk = _cmyk_color(gs['stroke_color'], gs['stroke_cs'])
                operations.append({
                    'type': 'path_stroke',
                    'fill_color': None,
                    'stroke_color': cmyk,
                    'overprint_fill': gs['overprint_fill'],
                    'overprint_stroke': gs['overprint_stroke'],
                })
                pos += 1
                continue
            if tok in ('B', 'B*', 'b', 'b*'):
                fc = _cmyk_color(gs['fill_color'], gs['fill_cs'])
                sc = _cmyk_color(gs['stroke_color'], gs['stroke_cs'])
                operations.append({
                    'type': 'path_fs',
                    'fill_color': fc,
                    'stroke_color': sc,
                    'overprint_fill': gs['overprint_fill'],
                    'overprint_stroke': gs['overprint_stroke'],
                })
                pos += 1
                continue

            if tok == 'n':
                # End path without painting — no color to record but consume sequence
                pos += 1
                continue

            pos += 1

    return operations


def _resize_to(arr, th, tw):
    if arr.shape[0] == th and arr.shape[1] == tw:
        return arr
    ys = (np.arange(th) * (arr.shape[0] / th)).astype(np.intp)
    xs = (np.arange(tw) * (arr.shape[1] / tw)).astype(np.intp)
    return arr[np.ix_(ys, xs)]


def _pt(v, ox, oy):
    """Convert a point-ish value (fitz.Point or (x, y) tuple) to a local Point."""
    if hasattr(v, 'x'):
        return fitz.Point(v.x - ox, v.y - oy)
    return fitz.Point(v[0] - ox, v[1] - oy)


_MASK_CACHE = {}


def _drawing_coverage_mask(d, zoom, cache_key, target_w=None, target_h=None):
    """Return a float32 [H, W] coverage mask (0..1) of the drawing's painted
    area (fill and/or stroke), rasterized with accurate path geometry.

    The mask is rendered for the drawing's bounding box and scaled to exactly
    ``(target_h, target_w)`` pixels so it aligns pixel-for-pixel with the
    region slice it will be composited into. Returns None on failure, in which
    case the caller should fall back to the bounding box.

    ``cache_key`` must be stable across renders of the same drawing/zoom
    (e.g. (id(doc), page.xref, seqno, zoom)) -- ``d`` itself is recreated on
    every ``get_cdrawings`` call, so its ``id`` must not be used for caching."""
    key = cache_key
    cached = _MASK_CACHE.get(key)
    if cached is not None:
        return cached

    r = d.get('rect')
    if r is None:
        _MASK_CACHE[key] = None
        return None
    if hasattr(r, 'x0'):
        x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
    else:
        x0, y0, x1, y1 = r[0], r[1], r[2], r[3]
    bw = x1 - x0
    bh = y1 - y0
    if bw <= 0 or bh <= 0:
        _MASK_CACHE[key] = None
        return None
    if d.get('fill') is None and d.get('stroke') is None:
        _MASK_CACHE[key] = None
        return None

    try:
        tmp = fitz.open()
        pg = tmp.new_page(width=bw, height=bh)
        sh = pg.new_shape()
        ok = False
        for it in d.get('items', []):
            t = it[0]
            if t == 're':
                rr = it[1]
                if hasattr(rr, 'x0'):
                    sh.draw_rect(fitz.Rect(rr.x0 - x0, rr.y0 - y0,
                                           rr.x1 - x0, rr.y1 - y0))
                else:
                    sh.draw_rect(fitz.Rect(rr[0] - x0, rr[1] - y0,
                                           rr[2] - x0, rr[3] - y0))
                ok = True
            elif t == 'l':
                p1, p2 = it[1], it[2]
                sh.draw_line(_pt(p1, x0, y0), _pt(p2, x0, y0))
                ok = True
            elif t == 'c':
                p1, p2, p3, p4 = it[1], it[2], it[3], it[4]
                sh.draw_bezier(_pt(p1, x0, y0), _pt(p2, x0, y0),
                               _pt(p3, x0, y0), _pt(p4, x0, y0))
                ok = True
        if not ok:
            tmp.close()
            _MASK_CACHE[key] = None
            return None
        sh.finish(
            fill=(1.0, 0.0, 0.0) if d.get('fill') is not None else None,
            color=(1.0, 0.0, 0.0) if d.get('stroke') is not None else None,
            width=(d.get('width') or 1.0),
            closePath=bool(d.get('closePath', False)),
            even_odd=bool(d.get('even_odd', False)),
        )
        sh.commit()
        # Render the mask at the SAME zoom as the main pixmap so its pixel grid
        # is identical to cmyk_arr (both use Matrix(zoom) over the same global
        # origin). Scaling the bbox to an exact region size with a different
        # matrix drifts by whole pixels on large drawings. The caller crops the
        # result to the exact region slice.
        mat = fitz.Matrix(zoom, zoom)
        pm = pg.get_pixmap(matrix=mat, alpha=True)
        tmp.close()
    except Exception:
        _MASK_CACHE[key] = None
        return None

    if pm.n < 4 or pm.height == 0 or pm.width == 0:
        _MASK_CACHE[key] = None
        return None
    alpha = np.frombuffer(pm.samples, dtype=np.uint8)
    alpha = alpha.reshape(pm.height, pm.width, pm.n)[:, :, 3].astype(np.float32) / 255.0
    _MASK_CACHE[key] = alpha
    return alpha


def simulate_overprint_on_cmyk(cmyk_arr, page, doc, active_channels=None):
    """Overprint simulation for separation / overprint preview.

    Uses page.get_cdrawings() for precise path geometry and content stream
    parsing for CMYK colors and overprint flags. Blends per-channel using
    max(fg, bg) where overprint is active (compositing in content-stream order,
    which correctly handles "overprint on top").

    ``active_channels`` (dict cyan/magenta/yellow/black -> bool) optionally
    masks the result to the requested separation plates so that overprint
    simulation does not destroy a separation channel selection.
    """
    if cmyk_arr is None or cmyk_arr.size == 0:
        return cmyk_arr

    # Parse operations from content stream
    operations = _parse_content_sequence(doc, page)
    if not operations:
        return cmyk_arr.copy()

    # Get drawings for geometry (rgb colors, but we use cs parser for cmyk).
    # In this PyMuPDF version get_cdrawings() reports `fill`/`stroke` as the
    # actual color (RGB/CMYK tuple) or None when the path is not painted, and
    # `fill_color`/`stroke_color` are not populated. So a path paints when
    # `fill`/`stroke` is not None. Clip-only paths have both None and are
    # excluded automatically.
    try:
        raw_drawings = page.get_cdrawings()
    except Exception:
        raw_drawings = []
    drawings = [d for d in raw_drawings
                if d.get('fill') is not None or d.get('stroke') is not None]

    page_height = page.rect.height

    # Determine zoom factor of the input cmyk_arr
    zoom = cmyk_arr.shape[0] / page_height if page_height > 0 else 1.0
    h, w = cmyk_arr.shape[:2]

    # Build overprint canvas from scratch (blank page).
    # All objects composited in original content stream order (z-order).
    # Knockout: replace channels. Overprint: per-channel max.
    result = np.zeros_like(cmyk_arr, dtype=np.float32)

    # Track drawing index for path correlation
    drawing_idx = 0

    for op in operations:
        if op['type'].startswith('path'):
            if drawing_idx >= len(drawings):
                drawing_idx += 1
                continue
            d = drawings[drawing_idx]
            drawing_idx += 1

            fc = op.get('fill_color')
            sc = op.get('stroke_color')
            if fc is None and sc is None:
                continue

            r = d.get('rect')
            if r is None:
                continue
            if hasattr(r, 'x0'):
                rx0, ry0, rx1, ry1 = r.x0, r.y0, r.x1, r.y1
            else:
                rx0, ry0, rx1, ry1 = r[0], r[1], r[2], r[3]

            # Align the region origin with MuPDF's get_pixmap pixel grid: that
            # function rounds (not floors) PDF coordinates to pixels, so we must
            # use round() here too. Otherwise the simulated overprint layer is
            # offset by up to one pixel relative to the knockout render.
            x0 = int(max(0, min(w, round(rx0 * zoom))))
            x1 = int(max(0, min(w, round(rx1 * zoom))))
            y0 = int(max(0, min(h, round(ry0 * zoom))))
            y1 = int(max(0, min(h, round(ry1 * zoom))))

            if x0 >= x1 or y0 >= y1:
                continue

            region = result[y0:y1, x0:x1]

            op_fill = bool(op.get('overprint_fill'))
            op_stroke = bool(op.get('overprint_stroke'))
            # A fill operation is only overprint when its FILL overprint flag
            # (/OP) is set; a stroke overprint flag (/op) is meaningless for a
            # fill. Likewise a stroke operation only overprints on /op. Mixing
            # the two (e.g. a filled triangle that merely carries a stroke
            # overprint flag) would wrongly repaint the object and shift it.
            if op['type'] == 'path_fill':
                is_overprint = op_fill
            elif op['type'] == 'path_stroke':
                is_overprint = op_stroke
            else:  # path_fs / path_b: either flag makes the op overprint
                is_overprint = op_fill or op_stroke

            # Non-overprint objects are already represented exactly by the
            # knockout render (`cmyk_arr`), which is restored at the end via
            # `np.maximum(result, cmyk_arr)`. Painting them here again would only
            # re-introduce anti-aliased edge artifacts, so we skip them and let
            # the knockout render stand. Only genuine overprint objects (whose
            # ink must be added on top of the backdrop) are painted here.
            if not is_overprint:
                continue

            # Every painted object needs its true path geometry, not its
            # bounding box -- otherwise shapes would render as filled squares
            # and their edges would appear shifted. Overprint objects are
            # composited additively (max) so they add ink on top of the layer
            # already built below them.
            region_w = x1 - x0
            region_h = y1 - y0

            # Coverage mask: the object's OWN shape from the geometry (so it is
            # isolated from overlapping objects), intersected with where real
            # ink actually exists in the knockout. This prevents the mask from
            # over-covering the background while still being the correct shape,
            # so an object's ink is only ever painted at its true position.
            seqno = d.get('seqno', drawing_idx)
            cache_key = (id(doc), page.xref, seqno, region_w, region_h)
            mask = _drawing_coverage_mask(
                d, zoom, cache_key, target_w=region_w, target_h=region_h)
            if mask is not None:
                cov = mask[0:region_h, 0:region_w]
                if cov.shape[0] < region_h or cov.shape[1] < region_w:
                    padded = np.zeros((region_h, region_w), dtype=np.float32)
                    padded[0:cov.shape[0], 0:cov.shape[1]] = cov
                    cov = padded
            else:
                cov = np.ones((region_h, region_w), dtype=np.float32)

            # The geometry comes from get_cdrawings() (always correct). The
            # color can come from two sources that must agree:
            #   * the parsed content stream (fg) -- the object's true stored
            #     CMYK, needed so genuine overprint is simulated correctly;
            #   * the knockout render (cmyk_arr) sampled at this exact geometry
            #     -- always correct in both color and position.
            # The content-stream <-> drawing index correlation can drift (images,
            # Form XObjects, clip paths, paint-order differences), which would
            # paint an object's ink onto the wrong geometry and make vector
            # elements (e.g. arrows) "jump". Detect that: if the parsed color
            # disagrees with the true knockout ink here, the correlation is off,
            # so fall back to the knockout ink (correct position, no jump). When
            # they agree, the parsed color is used (so overprint is simulated).
            src = cmyk_arr[y0:y1, x0:x1].astype(np.float32)

            # Tighten the geometry mask with the real ink: only paint where the
            # object's shape AND actual knockout ink coincide (small tolerance
            # for anti-aliased edges). This avoids over-coverage of background.
            _INK_THR = 8.0
            ink = src.reshape(-1, 4).max(axis=1).reshape(src.shape[0], src.shape[1])
            ink_mask = (ink > _INK_THR).astype(np.float32)
            cov = cov * ink_mask

            masked = src[cov > 0.5]
            if masked.size:
                obj_ink = masked.reshape(-1, 4).max(axis=0)
            else:
                obj_ink = np.zeros(4, dtype=np.float32)

            fg = np.zeros(4, dtype=np.float32)
            for ch in range(4):
                fc_v = float(fc[ch]) * 255.0 if (op_fill and fc and len(fc) >= 4) else 0.0
                sc_v = float(sc[ch]) * 255.0 if (op_stroke and sc and len(sc) >= 4) else 0.0
                fg[ch] = max(fc_v, sc_v)

            # Correlation is trustworthy when the parsed color actually matches
            # the ink present in the knockout at this geometry. Tolerate small
            # ICC/anti-alias rounding; gross mismatches (e.g. black vs orange)
            # indicate a drifted index, in which case we fall back to the true
            # knockout ink so the object stays at its correct position (no jump).
            _TOL = 70.0
            if obj_ink.max() < 5.0:
                # No real ink here in the knockout: never invent phantom ink,
                # just leave the region as-is (restored from knockout later).
                fg = np.zeros(4, dtype=np.float32)
            else:
                mismatch = any(abs(fg[ch] - obj_ink[ch]) > _TOL
                              for ch in range(4))
                if mismatch:
                    fg = obj_ink.copy()

            for ch in range(4):
                if fg[ch] <= 0:
                    continue
                np.maximum(region[:, :, ch], fg[ch] * cov,
                           out=region[:, :, ch])

        elif op['type'] == 'xobject':
            # Image / Form XObject placement. It occupies drawing slot(s) in
            # get_cdrawings() but is not painted here (raster content is
            # restored by the final max with the knockout render). Advance the
            # drawing index so the path<->drawing correlation stays aligned.
            # Images map to a single drawing; Form XObjects are expanded by
            # get_cdrawings() into their own path drawings -- for those we
            # consume one slot here and rely on their paths being parsed
            # separately (see _parse_content_sequence).
            if drawing_idx < len(drawings):
                drawing_idx += 1
            continue

        elif op['type'] == 'text':
            fc = op.get('fill_color')
            if not fc or len(fc) < 4:
                continue

            x_px = int(round(op.get('x', 0) * zoom))
            y_bu = op.get('y', 0)
            y_px = int(round((page_height - y_bu) * zoom))
            fs = op.get('font_size', 12) * zoom

            tx0 = max(0, x_px)
            tx1 = min(w, int(x_px + max(fs * 3, 1)))
            ty0 = max(0, int(y_px - fs * 1.2))
            ty1 = min(h, int(y_px + fs * 0.3))

            if tx0 >= tx1 or ty0 >= ty1:
                continue

            region = result[ty0:ty1, tx0:tx1]
            # Use the real knockout-rendered glyphs (correct geometry) as the
            # ink layer instead of filling the bounding box -- otherwise text
            # would appear as a solid rectangle. For overprint the glyph ink is
            # added (max) on top of whatever was already composited below; for
            # knockout it simply replaces. The final global max with cmyk_arr
            # below restores any glyphs that fell outside this loose bbox.
            src = cmyk_arr[ty0:ty1, tx0:tx1].astype(np.float32)
            np.maximum(region, src, out=region)

    result = np.clip(result, 0, 255).astype(np.float32)
    # The content-stream parser only handles vector/type3 text and Device*
    # colors; it skips images, ICCBased/spot colors and patterns. To avoid
    # losing that content (which would otherwise show up as a blank/black box),
    # keep the maximum ink from the real knockout render for every pixel.
    result = np.maximum(result, cmyk_arr.astype(np.float32))
    if active_channels is not None:
        for i, name in enumerate(('cyan', 'magenta', 'yellow', 'black')):
            if not active_channels.get(name, True):
                result[:, :, i] = 0.0
    return np.clip(result, 0, 255).astype(np.uint8)


# --- Overprint position cache for fast cursor lookup ---

_OP_CACHE = {}

_OP_MAP_CACHE = {}


def clear_overprint_cache():
    _OP_MAP_CACHE.clear()
    _OP_CACHE.clear()
    _MASK_CACHE.clear()


def get_page_overprint(doc, page):
    """Return (has_overprint, fill_count, stroke_count) for a page.

    This is a robust, geometry-independent answer to "does this page use
    overprint?" It is based purely on the content stream: it tracks the
    current ExtGState (via ``gs`` operators) and counts every painted
    operation whose overprint fill (``/OP true``) or stroke (``/op true``)
    flag is active. Unlike ``build_overprint_position_map`` it does NOT depend
    on 1:1 correlation with ``get_cdrawings()``, so it does not miss
    overprint that is defined but only applies to, e.g., strokes.

    Use this for page-level overprint indicators (e.g. the Color Profiles
    sidebar). Use ``build_overprint_position_map`` for per-position cursor
    lookups."""
    key = (id(doc), page.xref)
    if key in _OP_CACHE:
        return _OP_CACHE[key]

    ops = _parse_content_sequence(doc, page)
    if not ops:
        res = (False, 0, 0)
        _OP_CACHE[key] = res
        return res

    fill = 0
    stroke = 0
    for o in ops:
        if o.get('overprint_fill'):
            fill += 1
        if o.get('overprint_stroke'):
            stroke += 1
    res = (bool(fill or stroke), fill, stroke)
    _OP_CACHE[key] = res
    return res


def build_overprint_position_map(doc, page):
    """Build a list of {bbox, op_fill, op_stroke} entries for overprint objects.

    Correlates content stream operations with get_cdrawings() (paths) and
    get_text('rawdict') (text) to produce bounding boxes with overprint flags.
    The bbox is in PyMuPDF top-down coordinates (same as get_text / get_cdrawings).
    """
    key = (id(doc), page.xref)
    if key in _OP_MAP_CACHE:
        return _OP_MAP_CACHE[key]

    operations = _parse_content_sequence(doc, page)
    if not operations:
        _OP_MAP_CACHE[key] = []
        return []

    try:
        # Keep only drawings that actually paint (fill/stroke color present);
        # clip-only paths have both None and must not consume a paint-operation
        # slot in the parser (otherwise the parser<->drawing index correlation
        # drifts and overprint flags get attributed to the wrong geometry).
        raw_drawings = page.get_cdrawings()
        drawings = [d for d in raw_drawings
                    if d.get('fill') is not None or d.get('stroke') is not None]
    except Exception:
        drawings = []
    try:
        td = page.get_text("rawdict")
        text_spans = []
        for block in td.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text_spans.append(span)
    except Exception:
        text_spans = []

    result = []
    drawing_idx = 0
    text_idx = 0

    for op in operations:
        if op['type'].startswith('path'):
            if drawing_idx >= len(drawings):
                drawing_idx += 1
                continue
            d = drawings[drawing_idx]
            drawing_idx += 1

            r = d.get('rect')
            if r is None:
                continue
            if hasattr(r, 'x0'):
                bbox = (r.x0, r.y0, r.x1, r.y1)
            else:
                bbox = (r[0], r[1], r[2], r[3])

            op_fill = bool(op.get('overprint_fill', False))
            op_stroke = bool(op.get('overprint_stroke', False))

            # Only the flag matching the operation type makes the object
            # genuinely overprint at that position: a fill op overprints on /OP,
            # a stroke op on /op. A filled path that merely carries a stroke
            # overprint flag is not overprinting its fill, so it must not be
            # reported here (this also keeps it out of cursor hover results).
            if op['type'] == 'path_fill':
                is_op = op_fill
            elif op['type'] == 'path_stroke':
                is_op = op_stroke
            else:
                is_op = op_fill or op_stroke
            if is_op:
                # Filter out entries with bbox covering >50% of page area
                # (e.g. combined trim marks or registration mark groups)
                bw = bbox[2] - bbox[0]
                bh = bbox[3] - bbox[1]
                pw = page.rect.width
                ph = page.rect.height
                if bw * bh > 0.5 * pw * ph:
                    continue
                result.append({
                    'bbox': bbox,
                    'op_fill': op_fill,
                    'op_stroke': op_stroke,
                })

        elif op['type'] == 'text':
            op_fill = op.get('overprint_fill', False)
            op_stroke = op.get('overprint_stroke', False)
            if not (op_fill or op_stroke):
                continue
            if text_idx < len(text_spans):
                span = text_spans[text_idx]
                text_idx += 1
                result.append({
                    'bbox': tuple(span['bbox']),
                    'op_fill': op_fill,
                    'op_stroke': op_stroke,
                })

    _OP_MAP_CACHE[key] = result
    return result


def check_overprint_at(doc, page, pdf_x, pdf_y):
    """Check if a PDF position is on an overprint object.

    Returns dict: {'overprint': False} or {'overprint': True, 'fill': bool, 'stroke': bool}
    The fill/stroke flags reflect the CURRENT ExtGState at that position,
    not filtered by operation type (so the UI can decide what to show)."""
    op_map = build_overprint_position_map(doc, page)
    for entry in op_map:
        bbox = entry['bbox']
        if bbox[0] <= pdf_x <= bbox[2] and bbox[1] <= pdf_y <= bbox[3]:
            return {
                'overprint': True,
                'fill': entry['op_fill'],
                'stroke': entry['op_stroke'],
            }
    return {'overprint': False}

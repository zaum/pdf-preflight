"""PDF content stream parser — extracts exact source color values from PDF
operators, bypassing MuPDF's render pipeline entirely.

This is essential for print preflight: when the PDF says "0 0 0 1 k" (DeviceCMYK
pure black), the color picker MUST show C=0 M=0 Y=0 K=100, NOT the rendered
pixel values which may have been converted through RGB→CMYK color management.

Handles:
  - DeviceGray   (g, G operators)
  - DeviceRGB    (rg, RG operators)
  - DeviceCMYK   (k, K operators)
  - ICCBased     (sc, SC with /cs name, scn, SCN)
  - Separation   (sc, SC with /cs name)
  - Patterns      (scn, SCN with /Pattern)
  - Text showing  (Tj, TJ, ', ")
  - Path painting (f, F, S, B, b, B*, b*, s, n)
  - Form XObjects (Do) — recurses into their content streams
  - Graphics state push/pop (q, Q)
"""

import re
import fitz


def _to_str(data):
    """Convert bytes content stream to str for tokenization."""
    if isinstance(data, bytes):
        return data.decode('latin-1')
    return data


def _tokenize(data):
    """Tokenize a PDF content stream into a list of tokens.

    Accepts bytes or str. Returns a list where each token is one of:
      - int/float (numbers)
      - str (names like '/F1', operators like 'Tj', strings like '(Hello)')
      - '[' or ']' (array markers)
    """
    data = _to_str(data)
    tokens = []
    i = 0
    n = len(data)

    while i < n:
        c = data[i]

        # Whitespace
        if c in ' \t\n\r\f':
            i += 1
            continue

        # Comment
        if c == '%':
            while i < n and data[i] not in '\n\r':
                i += 1
            continue

        # Array markers
        if c == '[':
            tokens.append('[')
            i += 1
            continue
        if c == ']':
            tokens.append(']')
            i += 1
            continue

        # Number (integer or real, possibly signed)
        if c in '+-.' or c.isdigit():
            start = i
            i += 1
            while i < n and (data[i].isdigit() or data[i] == '.'):
                i += 1
            raw = data[start:i]
            try:
                if '.' in raw:
                    tokens.append(float(raw))
                else:
                    tokens.append(int(raw))
            except ValueError:
                tokens.append(raw)
            continue

        # Name (starts with /)
        if c == '/':
            start = i
            i += 1
            while i < n and data[i] not in ' \t\n\r\f()<>[]{}/%':
                i += 1
            tokens.append(data[start:i])
            continue

        # Literal string (in parentheses)
        if c == '(':
            depth = 1
            i += 1
            parts = []
            start = i
            while i < n and depth > 0:
                ch = data[i]
                if ch == '(':
                    parts.append(data[start:i])
                    depth += 1
                    i += 1
                    start = i
                elif ch == ')':
                    parts.append(data[start:i])
                    depth -= 1
                    i += 1
                    start = i
                elif ch == '\\':
                    parts.append(data[start:i])
                    i += 1
                    if i < n:
                        next_ch = data[i]
                        if next_ch == 'n':
                            parts.append('\n')
                        elif next_ch == 'r':
                            parts.append('\r')
                        elif next_ch == 't':
                            parts.append('\t')
                        elif next_ch in '\\()':
                            parts.append(next_ch)
                        elif next_ch.isdigit():
                            octal = next_ch
                            i += 1
                            while i < n and len(octal) < 3 and data[i].isdigit():
                                octal += data[i]
                                i += 1
                            i -= 1
                            try:
                                parts.append(chr(int(octal, 8)))
                            except ValueError:
                                pass
                        else:
                            parts.append('\\' + next_ch)
                    i += 1
                    start = i
                else:
                    i += 1
            if depth == 0:
                if start < i - 1:
                    parts.append(data[start:i - 1])
            else:
                parts.append(data[start:i])
            tokens.append('(' + ''.join(parts) + ')')
            continue

        # Hex string or dict start
        if c == '<':
            if i + 1 < n and data[i + 1] == '<':
                depth = 1
                i += 2
                while i < n and depth > 0:
                    if i + 1 < n and data[i:i + 2] == '<<':
                        depth += 1
                        i += 2
                    elif i + 1 < n and data[i:i + 2] == '>>':
                        depth -= 1
                        i += 2
                    else:
                        i += 1
                continue
            else:
                i += 1
                start = i
                while i < n and data[i] != '>':
                    i += 1
                tokens.append('<' + data[start:i] + '>')
                i += 1
                continue

        # Operator or keyword (alphabetic, *, ', ")
        if c.isalpha() or c in '*"\'_':
            start = i
            i += 1
            while i < n and (data[i].isalpha() or data[i] in '*"\'_.0123456789'):
                i += 1
            tokens.append(data[start:i])
            continue

        # Unknown — skip
        i += 1

    return tokens


def _extract_colors_from_stream(tokens, doc, page_height, recorded):
    """Walk through tokenized content stream, track graphics/text state,
    and record color information for text and paths.

    Args:
        tokens: list from _tokenize()
        doc: fitz.Document (needed for XObject recursion)
        page_height: page height in points (for Y-flip)
        recorded: list to append (cs_type, color_values, approx_y) tuples to

    Returns the updated recorded list.
    """
    # Graphics state stack
    gs = {
        'fill_cs': 'DeviceGray',
        'fill_color': (0,),
        'stroke_cs': 'DeviceGray',
        'stroke_color': (0,),
        'fill_cs_name': None,
        'stroke_cs_name': None,
    }
    gs_stack = []

    # Text state
    tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # text matrix
    tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # text line matrix
    font_size = 0.0
    leading = 0.0

    # Current path geometry (user-space PDF coords, bottom-left origin)
    path_bbox = None  # (x0, y0, x1, y1)

    def _extend_path(x, y):
        nonlocal path_bbox
        if path_bbox is None:
            path_bbox = [x, y, x, y]
        else:
            if x < path_bbox[0]:
                path_bbox[0] = x
            if y < path_bbox[1]:
                path_bbox[1] = y
            if x > path_bbox[2]:
                path_bbox[2] = x
            if y > path_bbox[3]:
                path_bbox[3] = y

    def _reset_path():
        nonlocal path_bbox
        path_bbox = None

    pos = 0
    n = len(tokens)

    def stack_pop(count):
        nonlocal pos
        result = []
        # Walk backwards through tokens to find the last `count` operands (numeric or name)
        found = 0
        j = pos - 1
        while j >= 0 and found < count:
            tok = tokens[j]
            if isinstance(tok, (int, float)):
                result.insert(0, tok)
                found += 1
            elif isinstance(tok, str) and tok.startswith('/'):
                # Name operands (e.g. /DeviceCMYK, /GS1)
                result.insert(0, tok)
                found += 1
            elif tok == ']':
                depth = 1
                j -= 1
                while j >= 0 and depth > 0:
                    if tokens[j] == ']':
                        depth += 1
                    elif tokens[j] == '[':
                        depth -= 1
                    j -= 1
                continue
            elif tok == '[':
                j -= 1
                continue
            else:
                break
            j -= 1
        return result

    def get_text_y():
        """Return the approximate Y position in PDF coordinates (bottom-left origin)."""
        return tm[5]

    while pos < n:
        tok = tokens[pos]

        # Graphics state push/pop
        if tok == 'q':
            gs_stack.append(dict(gs))
            pos += 1
            continue
        if tok == 'Q':
            if gs_stack:
                gs = gs_stack.pop()
            pos += 1
            continue

        # --- Fill color operators ---

        # DeviceGray: g
        if tok == 'g':
            ops = stack_pop(1)
            if len(ops) == 1:
                gs['fill_cs'] = 'DeviceGray'
                gs['fill_color'] = (ops[0],)
                gs['fill_cs_name'] = None
            pos += 1
            continue

        # DeviceRGB: rg
        if tok == 'rg':
            ops = stack_pop(3)
            if len(ops) == 3:
                gs['fill_cs'] = 'DeviceRGB'
                gs['fill_color'] = tuple(ops)
                gs['fill_cs_name'] = None
            pos += 1
            continue

        # DeviceCMYK: k
        if tok == 'k':
            ops = stack_pop(4)
            if len(ops) == 4:
                gs['fill_cs'] = 'DeviceCMYK'
                gs['fill_color'] = tuple(ops)
                gs['fill_cs_name'] = None
            pos += 1
            continue

        # Set fill color in current colorspace: sc
        if tok == 'sc':
            # Determine number of operands from current colorspace
            if gs['fill_cs'] == 'DeviceGray':
                ops = stack_pop(1)
            elif gs['fill_cs'] == 'DeviceRGB':
                ops = stack_pop(3)
            elif gs['fill_cs'] == 'DeviceCMYK':
                ops = stack_pop(4)
            else:
                ops = stack_pop(1)  # best guess
            if ops:
                gs['fill_color'] = tuple(ops)
            pos += 1
            continue

        # Pattern / Separation / extended fill: scn
        if tok == 'scn':
            # scn can take a pattern name or a colorspace name followed by operands
            # If the last operand is a name token before the numeric values,
            # the numeric values are the color values.
            # For simplicity, try to pop the right number of values.
            if gs['fill_cs'] == 'DeviceGray':
                ops = stack_pop(1)
            elif gs['fill_cs'] == 'DeviceRGB':
                ops = stack_pop(3)
            elif gs['fill_cs'] == 'DeviceCMYK':
                ops = stack_pop(4)
            else:
                ops = stack_pop(1)
            if ops:
                gs['fill_color'] = tuple(ops)
            pos += 1
            continue

        # --- Stroke color operators ---

        if tok == 'G':
            ops = stack_pop(1)
            if len(ops) == 1:
                gs['stroke_cs'] = 'DeviceGray'
                gs['stroke_color'] = (ops[0],)
                gs['stroke_cs_name'] = None
            pos += 1
            continue

        if tok == 'RG':
            ops = stack_pop(3)
            if len(ops) == 3:
                gs['stroke_cs'] = 'DeviceRGB'
                gs['stroke_color'] = tuple(ops)
                gs['stroke_cs_name'] = None
            pos += 1
            continue

        if tok == 'K':
            ops = stack_pop(4)
            if len(ops) == 4:
                gs['stroke_cs'] = 'DeviceCMYK'
                gs['stroke_color'] = tuple(ops)
                gs['stroke_cs_name'] = None
            pos += 1
            continue

        if tok == 'SC':
            if gs['stroke_cs'] == 'DeviceGray':
                ops = stack_pop(1)
            elif gs['stroke_cs'] == 'DeviceRGB':
                ops = stack_pop(3)
            elif gs['stroke_cs'] == 'DeviceCMYK':
                ops = stack_pop(4)
            else:
                ops = stack_pop(1)
            if ops:
                gs['stroke_color'] = tuple(ops)
            pos += 1
            continue

        if tok == 'SCN':
            if gs['stroke_cs'] == 'DeviceGray':
                ops = stack_pop(1)
            elif gs['stroke_cs'] == 'DeviceRGB':
                ops = stack_pop(3)
            elif gs['stroke_cs'] == 'DeviceCMYK':
                ops = stack_pop(4)
            else:
                ops = stack_pop(1)
            if ops:
                gs['stroke_color'] = tuple(ops)
            pos += 1
            continue

        # --- Colorspace setting ---

        if tok == 'cs':
            ops = stack_pop(1)
            if ops and isinstance(ops[0], str) and ops[0].startswith('/'):
                name = ops[0]
                gs['fill_cs_name'] = name
                if name == '/DeviceGray':
                    gs['fill_cs'] = 'DeviceGray'
                elif name == '/DeviceRGB':
                    gs['fill_cs'] = 'DeviceRGB'
                elif name == '/DeviceCMYK':
                    gs['fill_cs'] = 'DeviceCMYK'
                else:
                    gs['fill_cs'] = name  # ICCBased, Separation, etc.
            pos += 1
            continue

        if tok == 'CS':
            ops = stack_pop(1)
            if ops and isinstance(ops[0], str) and ops[0].startswith('/'):
                name = ops[0]
                gs['stroke_cs_name'] = name
                if name == '/DeviceGray':
                    gs['stroke_cs'] = 'DeviceGray'
                elif name == '/DeviceRGB':
                    gs['stroke_cs'] = 'DeviceRGB'
                elif name == '/DeviceCMYK':
                    gs['stroke_cs'] = 'DeviceCMYK'
                else:
                    gs['stroke_cs'] = name
            pos += 1
            continue

        # --- Text state operators ---

        if tok == 'BT':
            tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            tlm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            pos += 1
            continue

        if tok == 'ET':
            pos += 1
            continue

        if tok == 'Tm':
            ops = stack_pop(6)
            if len(ops) == 6:
                tm = list(ops)
                tlm = list(ops)
            pos += 1
            continue

        if tok == 'Td':
            ops = stack_pop(2)
            if len(ops) == 2:
                tx, ty = ops
                tlm[4] += tx
                tlm[5] += ty
                tm = list(tlm)
            pos += 1
            continue

        if tok == 'TD':
            ops = stack_pop(2)
            if len(ops) == 2:
                tx, ty = ops
                leading = -ty
                tlm[4] += tx
                tlm[5] += ty
                tm = list(tlm)
            pos += 1
            continue

        if tok == 'T*':
            tlm[4] = tlm[4]
            tlm[5] = tlm[5] - leading
            tm = list(tlm)
            pos += 1
            continue

        if tok == 'Tf':
            ops = stack_pop(2)
            if len(ops) == 2:
                font_size = float(ops[1]) if isinstance(ops[1], (int, float)) else 0.0
            pos += 1
            continue

        # --- Text showing operators ---

        if tok == 'Tj':
            y = get_text_y()
            recorded.append({
                'type': 'text',
                'fill_cs': gs['fill_cs'],
                'fill_color': gs['fill_color'],
                'stroke_cs': gs['stroke_cs'],
                'stroke_color': gs['stroke_color'],
                'y_pdf': y,
            })
            # Advance text position by glyph widths (approximate)
            tm[4] += font_size * 0.5  # rough advance for Tj
            pos += 1
            continue

        if tok == 'TJ':
            y = get_text_y()
            recorded.append({
                'type': 'text',
                'fill_cs': gs['fill_cs'],
                'fill_color': gs['fill_color'],
                'stroke_cs': gs['stroke_cs'],
                'stroke_color': gs['stroke_color'],
                'y_pdf': y,
            })
            tm[4] += font_size * 0.5  # rough advance
            pos += 1
            continue

        if tok == "'":
            # Move to next line and show text
            tlm[4] = tlm[4]
            tlm[5] = tlm[5] - leading
            tm = list(tlm)
            y = get_text_y()
            recorded.append({
                'type': 'text',
                'fill_cs': gs['fill_cs'],
                'fill_color': gs['fill_color'],
                'stroke_cs': gs['stroke_cs'],
                'stroke_color': gs['stroke_color'],
                'y_pdf': y,
            })
            pos += 1
            continue

        if tok == '"':
            ops = stack_pop(3)
            # aw, ac, text — set word spacing, char spacing, move line, show text
            tlm[4] = tlm[4]
            tlm[5] = tlm[5] - leading
            tm = list(tlm)
            y = get_text_y()
            recorded.append({
                'type': 'text',
                'fill_cs': gs['fill_cs'],
                'fill_color': gs['fill_color'],
                'stroke_cs': gs['stroke_cs'],
                'stroke_color': gs['stroke_color'],
                'y_pdf': y,
            })
            pos += 1
            continue

        # --- Path construction operators (track geometry) ---

        if tok == 'm':  # moveto
            ops = stack_pop(2)
            if len(ops) == 2:
                _reset_path()
                _extend_path(float(ops[0]), float(ops[1]))
            pos += 1
            continue

        if tok == 'l':  # lineto
            ops = stack_pop(2)
            if len(ops) == 2:
                _extend_path(float(ops[0]), float(ops[1]))
            pos += 1
            continue

        if tok == 'c':  # cubic bezier
            ops = stack_pop(6)
            if len(ops) == 6:
                _extend_path(float(ops[0]), float(ops[1]))
                _extend_path(float(ops[2]), float(ops[3]))
                _extend_path(float(ops[4]), float(ops[5]))
            pos += 1
            continue

        if tok == 'v':  # bezier with implicit first control point
            ops = stack_pop(4)
            if len(ops) == 4:
                _extend_path(float(ops[0]), float(ops[1]))
                _extend_path(float(ops[2]), float(ops[3]))
            pos += 1
            continue

        if tok == 'y':  # bezier with implicit last control point
            ops = stack_pop(4)
            if len(ops) == 4:
                _extend_path(float(ops[0]), float(ops[1]))
                _extend_path(float(ops[2]), float(ops[3]))
            pos += 1
            continue

        if tok == 'h':  # closepath
            pos += 1
            continue

        if tok == 're':  # rectangle
            ops = stack_pop(4)
            if len(ops) == 4:
                x, y, w, h = float(ops[0]), float(ops[1]), float(ops[2]), float(ops[3])
                _reset_path()
                _extend_path(x, y)
                _extend_path(x + w, y + h)
            pos += 1
            continue

        # --- Path painting operators ---
        # For these, we record the current fill/stroke color at the approximate
        # path position. We don't track path geometry, so we use a rough estimate.

        if tok in ('f', 'F', 'f*', 'B', 'B*', 'b', 'b*', 's', 'S', 'n'):
            recorded.append({
                'type': 'path_paint',
                'fill_cs': gs['fill_cs'] if tok not in ('S', 's') else None,
                'fill_color': gs['fill_color'] if tok not in ('S', 's') else None,
                'stroke_cs': gs['stroke_cs'] if tok not in ('f', 'F', 'f*') else None,
                'stroke_color': gs['stroke_color'] if tok not in ('f', 'F', 'f*') else None,
                'bbox': tuple(path_bbox) if path_bbox else None,
            })
            _reset_path()
            pos += 1
            continue

        # --- Form XObjects ---
        if tok == 'Do':
            ops = stack_pop(1)
            if ops and isinstance(ops[0], str) and ops[0].startswith('/'):
                xobj_name = ops[0]
                if doc:
                    _recurse_xobject(doc, xobj_name, gs, doc[self._current_page_num].rect.height if hasattr(doc, '__page') else page_height, recorded)
            pos += 1
            continue

        # Default: skip
        pos += 1

    return recorded


def _recurse_xobject(doc, xobj_name, parent_gs, page_height, recorded):
    """Recurse into a Form XObject's content stream."""
    try:
        # Find the XObject in resources
        for xri in range(1, doc.xref_length()):
            obj = doc.xref_object(xri)
            if f'{xobj_name}' in obj and '/Subtype' in obj and '/Form' in obj:
                stream = doc.xref_stream(xri)
                if stream:
                    tokens = _tokenize(stream)
                    gs = dict(parent_gs)
                    gs_stack_backup = []
                    _extract_colors_from_stream(tokens, doc, page_height, recorded)
                break
    except Exception:
        pass


class PageColorExtractor:
    """Extract exact source color values for all text elements on a PDF page
    by parsing the content stream."""

    def __init__(self, doc):
        self.doc = doc

    def extract_page_colors(self, page_num):
        """Parse the page's content streams and return a list of color records.

        Each record is a dict:
          {'type': 'text'|'path_paint',
           'fill_cs': 'DeviceCMYK'|'DeviceRGB'|'DeviceGray'|...,
           'fill_color': (c,m,y,k) or (r,g,b) or (gray,),
           'stroke_cs': ...,
           'stroke_color': ...,
           'y_pdf': float or None}
        """
        page = self.doc[page_num]
        page_height = page.rect.height

        recorded = []
        xrefs = page.get_contents()
        if not xrefs:
            return recorded

        for xri in xrefs:
            stream = self.doc.xref_stream(xri)
            if not stream:
                continue
            tokens = _tokenize(stream)
            _extract_colors_from_stream(tokens, self.doc, page_height, recorded)

        return recorded


def find_text_color_at(recorded_colors, page, pdf_x, pdf_y):
    """Given the recorded colors and a page, find the source color
    at a specific PDF coordinate.

    Uses page.get_text('rawdict') for accurate text position detection,
    then matches to recorded colors by text index (ordered) with
    coordinate fallback.

    Returns:
        dict with keys: found, colorspace, fill_color, stroke_color,
                        type, source_text, or {'found': False}
    """
    try:
        from preview.pdf_inspector import _get_text_dict
        td = _get_text_dict(page)
    except Exception:
        return {'found': False}
    if td is None:
        return {'found': False}

    page_height = page.rect.height

    # Collect all text spans
    all_spans = []
    for block in td.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                all_spans.append(span)

    # Find which span contains the click position
    # PyMuPDF bbox is top-down: x left→right, y top→down
    target_span_idx = None
    for idx, span in enumerate(all_spans):
        bbox = span["bbox"]
        if bbox[0] <= pdf_x <= bbox[2] and bbox[1] <= pdf_y <= bbox[3]:
            target_span_idx = idx
            break

    if target_span_idx is None:
        return {'found': False}

    target_span = all_spans[target_span_idx]

    # Collect only text-type recorded colors
    text_records = [r for r in recorded_colors if r.get('type') == 'text']

    # Strategy 1: Match by index (spans and text records are typically in same order)
    if target_span_idx < len(text_records):
        best_match = text_records[target_span_idx]
    else:
        # Strategy 2: Match by Y proximity
        # Convert bottom-up Tm[5] to top-down for comparison with bbox
        span_y_base = target_span['bbox'][1]  # top-down y0 (closest to baseline)
        # Also try origin
        origin_y = target_span.get('origin', (0, 0))[1]  # top-down baseline

        best_match = None
        best_dist = float('inf')
        for rec in text_records:
            if rec.get('y_pdf') is None:
                continue
            # Convert bottom-up Tm[5] to top-down
            rec_y_td = page_height - rec['y_pdf']
            # Use origin for matching (exact baseline)
            dist = abs(rec_y_td - origin_y) if origin_y else abs(rec_y_td - span_y_base)
            if dist < best_dist:
                best_dist = dist
                best_match = rec

        # Accept only if within reasonable distance
        font_size = target_span.get('size', 12)
        if best_match is None or best_dist > font_size * 2.5:
            return {'found': False}

    fill_cs = best_match.get('fill_cs', 'DeviceGray')
    fill_color = best_match.get('fill_color', (0,))

    return {
        'found': True,
        'type': 'text',
        'colorspace': fill_cs,
        'fill_color': fill_color,
        'stroke_color': best_match.get('stroke_color'),
        'stroke_cs': best_match.get('stroke_cs'),
        'source_text': target_span.get('text', ''),
        'font': target_span.get('font', ''),
        'size': target_span.get('size', 0),
    }


def _is_cmyk_space(cs):
    """True if the colorspace is a CMYK-based space (DeviceCMYK or ICCBased)."""
    if cs is None:
        return False
    if cs == 'DeviceCMYK':
        return True
    # ICCBased / Separation / DeviceN CMYK colorspaces are stored by name
    return isinstance(cs, str) and cs not in ('DeviceRGB', 'DeviceGray')


def find_color_at(recorded_colors, page, pdf_x, pdf_y):
    """Find the EXACT source color at a PDF coordinate, covering both text
    and vector/filled path elements.

    Returns a dict with the same keys as find_text_color_at, or
    {'found': False} if no source color can be resolved.
    """
    # 1) Text match (exact, high priority)
    text_result = find_text_color_at(recorded_colors, page, pdf_x, pdf_y)
    if text_result.get('found'):
        return text_result

    # 2) Path / fill match by geometry (content-stream tracked bbox)
    path_records = [r for r in recorded_colors
                    if r.get('type') == 'path_paint' and r.get('bbox')]

    best = None
    best_area = float('inf')
    for rec in path_records:
        b = rec['bbox']
        if b[0] <= pdf_x <= b[2] and b[1] <= pdf_y <= b[3]:
            area = (b[2] - b[0]) * (b[3] - b[1])
            # Prefer the smallest (innermost / topmost-painted) region
            if area < best_area:
                best_area = area
                best = rec

    if best is not None:
        fill_cs = best.get('fill_cs')
        stroke_cs = best.get('stroke_cs')
        # For prepress, prefer the CMYK-based source color (fill or stroke)
        if _is_cmyk_space(fill_cs):
            return {
                'found': True,
                'type': 'path',
                'colorspace': fill_cs,
                'fill_color': best.get('fill_color'),
                'stroke_color': best.get('stroke_color'),
                'stroke_cs': stroke_cs,
                'bbox': best.get('bbox'),
            }
        if _is_cmyk_space(stroke_cs):
            return {
                'found': True,
                'type': 'path',
                'colorspace': stroke_cs,
                'fill_color': best.get('stroke_color'),
                'stroke_color': best.get('stroke_color'),
                'stroke_cs': stroke_cs,
                'bbox': best.get('bbox'),
            }
        if fill_cs is not None and best.get('fill_color') is not None:
            # Non-CMYK source (e.g. DeviceRGB) — still return exact source
            return {
                'found': True,
                'type': 'path',
                'colorspace': fill_cs,
                'fill_color': best.get('fill_color'),
                'stroke_color': best.get('stroke_color'),
                'stroke_cs': stroke_cs,
                'bbox': best.get('bbox'),
            }

    return {'found': False}

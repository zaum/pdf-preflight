"""Step-by-step ExtGState trace for uj.pdf content stream."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import fitz
from preview.overprint import _get_extgstate_overprints
from preview.content_stream import _tokenize

doc = fitz.open(r'I:\PDF preflight\uj.pdf')
page = doc[0]

op_gs = _get_extgstate_overprints(doc, page)
print("Overprint GS names:", op_gs)

gs = {'overprint_fill': False, 'overprint_stroke': False}
gs_stack = []
op_count = 0

for xri in page.get_contents():
    stream = doc.xref_stream(xri)
    if not stream:
        continue
    tokens = _tokenize(stream)
    i = 0
    n = len(tokens)

    while i < n:
        tok = tokens[i]
        if tok == 'q':
            gs_stack.append(dict(gs))
            i += 1; continue
        if tok == 'Q':
            if gs_stack:
                gs = gs_stack.pop()
                print(f"  Q -> restored fill_op={gs['overprint_fill']} stroke_op={gs['overprint_stroke']}")
            i += 1; continue
        if tok == 'gs':
            # Find the name operand before 'gs'
            j = i - 1
            name = None
            while j >= 0:
                t = tokens[j]
                if isinstance(t, str) and t.startswith('/'):
                    name = t[1:]
                    break
                j -= 1
            if name:
                old_fill, old_stroke = gs['overprint_fill'], gs['overprint_stroke']
                if name in op_gs:
                    gs['overprint_fill'], gs['overprint_stroke'] = op_gs[name]
                else:
                    gs['overprint_fill'] = gs['overprint_stroke'] = False
                if old_fill != gs['overprint_fill'] or old_stroke != gs['overprint_stroke']:
                    print(f"  /{name} gs -> fill_op={gs['overprint_fill']} stroke_op={gs['overprint_stroke']}")
            i += 1; continue

        if tok in ('k', 'rg', 'g', 'K', 'RG', 'G'):
            i += 1; continue

        if tok in ('f', 'F', 'f*', 'S', 's', 'B', 'B*', 'b', 'b*', 'n'):
            if op_count < 25 or gs['overprint_fill'] or gs['overprint_stroke']:
                # Get color for context
                color_info = ""
                j = i - 1
                while j >= 0:
                    t = tokens[j]
                    if isinstance(t, str) and t == 'k':
                        # Get CMYK values
                        vals = []
                        jj = j - 1
                        while jj >= 0 and len(vals) < 4:
                            tt = tokens[jj]
                            if isinstance(tt, (int, float)):
                                vals.insert(0, tt)
                            else:
                                break
                            jj -= 1
                        if len(vals) == 4:
                            color_info = f" cmyk=({vals[0]:.1f},{vals[1]:.1f},{vals[2]:.1f},{vals[3]:.1f})"
                        break
                    if isinstance(t, (int, float)):
                        j -= 1
                        continue
                    break
                print(f"  PAINT[{op_count}] {tok}{color_info} fill_op={gs['overprint_fill']} stroke_op={gs['overprint_stroke']}")
            op_count += 1
            i += 1; continue

        i += 1

doc.close()

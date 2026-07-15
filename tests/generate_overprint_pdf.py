import fitz


def create_overprint_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.set_artbox(fitz.Rect(20, 20, 575, 822))
    page.set_bleedbox(fitz.Rect(10, 10, 585, 832))
    page.set_trimbox(fitz.Rect(30, 30, 565, 812))

    # Normal (non-overprint) objects
    page.draw_rect(fitz.Rect(50, 50, 250, 250), color=(1, 0, 0), fill=(1, 0, 0))
    page.draw_circle(fitz.Point(400, 200), 80, color=(0, 0, 1), fill=(0, 0, 1))

    page.insert_text(fitz.Point(60, 280), "Normal RGB objects", fontsize=12, color=(0, 0, 0))

    page.draw_rect(fitz.Rect(50, 450, 250, 550), color=(0, 0, 0), fill=None, width=3)
    page.insert_text(fitz.Point(60, 570), "Black stroke, no fill", fontsize=10, color=(0, 0, 0))

    doc.save(path)
    doc.close()

    # Now reopen and inject overprint ExtGState
    doc = fitz.open(path)

    # Create an ExtGState object with overprint flags
    # xref for new ExtGState
    op_gs_xref = doc.get_new_xref()
    doc.xref_set_key(op_gs_xref, "", "<< /Type /ExtGState /OP true /op true /OPM 1 >>")

    # Modify page resources to include the ExtGState
    page = doc[0]
    page_xref = page.xref

    # Get current page dict
    page_dict = doc.xref_object(page_xref)

    # Add /Resources with /ExtGState if not present
    if '/Resources' not in page_dict:
        # Need to add resources
        doc.xref_set_key(page_xref, "Resources",
                         f"<< /ExtGState << /OP1 {op_gs_xref} 0 R >> >>")

        # Inject gs operator into content stream
        content = page.read_contents()
        if content:
            new_content = b"q\n/OP1 gs\n" + content + b"\nQ\n"
            page.set_contents(new_content)

    doc.save(path, incremental=True, encryption=0)
    doc.close()
    print(f"Overprint test PDF saved: {path}")
    print(f"ExtGState xref: {op_gs_xref}")


if __name__ == "__main__":
    create_overprint_pdf("resources/test_overprint.pdf")

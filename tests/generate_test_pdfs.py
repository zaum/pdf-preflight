import fitz


def create_overprint_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.set_artbox(fitz.Rect(20, 20, 575, 822))
    page.set_bleedbox(fitz.Rect(10, 10, 585, 832))
    page.set_trimbox(fitz.Rect(30, 30, 565, 812))

    page.draw_rect(fitz.Rect(50, 50, 250, 250), color=(1, 0, 0), fill=(1, 0, 0))
    page.draw_circle(fitz.Point(400, 200), 80, color=(0, 0, 1), fill=(0, 0, 1))
    page.insert_text(fitz.Point(60, 280), "Normal objects", fontsize=12, color=(0, 0, 0))
    page.draw_rect(fitz.Rect(50, 450, 250, 550), color=(0, 0, 0), fill=None, width=3)
    page.insert_text(fitz.Point(60, 570), "Black stroke", fontsize=10, color=(0, 0, 0))

    doc.save(path)
    doc.close()

    doc = fitz.open(path)

    op_xref = doc.get_new_xref()
    doc.update_object(op_xref, "<< /Type /ExtGState /OP true /op true /OPM 1 >>")

    page = doc[0]
    pxref = page.xref

    doc.xref_set_key(pxref, "Resources",
                     f"<< /ExtGState << /OP1 {op_xref} 0 R >> >>")

    content = page.read_contents()
    if content:
        new_content = b"q\n/OP1 gs\n" + content + b"\nQ\n"
        content_xref = doc.get_new_xref()
        doc.update_object(content_xref, "<< /Length %d >>" % len(new_content))
        doc.update_stream(content_xref, new_content)
        page.set_contents(content_xref)

    import os
    tmp = path + ".tmp"
    doc.save(tmp, incremental=False)
    doc.close()
    os.replace(tmp, path)
    print(f"Overprint test PDF saved: {path}")


def create_cmyk_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.set_artbox(fitz.Rect(20, 20, 575, 822))
    page.set_bleedbox(fitz.Rect(10, 10, 585, 832))
    page.set_trimbox(fitz.Rect(30, 30, 565, 812))

    page.draw_rect(fitz.Rect(50, 50, 250, 250), color=(1, 0, 0), fill=(1, 0, 0))
    page.draw_rect(fitz.Rect(100, 100, 300, 300), color=(0, 1, 0), fill=(0, 1, 0))
    page.draw_circle(fitz.Point(400, 200), 80, color=(0, 0, 1), fill=(0, 0, 1))
    page.draw_rect(fitz.Rect(50, 400, 250, 500), color=(0, 0, 0), fill=None, width=3)
    page.draw_rect(fitz.Rect(350, 400, 550, 550), color=(1, 1, 0), fill=(1, 1, 0))
    page.insert_text(fitz.Point(60, 270), "RGB test objects", fontsize=12, color=(0, 0, 1))
    page.insert_text(fitz.Point(350, 570), "Yellow + empty", fontsize=10, color=(0, 0, 0))

    doc.save(path)
    doc.close()
    print(f"Standard test PDF saved: {path}")


if __name__ == "__main__":
    create_overprint_pdf("resources/test_overprint.pdf")
    create_cmyk_pdf("resources/test_document.pdf")

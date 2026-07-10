import fitz


def generate_test_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.set_artbox(fitz.Rect(20, 20, 575, 822))
    page.set_bleedbox(fitz.Rect(10, 10, 585, 832))
    page.set_trimbox(fitz.Rect(30, 30, 565, 812))
    page.set_cropbox(fitz.Rect(0, 0, 595, 842))

    red = (1, 0, 0)
    blue = (0, 0, 1)
    green = (0, 1, 0)
    black = (0, 0, 0)
    yellow = (1, 1, 0)

    page.draw_rect(fitz.Rect(50, 50, 250, 250),
                    color=red, fill=red, width=2)
    page.draw_rect(fitz.Rect(100, 100, 300, 300),
                    color=blue, fill=blue, width=2,
                    overlay=True)

    page.draw_circle(fitz.Point(400, 200), 80,
                      color=green, fill=green, width=2)

    page.draw_rect(fitz.Rect(50, 400, 250, 500),
                    color=black, fill=None, width=3)

    page.insert_text(fitz.Point(60, 270),
                      "RGB Red (overprint test)",
                      fontsize=12, color=black)

    page.insert_text(fitz.Point(60, 520),
                      "PDF Preflight Test Page",
                      fontsize=18, color=blue)

    page.draw_rect(fitz.Rect(350, 400, 550, 550),
                    color=yellow, fill=yellow, width=1)

    page.insert_text(fitz.Point(350, 570),
                      "Yellow fill",
                      fontsize=10, color=black)

    doc.save(path)
    doc.close()
    print(f"Test PDF saved: {path}")


if __name__ == "__main__":
    generate_test_pdf("resources/test_document.pdf")

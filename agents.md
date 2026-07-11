This program is a professional PDF print preparation and verification application. Maximum accuracy and quality in displayed values (color, color profile, PDF properties) is important. Adobe Acrobat is the reference application. If necessary, search the internet for information. The program must be very fast and strictly precise. Only use English terms in the code and file names.

## UI Rules

- Every clickable element should have a mouse hover effect (color change, highlighting)
- Crop eye icons and box toggle buttons should have hover effects: on hover, show the same style as the pressed/checked state, but in a more transparent version
- The background color of sidebar CollapsibleBlock blocks (Page Boxes, Color Profiles, etc.) should be set with `setStyleSheet("background-color: ...")` QSS, NOT with QPalette, because qt-material's `apply_stylesheet()` overwrites QPalette
- Sidebar blocks should appear as "islands": the block's background color should be slightly lighter than the sidebar (dock_content) background color, and there should be spacing between blocks where the darker sidebar background is visible
- The block header (CollapsibleBlock header) should also show the block's background color (QToolButton `background: transparent`)
- Dark theme: sidebar `#404040`, blocks `#4e4e4e`
- Light theme: sidebar `#d0d0d0`, blocks `#dddddd`

I have read and understood the above instructions.

## Color Accuracy Rules (Prepress)

- This is a print preparation (prepress) application. Displayed CMYK values MUST reflect the **exact color values stored in the PDF**, NOT values derived from a rendered/ICC-converted/overprint-simulated pixmap.
- For DeviceCMYK and ICCBased CMYK, always show the original operator values (e.g. `sc`/`scn`/`k`) from the content stream. These are what the print shop uses.
- When no exact source color can be resolved at a position (e.g. raster image, anti-aliasing boundary), fall back to the rendered CMYK value but clearly mark it as approximate.
- Hover (mouse move) must show the same exact stored CMYK values as a click, not an approximate rendered value.
- Never convert CMYK ← RGB ← CMYK as a substitute for the true stored CMYK value.
- Reference for correctness: Adobe Acrobat's Output Preview / Object Inspector.

## Running / Testing the Application

- After making a fix or change, **close the currently running instance** of the application (do not leave old instances running).
- Then (re)launch the application, preferably using the `.bet` build/launch script (batch file) found in the project, or otherwise start it via the normal run command for the project.
- Never run multiple instances of the application at the same time while verifying a fix.

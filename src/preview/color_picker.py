class ColorPicker:
    def format_cmyk(self, cmyk_values):
        c, m, y, k = cmyk_values
        return {
            'cyan': c / 2.55,
            'magenta': m / 2.55,
            'yellow': y / 2.55,
            'black': k / 2.55,
        }

    def get_info(self, cmyk_arr, x, y):
        if cmyk_arr is None:
            return None
        if 0 <= y < cmyk_arr.shape[0] and 0 <= x < cmyk_arr.shape[1]:
            return self.format_cmyk(cmyk_arr[y, x])
        return None

    @staticmethod
    def analyze_source(source_info, rendered_cmyk):
        """Analyze source color info and rendered CMYK for discrepancies.

        Args:
            source_info: dict from pdf_inspector.inspect_position[exact]()
            rendered_cmyk: numpy array [C,M,Y,K] 0-255

        Returns dict with:
            source_type, source_color_desc, rich_black, warning
        """
        result = {
            'source_type': None,
            'source_color_desc': '',
            'rich_black': False,
            'warning': '',
        }

        if not source_info or not source_info.get('found'):
            result['source_color_desc'] = '(no object info)'
            return result

        obj_type = source_info.get('type', '')
        result['source_type'] = obj_type

        # --- New format: content stream parser provides exact colorspace and values ---
        cs = source_info.get('colorspace')
        fill_color = source_info.get('fill_color')

        if cs and fill_color:
            # Values from content stream are in 0-1 range (PDF standard)
            def _pct(v):
                return v * 100.0

            def _is_cmyk(c):
                if c == 'DeviceCMYK':
                    return True
                return isinstance(c, str) and c not in ('DeviceRGB', 'DeviceGray')

            if _is_cmyk(cs) and len(fill_color) >= 4:
                c, m, y, k = fill_color[:4]
                label = 'DeviceCMYK' if cs == 'DeviceCMYK' else f'CMYK ({cs})'
                result['source_color_desc'] = (
                    f"{label} C={_pct(c):.0f}% M={_pct(m):.0f}% "
                    f"Y={_pct(y):.0f}% K={_pct(k):.0f}%"
                )
                # Detect rich black: source is CMYK K-only but rendered has C,M,Y
                if (c == 0 and m == 0 and y == 0 and k > 0
                        and int(rendered_cmyk[3]) > 10):
                    rc, rm, ry, rk = (int(rendered_cmyk[0]), int(rendered_cmyk[1]),
                                      int(rendered_cmyk[2]), int(rendered_cmyk[3]))
                    if rc > 3 or rm > 3 or ry > 3:
                        result['rich_black'] = True
                        result['warning'] = (
                            "Warning: source is pure K but rendered CMYK contains "
                            f"C:{rc / 2.55:.0f}% M:{rm / 2.55:.0f}% Y:{ry / 2.55:.0f}%. "
                            "Anti-aliasing or background may affect edge pixels."
                        )

            elif cs == 'DeviceRGB' and len(fill_color) >= 3:
                r, g, b = fill_color[:3]
                result['source_color_desc'] = (
                    f"DeviceRGB R={_pct(r):.0f}% G={_pct(g):.0f}% B={_pct(b):.0f}%"
                )
                # Detect RGB black → rich CMYK
                if r == 0 and g == 0 and b == 0:
                    rc, rm, ry, rk = (int(rendered_cmyk[0]), int(rendered_cmyk[1]),
                                      int(rendered_cmyk[2]), int(rendered_cmyk[3]))
                    if rk > 10 and (rc > 3 or rm > 3 or ry > 3):
                        result['rich_black'] = True
                        result['warning'] = (
                            "Warning: rich black — source is DeviceRGB black but "
                            "CMYK rendering contains C,M,Y. The text is defined in "
                            "DeviceRGB and will print as 4-color, not pure K."
                        )

            elif cs == 'DeviceGray' and len(fill_color) >= 1:
                g = fill_color[0]
                result['source_color_desc'] = (
                    f"DeviceGray {_pct(g):.0f}%"
                )
                if g < 0.04:  # near-black
                    rc, rm, ry, rk = (int(rendered_cmyk[0]), int(rendered_cmyk[1]),
                                      int(rendered_cmyk[2]), int(rendered_cmyk[3]))
                    if rk > 10 and (rc > 3 or rm > 3 or ry > 3):
                        result['rich_black'] = True
                        result['warning'] = (
                            "Warning: source is DeviceGray black but rendered CMYK "
                            "contains C,M,Y."
                        )

            else:
                result['source_color_desc'] = (
                    f"{cs} {' '.join(f'{v:.2f}' for v in fill_color)}"
                )

            return result

        # --- Old format: MuPDF high-level API provides sRGB only ---
        if obj_type == 'text':
            rgb = source_info.get('color_rgb', (0, 0, 0))
            result['source_color_desc'] = (
                f"Text sRGB({rgb[0]},{rgb[1]},{rgb[2]})"
            )

            from preview.pdf_inspector import detect_rich_black
            rb = detect_rich_black(rgb, rendered_cmyk)
            if rb['is_rich_black']:
                result['rich_black'] = True
                result['warning'] = (
                    "Warning: rich black — source is black but CMYK rendering "
                    "contains C,M,Y. The text is likely defined in DeviceRGB."
                )

        elif obj_type == 'path':
            fill = source_info.get('fill_rgb')
            stroke = source_info.get('stroke_rgb')
            if fill:
                result['source_color_desc'] = (
                    f"Fill sRGB({fill[0]},{fill[1]},{fill[2]})"
                )

                from preview.pdf_inspector import detect_rich_black
                rb = detect_rich_black(fill, rendered_cmyk)
                if rb['is_rich_black']:
                    result['rich_black'] = True
                    result['warning'] = (
                        "Warning: rich black — source fill is black but CMYK "
                        "rendering contains C,M,Y."
                    )
            if stroke:
                desc = (
                    f"Stroke sRGB({stroke[0]},{stroke[1]},{stroke[2]})"
                )
                if result['source_color_desc']:
                    result['source_color_desc'] += ' | ' + desc
                else:
                    result['source_color_desc'] = desc

        return result

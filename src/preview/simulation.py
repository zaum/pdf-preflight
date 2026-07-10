import os
import tempfile


class SimulationEngine:
    def __init__(self):
        self._simulation_profile_path = None
        self._profile_cache = {}

    def set_simulation_profile(self, icc_path):
        self._simulation_profile_path = icc_path

    def clear_simulation_profile(self):
        self._simulation_profile_path = None

    def get_active_profile_path(self):
        return self._simulation_profile_path

    def is_active(self):
        return self._simulation_profile_path is not None and os.path.isfile(self._simulation_profile_path)

    def get_profile_info(self, icc_path):
        try:
            with open(icc_path, 'rb') as f:
                hdr = f.read(128)
            desc = self._parse_icc_description(icc_path)
            cs_sig = hdr[16:20].decode('ascii', errors='replace')
            pcs_sig = hdr[20:24].decode('ascii', errors='replace')
            return {
                'description': desc or os.path.basename(icc_path),
                'color_space': cs_sig.strip(),
                'pcs': pcs_sig.strip(),
                'path': icc_path,
            }
        except Exception:
            return {
                'description': os.path.basename(icc_path),
                'color_space': 'unknown',
                'pcs': 'unknown',
                'path': icc_path,
            }

    def get_embedded_profiles(self, doc):
        if doc is None:
            return []
        profiles = []
        try:
            for xri in range(1, doc.xref_length()):
                try:
                    obj = doc.xref_object(xri)
                    if '/ICCBased' in obj and '/N' in obj:
                        n_str = obj.split('/N')[1].strip().split()[0]
                        try:
                            n = int(n_str)
                        except ValueError:
                            n = 0
                        if n == 4:
                            stream = doc.xref_stream(xri)
                            if stream and len(stream) > 128:
                                desc = _parse_icc_desc_from_bytes(stream)
                                tmp = tempfile.NamedTemporaryFile(
                                    suffix='.icc', delete=False)
                                tmp.write(stream)
                                tmp.close()
                                display = desc if desc else f"Embedded ICC (xref {xri})"
                                profiles.append((display, tmp.name))
                except Exception:
                    continue

            for xri in range(1, doc.xref_length()):
                try:
                    obj = doc.xref_object(xri)
                    if '/DestOutputProfile' in obj:
                        parts = obj.split('/DestOutputProfile')
                        ref = parts[1].strip().split()[0] if len(parts) > 1 else ''
                        if ref.isdigit():
                            stream = doc.xref_stream(int(ref))
                            if stream and len(stream) > 128:
                                desc = _parse_icc_desc_from_bytes(stream)
                                tmp = tempfile.NamedTemporaryFile(
                                    suffix='.icc', delete=False)
                                tmp.write(stream)
                                tmp.close()
                                display = desc if desc else f"Output Intent Profile (xref {ref})"
                                if not any(p[1] == tmp.name for p in profiles):
                                    profiles.append((display, tmp.name))
                except Exception:
                    continue
        except Exception:
            pass
        return profiles

    def get_system_profiles(self):
        profiles = []
        paths = []
        windir = os.environ.get('WINDIR', '')
        if windir:
            paths.append(os.path.join(windir, r'System32\spool\drivers\color'))
        paths.extend([
            '/Library/ColorSync/Profiles',
            '/usr/share/color/icc',
        ])
        icc_dir = os.environ.get('ICC_PROFILE_DIR', '')
        if icc_dir and os.path.isdir(icc_dir):
            paths.insert(0, icc_dir)
        seen = set()
        for d in paths:
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if f.lower().endswith('.icc') or f.lower().endswith('.icm'):
                    full = os.path.join(d, f)
                    if full not in seen:
                        seen.add(full)
                        profiles.append((f, full))
        return profiles

    def _parse_icc_description(self, icc_path):
        try:
            with open(icc_path, 'rb') as f:
                data = f.read()
            return _parse_icc_desc_from_bytes(data)
        except Exception:
            return None


def _parse_icc_desc_from_bytes(data):
    try:
        if len(data) < 132:
            return None
        tag_count = int.from_bytes(data[128:132], 'big')
        pos = 132
        for _ in range(tag_count):
            if pos + 12 > len(data):
                break
            sig = data[pos:pos+4]
            offset = int.from_bytes(data[pos+4:pos+8], 'big')
            size = int.from_bytes(data[pos+8:pos+12], 'big')
            pos += 12
            if sig == b'desc' and offset + size <= len(data):
                chunk = data[offset:offset+size]
                if len(chunk) >= 12:
                    str_len = int.from_bytes(chunk[8:12], 'big')
                    if str_len > 0 and 8 + str_len <= len(chunk):
                        return chunk[8:8+str_len].decode('ascii', errors='replace').strip('\x00')
        return None
    except Exception:
        return None

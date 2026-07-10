import numpy as np


_CHANNEL_NAMES = ['cyan', 'magenta', 'yellow', 'black']


class SeparationPreview:
    def separate(self, cmyk_arr):
        if cmyk_arr is None:
            return None
        return {name: cmyk_arr[:, :, i]
                for i, name in enumerate(_CHANNEL_NAMES)}

    def composite(self, cmyk_arr, active_channels):
        """
        Adobe-style separation composite.
        - ALL on: return unmodified CMYK.
        - ONE on: show that channel's coverage as inverted grayscale
          (0% = white, 100% = black) — simulates how Adobe shows a single plate.
        - Multiple on: return CMYK with only active channels (others zeroed).
        - None on: return zero-filled CMYK.
        """
        if cmyk_arr is None:
            return None

        active = {n: active_channels.get(n, True) for n in _CHANNEL_NAMES}
        count_on = sum(1 for v in active.values() if v)

        if count_on == 4:
            return cmyk_arr.astype(np.uint8)

        if count_on == 0:
            return np.zeros_like(cmyk_arr)

        if count_on == 1:
            # Show the active plate in its correct ink channel.
            # C→index 0, M→1, Y→2, K→3.  All other channels stay 0 so the
            # ICC conversion renders the correct ink hue (cyan tones for C,
            # magenta for M, etc.) on a white-paper background.
            result = np.zeros((*cmyk_arr.shape[:2], 4), dtype=np.uint8)
            for i, name in enumerate(_CHANNEL_NAMES):
                if active[name]:
                    result[:, :, i] = cmyk_arr[:, :, i]
                    break
            return result

        # 2-3 channels: zero the inactive ones
        result = cmyk_arr.astype(np.float32)
        for i, name in enumerate(_CHANNEL_NAMES):
            if not active[name]:
                result[:, :, i] = 0
        return result.astype(np.uint8)

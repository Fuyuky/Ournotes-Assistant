"""Focus evidence from the two native SDK input borders, outside their text."""
import numpy as np


def focused(image, field):
    borders = {'email': (350, 409), 'code': (425, 484)}
    pixels = np.asarray(image.convert('RGB'))

    def highlighted(name):
        return all(((strip[:, :, 0] > 230) & (strip[:, :, 1] < 165)
                    & (strip[:, :, 2] > 95) & (strip[:, :, 2] < 220)).mean(axis=1).max() > .9
                   for y in borders[name] for strip in [pixels[y-1:y+2, 660:840]])

    return highlighted(field) and not highlighted('code' if field == 'email' else 'email')

"""테스트용: 이미 칠한 밑색 PSD에서 영역마다 점 하나씩 찍은 '참고' 폴더를 만들어 원본 PSD에 넣는다.

작가가 영역마다 점을 찍은 상황을 흉내 낸다.
usage: python tools/make_sample_hints.py <원본.psd> <밑색 결과.psd> <저장할.psd>
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from psd_tools import PSDImage
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psdpaint import psdio  # noqa: E402
from psdpaint.core import Options, segment  # noqa: E402

DOT_R = 6
MIN_AREA = 50  # 점을 찍기 어려운 아주 작은 영역은 건너뛴다


def main(src, painted, dst):
    psd = PSDImage.open(src)
    done = PSDImage.open(painted)
    _, _, labels, n = segment(psd, Options())
    depth = ndimage.distance_transform_edt(labels > 0)

    flat = psdio.find(done, "밑색", True)
    hint_group = psdio.new_group(psd, "참고")
    yy, xx = np.ogrid[-DOT_R:DOT_R + 1, -DOT_R:DOT_R + 1]
    disk = xx ** 2 + yy ** 2 <= DOT_R ** 2
    for layer in flat:  # 아래 레이어부터 넣어 원래 순서를 지킨다
        rgba = psdio.canvas_rgba(layer, done)
        covered = rgba[..., 3] > 0
        ids = np.unique(labels[covered & (labels > 0)])
        dots = np.zeros_like(rgba)
        for i in ids:
            region = labels == i
            m = region & covered
            # 선 아래로 넓힌 칠이 살짝 걸친 영역(배경 등)에는 찍지 않는다
            if m.sum() < MIN_AREA or m.sum() < 0.5 * region.sum():
                continue
            y, x = np.unravel_index(np.argmax(np.where(m, depth, 0)), m.shape)
            sl = (slice(y - DOT_R, y + DOT_R + 1), slice(x - DOT_R, x + DOT_R + 1))
            dots[sl][disk] = rgba[y, x]
        ys, xs = np.nonzero(dots[..., 3])
        if not ys.size:
            continue
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        psdio.new_pixel_layer(hint_group, Image.fromarray(dots[y0:y1, x0:x1], "RGBA"),
                              layer.name, int(y0), int(x0))
        print(layer.name, "점", len(ids))
    psd.save(dst)


if __name__ == "__main__":
    main(*sys.argv[1:4])

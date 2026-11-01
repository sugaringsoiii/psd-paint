"""선화 PSD에 참고 레이어 힌트대로 밑색 레이어를 만든다."""
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from psd_tools import PSDImage
from scipy import ndimage

from . import psdio


@dataclass
class Options:
    line_group: str = "선화"            # 선화 레이어가 든 폴더 이름
    hint_group: str = "참고"            # 힌트 폴더(또는 레이어) 이름
    extra_boundary: list = field(default_factory=lambda: ["컷*", "*테두리*"])  # 경계로 쓸 픽셀 레이어 이름 패턴
    output_group: str = "밑색"
    gap_close_px: int = 2       # 선을 이만큼 두껍게 해서 작은 틈을 막는다
    small_area: int = 400       # 힌트 없는 이보다 작은 영역은 이웃 영역을 따른다
    min_hint_px: int = 5        # 영역 안 힌트 픽셀이 이보다 적으면 무시한다 (선 넘은 낙서 끝 방지)
    under_line_px: int = 9      # 선 아래로 밑색을 넓히는 거리
    pocket_layers: list = field(default_factory=lambda: ["머리"])  # 잔머리 틈을 채울 레이어 이름
    pocket_gap_px: int = 6      # 잔머리 틈을 막을 때 선을 넓히는 거리
    pocket_max: int = 6000      # 이보다 넓게 갇힌 배경은 채우지 않는다
    offset_px: int = 2          # 밑색 바깥 윤곽을 안쪽으로 줄이는 거리


@dataclass
class Result:
    out_path: Path
    layers: list
    regions: int
    hinted: int
    unhinted_large: int
    preview: Image.Image = None  # 선화 + 밑색 축소 이미지


class PaintError(Exception):
    pass


def _disk(r):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def read_hints(psd, opts):
    """힌트 레이어 → (힌트 키 맵, 키별 (레이어 이름, RGB), 레이어 이름 순서(위→아래)).

    폴더면 하위 레이어 이름이 밑색 레이어 이름이 된다. 레이어 하나면 색마다 레이어를 나눈다.
    """
    hint = psdio.find(psd, opts.hint_group)
    if hint is None:
        raise PaintError(f"'{opts.hint_group}' 힌트 폴더나 레이어가 없습니다")
    if hint.is_group():
        sources = [(layer.name, layer) for layer in hint if not layer.is_group()]
    else:
        sources = [(None, hint)]
    if not sources:
        raise PaintError(f"'{opts.hint_group}' 폴더 안에 힌트 레이어가 없습니다")

    key_map = np.full((psd.height, psd.width), -1, np.int32)
    keys = []
    for name, layer in sources:  # 아래 레이어부터 칠해 위 레이어가 덮는다
        rgba = psdio.canvas_rgba(layer, psd)
        solid = rgba[..., 3] >= 250
        if not solid.any():
            continue
        packed = (rgba[..., 0].astype(np.int32) << 16) | (rgba[..., 1].astype(np.int32) << 8) | rgba[..., 2]
        colors, inverse = np.unique(packed[solid], return_inverse=True)
        base = len(keys)
        for c in colors:
            rgb = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
            keys.append((name or f"색_{c:06X}", rgb))
        key_map[solid] = base + inverse
    names_top_down = list(dict.fromkeys(n for n, _ in reversed(keys)))
    return key_map, keys, names_top_down


def segment(psd, opts):
    line_group = psdio.find(psd, opts.line_group, True)
    if line_group is None:
        raise PaintError(f"'{opts.line_group}' 선화 폴더가 없습니다")
    line = psdio.group_alpha(line_group, psd) > 80
    boundary = line.copy()
    for layer in psdio.find_pixel_layers(psd, opts.extra_boundary):
        # 숨긴 레이어와 캔버스 절반 이상을 덮는 레이어(컷 배경 등)는 경계가 아니다
        if not layer.is_visible():
            continue
        a = psdio.canvas_rgba(layer, psd)[..., 3] > 80
        if a.mean() < 0.5:
            boundary |= a
    closed = cv2.dilate(boundary.astype(np.uint8), _disk(1), iterations=opts.gap_close_px) > 0
    labels, n = ndimage.label(~closed)
    return line_group, boundary, labels, n


def assign(labels, n, key_map, opts):
    """영역마다 가장 많이 칠해진 힌트 키. 힌트가 없으면 -1."""
    region_key = np.full(n + 1, -1, np.int64)
    m = (key_map >= 0) & (labels > 0)
    if m.any():
        k_count = int(key_map.max()) + 1
        pairs = labels[m].astype(np.int64) * k_count + key_map[m]
        uniq, counts = np.unique(pairs, return_counts=True)
        lab, key = uniq // k_count, uniq % k_count
        order = np.lexsort((-counts, lab))
        lab, key, counts = lab[order], key[order], counts[order]
        first = np.r_[True, lab[1:] != lab[:-1]]
        ok = first & (counts >= opts.min_hint_px)
        region_key[lab[ok]] = key[ok]
    hinted = int((region_key[1:] >= 0).sum())

    # 힌트 없는 작은 영역은 둘레 12px 안에서 가장 많이 맞닿은 힌트 영역을 따른다
    areas = np.bincount(labels.ravel(), minlength=n + 1)
    boxes = ndimage.find_objects(labels)
    for i in range(1, n + 1):
        if region_key[i] >= 0 or areas[i] >= opts.small_area or boxes[i - 1] is None:
            continue
        sl = tuple(slice(max(s.start - 12, 0), s.stop + 12) for s in boxes[i - 1])
        mine = labels[sl] == i
        ring = ndimage.binary_dilation(mine, iterations=12) & ~mine
        neigh = labels[sl][ring]
        neigh = neigh[(neigh > 0) & (region_key[neigh] >= 0)]
        if neigh.size:
            region_key[i] = region_key[np.bincount(neigh).argmax()]
    unhinted_large = int(((region_key < 0) & (areas >= opts.small_area))[1:].sum())
    return region_key, hinted, unhinted_large


def fill(labels, boundary, region_key, keys, opts):
    """영역 키를 픽셀로 펼치고 선 아래 넓히기·잔머리 틈 채우기·윤곽 줄이기를 한다. 0 = 빈칸, k+1 = 키 k."""
    cls = (region_key + 1).astype(np.int32)[labels]
    cls[labels == 0] = 0

    # 선(경계) 아래로 밑색을 넓혀 빈틈을 없앤다
    dist, (iy, ix) = ndimage.distance_transform_edt(cls == 0, return_indices=True)
    grow = (labels == 0) & (dist <= opts.under_line_px)
    cls[grow] = cls[iy[grow], ix[grow]]

    # 잔머리 끝은 선이 열려 배경으로 분류된다. 선을 더 두껍게 했을 때 큰 배경과 끊기는
    # 작은 틈 중 대상 레이어에 맞닿은 것만 채운다 (바깥 윤곽 밖은 큰 배경에 이어져 있어 칠하지 않는다)
    pocket_ks = {k + 1 for k, (name, _) in enumerate(keys) if name in opts.pocket_layers}
    if pocket_ks:
        bg = cls == 0
        thick = cv2.dilate(boundary.astype(np.uint8), _disk(opts.pocket_gap_px)) > 0
        pockets, _ = ndimage.label(bg & ~thick)
        area = np.bincount(pockets.ravel())
        walk = opts.pocket_gap_px + 2
        # 큰 배경에서 같은 거리만큼 넓힌 곳은 틈 입구 바깥이므로 채우지 않는다
        open_bg = np.isin(pockets, np.nonzero(area > opts.pocket_max)[0]) & (pockets > 0)
        open_bg = ndimage.binary_dilation(open_bg, iterations=walk, mask=bg & ~boundary)
        for i, sl in enumerate(ndimage.find_objects(pockets), 1):
            if sl is None or area[i] > opts.pocket_max:
                continue
            sl = tuple(slice(max(s.start - walk, 0), s.stop + walk) for s in sl)
            reach = ndimage.binary_dilation(pockets[sl] == i, iterations=walk, mask=bg[sl] & ~boundary[sl])
            ring = ndimage.binary_dilation(reach, iterations=3) & ~reach
            neigh = cls[sl][ring & ~bg[sl]]
            if not neigh.size:
                continue
            top = np.bincount(neigh).argmax()
            if top in pocket_ks and (neigh == top).sum() > 0.5 * neigh.size:
                cls[sl][reach & bg[sl] & ~open_bg[sl]] = top

    # 밑색 전체의 바깥 윤곽만 안쪽으로 줄인다 (부위끼리 맞닿은 경계는 그대로)
    if opts.offset_px:
        cls[cv2.erode((cls > 0).astype(np.uint8), _disk(opts.offset_px)) == 0] = 0
    return cls


def write(psd, line_group, cls, keys, names_top_down, opts, out_path):
    parent = line_group.parent
    group = psdio.new_group(parent, opts.output_group)
    lut_rgb = np.array([(0, 0, 0)] + [rgb for _, rgb in keys], np.uint8)
    key_names = np.array([None] + [name for name, _ in keys], object)
    written = []
    for name in reversed(names_top_down):  # 아래 레이어부터 넣는다
        ks = np.nonzero(key_names == name)[0]
        m = np.isin(cls, ks)
        if not m.any():
            continue
        ys, xs = np.nonzero(m)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        sub = m[y0:y1, x0:x1]
        rgba = np.zeros(sub.shape + (4,), np.uint8)
        rgba[..., :3] = lut_rgb[cls[y0:y1, x0:x1]]
        rgba[..., 3] = sub * 255
        rgba[~sub, :3] = 0
        psdio.new_pixel_layer(group, Image.fromarray(rgba, "RGBA"), name, int(y0), int(x0))
        written.append(name)
    # 밑색 폴더를 선화 폴더 바로 아래로
    parent.remove(group)
    parent.insert(parent.index(line_group), group)
    hint = psdio.find(psd, opts.hint_group)
    if hint is not None:
        hint.visible = False
    psd.save(out_path)
    return list(reversed(written))


def make_preview(cls, boundary, keys, width=480):
    lut = np.array([(255, 255, 255)] + [rgb for _, rgb in keys], np.uint8)
    rgb = lut[cls]
    rgb[boundary] = 0
    im = Image.fromarray(rgb)
    return im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)


def paint(psd_path, out_path, opts=None, progress=None):
    """psd_path에 힌트대로 밑색을 넣어 out_path로 저장한다. progress(단계 설명, 0~1)를 부른다."""
    opts = opts or Options()
    say = progress or (lambda msg, frac: None)
    say("PSD 여는 중", 0.0)
    psd = PSDImage.open(psd_path)
    say("선화 영역 나누는 중", 0.15)
    line_group, boundary, labels, n = segment(psd, opts)
    say("참고 레이어 읽는 중", 0.35)
    key_map, keys, names = read_hints(psd, opts)
    say("영역에 힌트 대응하는 중", 0.45)
    region_key, hinted, unhinted_large = assign(labels, n, key_map, opts)
    say("밑색 채우는 중", 0.6)
    cls = fill(labels, boundary, region_key, keys, opts)
    say("PSD 저장 중", 0.85)
    layers = write(psd, line_group, cls, keys, names, opts, out_path)
    preview = make_preview(cls, boundary, keys)
    say("완료", 1.0)
    return Result(Path(out_path), layers, n, hinted, unhinted_large, preview)

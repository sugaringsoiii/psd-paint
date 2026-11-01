"""PSD 레이어 읽기·쓰기 도우미."""
import fnmatch

import numpy as np
from psd_tools.api.layers import Group, PixelLayer
from psd_tools.constants import Tag


def find(group, name, want_group=None):
    """이름이 같은 첫 레이어를 하위 폴더까지 찾는다. want_group으로 폴더/픽셀 레이어를 가린다."""
    for layer in group:
        if layer.name == name and (want_group is None or layer.is_group() == want_group):
            return layer
        if layer.is_group():
            found = find(layer, name, want_group)
            if found is not None:
                return found
    return None


def find_pixel_layers(group, patterns):
    """이름이 패턴(* 와일드카드)에 맞는 픽셀 레이어 전부. 폴더는 제외한다."""
    out = []
    for layer in group.descendants():
        if not layer.is_group() and any(fnmatch.fnmatch(layer.name, p) for p in patterns):
            out.append(layer)
    return out


def canvas_rgba(layer, psd):
    """레이어 픽셀을 캔버스 크기 RGBA 배열로. 보이기 설정과 무관하게 레이어 자체 픽셀을 쓴다."""
    h, w = psd.height, psd.width
    out = np.zeros((h, w, 4), np.uint8)
    im = layer.topil()
    if im is None:
        return out
    im = np.asarray(im.convert("RGBA"))
    x0, y0 = layer.left, layer.top
    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    cw = min(w - dx0, im.shape[1] - sx0)
    ch = min(h - dy0, im.shape[0] - sy0)
    if cw > 0 and ch > 0:
        out[dy0:dy0 + ch, dx0:dx0 + cw] = im[sy0:sy0 + ch, sx0:sx0 + cw]
    return out


def group_alpha(group, psd):
    """폴더 안에서 보이는 레이어를 합친 알파 (선화 폴더용)."""
    im = group.composite(viewport=psd.viewbox, force=True)
    return np.asarray(im)[..., 3]


def new_pixel_layer(parent, image, name, top, left):
    """투명도를 레이어 자체의 투명 채널에 담은 픽셀 레이어 (레이어 마스크 없음).

    psd-tools는 알파 채널이 없는 RGB PSD에 RGBA 이미지를 넣으면 알파를 버리고
    레이어 마스크로만 옮긴다. 레이어 투명 채널은 PSD 채널 수와 무관하게 쓸 수 있으므로,
    만드는 동안만 PSD 모드를 RGBA로 보이게 해 알파를 투명 채널에 쓴다.
    """
    psd_cls = type(parent._psd)
    original = psd_cls.pil_mode
    psd_cls.pil_mode = property(lambda self: "RGBA")
    try:
        layer = PixelLayer.frompil(image, parent=parent, name=name, top=top, left=left)
    finally:
        psd_cls.pil_mode = original
    set_name(layer, name)
    return layer


def new_group(parent, name):
    group = Group.new(parent=parent, name=name)
    set_name(group, name)
    return group


def set_name(layer, name):
    """PSD의 레거시 이름 칸은 macroman으로 쓰여 한글을 담지 못한다. 클립스튜디오 레이어와 같게
    이 칸에는 cp949 바이트를, 유니코드 이름 블록(luni)에는 원래 이름을 넣는다."""
    layer._record.tagged_blocks.set_data(Tag.UNICODE_LAYER_NAME, name)
    layer._record.name = name.encode("cp949", errors="replace")[:255].decode("mac_roman")

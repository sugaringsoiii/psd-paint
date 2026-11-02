"""색상 견본 파일 읽기(포토샵 .aco·Adobe Swatch Exchange .ase·클립스튜디오 컬러 세트 .cls)와 쓰기(.aco·.ase)."""
import colorsys
import struct
from pathlib import Path


def read_palette(path):
    """→ [(이름, (r, g, b))]. 이름이 없는 색은 '파일이름 n'. 모르는 형식이면 ValueError

    이름 없는 색은 같은 색이 앞에 있으면 뺀다 (클립스튜디오 세트의 빈 칸은 같은 회색으로 수십 개 채워져 있다).
    """
    path = Path(path)
    data = path.read_bytes()
    try:
        if data[:4] == b"ASEF":
            colors = _ase(data)
        elif data[:4] == b"SLCC":
            colors = _cls(data)
        elif path.suffix.lower() == ".aco":
            colors = _aco(data)
        else:
            raise ValueError("포토샵 색상 견본(.aco·.ase)이나 클립스튜디오 컬러 세트(.cls)만 읽을 수 있습니다.")
    except (struct.error, UnicodeDecodeError):  # 중간에 끊기거나 깨진 파일
        raise ValueError("파일이 손상됐거나 형식이 다릅니다.") from None
    seen, out = set(), []
    for name, rgb in colors:
        if name or rgb not in seen:
            out.append((name or f"{path.stem} {len(out) + 1}", rgb))
        seen.add(rgb)
    return out


def _rgb(space, v):
    """ACO 색 공간 값(16비트 4개) → RGB. 모르는 색 공간이면 None"""
    w, x, y, z = v
    if space == 0:  # RGB
        return tuple(round(c / 65535 * 255) for c in (w, x, y))
    if space == 1:  # HSB
        return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(w / 65536, x / 65535, y / 65535))
    if space == 2:  # CMYK (0 이 진한 색)
        c, m, ye, k = (1 - n / 65535 for n in v)
        return tuple(round(255 * (1 - a) * (1 - k)) for a in (c, m, ye))
    if space == 8:  # 회색 0~10000 (10000 이 검정)
        g = round(255 * (1 - w / 10000))
        return g, g, g
    return None  # Lab 등은 건너뛴다


def _aco(data):
    # 1판(이름 없음) 뒤에 2판(이름 있음)이 붙어 있다. 2판이 있으면 그것을 쓴다. 그 뒤 8BIM 묶음 정보는 무시
    version, count = struct.unpack_from(">HH", data, 0)
    pos = 4 + count * 10 if version == 1 else 0
    if version == 1 and len(data) < pos + 4:
        return [(None, rgb) for rgb in (_rgb(*_aco_color(data, 4 + i * 10)) for i in range(count)) if rgb]
    version, count = struct.unpack_from(">HH", data, pos)
    if version != 2:
        raise ValueError("색상 견본 파일 형식을 알 수 없습니다")
    pos += 4
    out = []
    for _ in range(count):
        space, v = _aco_color(data, pos)
        (n,) = struct.unpack_from(">I", data, pos + 10)
        name = data[pos + 14:pos + 14 + n * 2].decode("utf-16-be").rstrip("\0")
        pos += 14 + n * 2
        rgb = _rgb(space, v)
        if rgb:
            out.append((name, rgb))
    return out


def _aco_color(data, pos):
    space, *v = struct.unpack_from(">HHHHH", data, pos)
    return space, v


def _ase(data):
    (count,) = struct.unpack_from(">I", data, 8)
    pos, out = 12, []
    for _ in range(count):
        kind, size = struct.unpack_from(">HI", data, pos)
        body = pos + 6
        pos = body + size
        if kind != 1:  # 0xC001·0xC002 는 그룹 시작·끝
            continue
        (n,) = struct.unpack_from(">H", data, body)
        name = data[body + 2:body + 2 + n * 2].decode("utf-16-be").rstrip("\0")
        at = body + 2 + n * 2
        model = data[at:at + 4]
        vals = struct.unpack_from(">" + "f" * {b"RGB ": 3, b"CMYK": 4, b"Gray": 1}.get(model, 0), data, at + 4)
        if model == b"RGB ":
            rgb = tuple(round(c * 255) for c in vals)
        elif model == b"CMYK":
            c, m, y, k = vals
            rgb = tuple(round(255 * (1 - a) * (1 - k)) for a in (c, m, y))
        elif model == b"Gray":
            rgb = (round(vals[0] * 255),) * 3
        else:
            continue  # LAB 은 건너뛴다
        out.append((name, rgb))
    return out


def _cls(data):
    """클립스튜디오 컬러 세트. 리틀 엔디언: 'SLCC', 판(2), 머리 길이(4)+머리(세트 이름),
    4, 색 수(4), 남은 길이(4), 그다음 색마다 길이(4) + RGBA(4) + 이름 있음(4) [+ 이름 길이(2) + UTF-16 이름]."""
    (head,) = struct.unpack_from("<I", data, 6)
    pos = 10 + head
    _, count, _ = struct.unpack_from("<III", data, pos)
    pos += 12
    out = []
    for _ in range(count):
        (size,) = struct.unpack_from("<I", data, pos)
        entry = data[pos + 4:pos + 4 + size]
        pos += 4 + size
        r, g, b, _a, has_name = struct.unpack_from("<BBBBI", entry, 0)
        name = ""
        if has_name:
            (n,) = struct.unpack_from("<H", entry, 8)
            name = entry[10:10 + n].decode("utf-16-le")
        out.append((name, (r, g, b)))
    return out


if __name__ == "__main__":  # 자체 점검: 만든 바이트를 다시 읽어 본다
    import tempfile

    def utf16(s):
        return (s + "\0").encode("utf-16-be")
    aco = struct.pack(">HH", 1, 1) + struct.pack(">5H", 0, 65535, 0, 0, 0)
    aco += struct.pack(">HH", 2, 1) + struct.pack(">5H", 0, 65535, 0, 0, 0) + struct.pack(">I", 3) + utf16("빨강")
    name = utf16("파랑")
    block = struct.pack(">H", len(name) // 2) + name + b"RGB " + struct.pack(">3f", 0, 0, 1) + struct.pack(">H", 2)
    ase = b"ASEF" + struct.pack(">HHI", 1, 0, 1) + struct.pack(">HI", 1, len(block)) + block
    title = "세트".encode("utf-8")
    head = struct.pack("<H", len(title)) + title
    named = bytes([0, 128, 0, 255]) + struct.pack("<I", 1) + struct.pack("<H", 4) + "초록".encode("utf-16-le")
    blank = bytes([91, 91, 91, 255]) + struct.pack("<I", 0)
    entries = [named, blank, blank]  # 빈 칸 회색은 한 번만 들어가야 한다
    cls = b"SLCC" + struct.pack("<HI", 1, len(head)) + head + struct.pack("<III", 4, len(entries), 0)
    cls += b"".join(struct.pack("<I", len(e)) + e for e in entries)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.aco").write_bytes(aco)
        (Path(d) / "b.ase").write_bytes(ase)
        assert read_palette(Path(d) / "a.aco") == [("빨강", (255, 0, 0))]
        assert read_palette(Path(d) / "b.ase") == [("파랑", (0, 0, 255))]
        (Path(d) / "c.cls").write_bytes(cls)
        assert read_palette(Path(d) / "c.cls") == [("초록", (0, 128, 0)), ("c 2", (91, 91, 91))]
    print("ok")


# ---- 내보내기 ----
def write_aco(path, colors):
    """포토샵 색상 견본 .aco (1판 + 이름 있는 2판). colors: [(이름, (r, g, b))]"""
    def block(version):
        out = struct.pack(">HH", version, len(colors))
        for name, (r, g, b) in colors:
            out += struct.pack(">HHHHH", 0, r * 257, g * 257, b * 257, 0)
            if version == 2:
                text = name.encode("utf-16-be") + b"\0\0"
                out += struct.pack(">I", len(text) // 2) + text
        return out
    Path(path).write_bytes(block(1) + block(2))


def write_ase(path, colors):
    """Adobe Swatch Exchange .ase (RGB 전역 색). colors: [(이름, (r, g, b))]"""
    out = b"ASEF" + struct.pack(">HHI", 1, 0, len(colors))
    for name, rgb in colors:
        text = name.encode("utf-16-be") + b"\0\0"
        body = struct.pack(">H", len(text) // 2) + text + b"RGB " + struct.pack(">fff", *(c / 255 for c in rgb))
        body += struct.pack(">H", 2)  # 2 = 일반 색
        out += struct.pack(">HI", 1, len(body)) + body
    Path(path).write_bytes(out)

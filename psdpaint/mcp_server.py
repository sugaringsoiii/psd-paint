"""psd-paint MCP 서버: Claude 데스크톱·Claude Code 같은 MCP 클라이언트에서 밑색을 칠한다.

다른 페이지 레퍼런스는 API 키 없이 클라이언트의 AI(구독)가 직접 영역을 맞춘다.
paint_psd가 번호 지도와 레퍼런스 이미지를 돌려주면, AI가 영역마다 팔레트를 골라 apply_match로 넘긴다.

실행: python -m psdpaint.mcp_server   (stdio)
"""
import io
import json
import threading
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from .cli import output_path
from .core import AwaitingAnswer, Options, PaintError, paint

server = MCPServer(
    name="psd-paint",
    instructions=(
        "선화 PSD에 밑색 레이어를 자동으로 깔아 주는 도구다. "
        "paint_psd로 칠한다. 레퍼런스가 다른 페이지면 paint_psd가 레퍼런스(P번호 팔레트)와 원고 번호 지도 이미지를 돌려준다. "
        "그때는 이미지를 보고 먼저 인물마다 자세와 인체 구조를 따라 부위(머리카락·피부·겉옷 …)별 번호를 나눈 뒤, "
        "레퍼런스에서 같은 인물의 부위별 팔레트를 골라 apply_match(people=…)를 호출한다. "
        "판단이 서지 않는 영역과 배경은 none으로 둔다. 엑스트라도 피부는 피부 팔레트를 고른다. "
        "apply_match가 돌려준 칠한 결과 지도를 보고 틀린 곳이 있으면 한 번 고쳐 다시 호출한다."
    ),
)


def _png(im):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG", optimize=True)
    return Image(data=buf.getvalue(), format="png")


def _summary(result):
    lines = [f"저장: {result.out_path}", f"밑색 레이어: {', '.join(result.layers)}"]
    if result.auto:
        lines.append(f"자동(회색)으로 칠한 영역: {result.auto}개")
    if result.merged:
        lines.append(f"'기타'로 합친 레이어: {', '.join(result.merged)}")
    return "\n".join(lines)


def _job_folder(out_path):
    out_path = Path(out_path)
    return out_path.with_name(out_path.stem + "_AI매칭")


def _run(psd_path, out_path, opts):
    """칠하기를 실행한다. 다른 페이지 레퍼런스면 매칭 요청(이미지·지시문)을 돌려준다."""
    try:
        result = paint(psd_path, out_path, opts)
    except AwaitingAnswer:
        folder = _job_folder(out_path)
        (folder / "job.json").write_text(json.dumps(
            {"psd_path": str(psd_path), "out_path": str(out_path), "options": asdict(opts)}, ensure_ascii=False))
        readme = (folder / "README.md").read_text()
        prompt = readme.split("\n\n", 2)[2].split("답은 schema.json")[0].strip()
        out = [f"다른 페이지 레퍼런스라 AI 매칭이 필요합니다. 매칭 작업: {folder}\n"
               "아래 이미지를 보고 원고 번호마다 팔레트를 골라 apply_match(job=위 경로, assignments=[...]) 를 호출하세요.\n"]
        for p in sorted(folder.glob("ref_*.png")):
            out += [f"[레퍼런스 {p.stem}]", Image(path=p)]
        for p in sorted(folder.glob("map_*.png")):
            out += [f"[원고 지도 {p.stem}]", Image(path=p)]
        out.append(prompt)
        return out
    except PaintError as e:
        return f"실패: {e}"
    out = [_summary(result), _png(result.preview)]
    folder = _job_folder(out_path)
    results = sorted(folder.glob("result_*.png")) if folder.exists() else []
    if results:
        out.append("[칠한 결과 지도] 번호마다 칠한 색이다. 분홍색은 none(회색 자동)이다. 레퍼런스와 비교해 "
                   "틀린 번호(인물이 바뀐 색, 회색 피부, 덩어리 안 한 조각만 다른 색)가 있으면 그 번호만 "
                   "apply_match(job, assignments=[...])로 고친다. 고칠 곳이 없으면 끝낸다.")
        out += [Image(path=p) for p in results]
    return out


@server.tool()
def inspect_psd(psd_path: str) -> str:
    """PSD의 레이어 구조(폴더·레이어 이름, 보이기 여부)를 보여 준다. 선화 폴더·참고 폴더 이름을 확인할 때 쓴다."""
    from psd_tools import PSDImage

    psd = PSDImage.open(psd_path)
    lines = [f"{Path(psd_path).name}  {psd.width}×{psd.height}"]

    def walk(group, depth):
        for layer in reversed(list(group)):  # 위 레이어부터
            mark = "📁" if layer.is_group() else "▫️"
            vis = "" if layer.visible else " (숨김)"
            lines.append(f"{'  ' * depth}{mark} {layer.name}{vis}")
            if layer.is_group():
                walk(layer, depth + 1)
    walk(psd, 0)
    return "\n".join(lines)


@server.tool()
def paint_psd(psd_path: str, reference: str = "", output_dir: str = "", line_group: str = "선화",
              hint_group: str = "참고", max_layers: int = 10, offset_px: int = 2, auto_fill: bool = True,
              boundary_layers: str = "컷*, *테두리*"):
    """선화 PSD에 밑색 레이어를 깔아 새 PSD(원본이름_밑색.psd)로 저장한다. 원본은 바꾸지 않는다.

    - reference를 비우면 PSD 안의 '참고' 폴더(점·낙서 힌트)를 쓰고, 힌트가 없는 영역은 회색으로 자동 칠한다.
    - reference에 PNG·JPG·PSD 경로를 주면 그 레퍼런스의 색을 따른다. 다른 페이지(캐릭터 시트 등)면
      레퍼런스·원고 번호 지도 이미지를 돌려주니, 보고 나서 apply_match를 호출한다.
    - boundary_layers: 선화 외에 경계로 쓸 레이어 이름 패턴(쉼표 구분, * 사용).
    """
    opts = Options(line_group=line_group, hint_group=hint_group, max_layers=max_layers, offset_px=offset_px,
                   auto_fill=auto_fill, ai_mode="files",
                   extra_boundary=[p.strip() for p in boundary_layers.split(",") if p.strip()])
    if reference:
        opts.hint_source = "psd" if reference.lower().endswith(".psd") else "image"
        opts.hint_path = reference
    out_path = output_path(psd_path, output_dir or None, "_밑색")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return _run(psd_path, out_path, opts)


@server.tool()
def apply_match(job: str, people: list[dict] | None = None, objects: list[dict] | None = None,
                background: list[int] | None = None, assignments: list[dict] | None = None):
    """paint_psd가 돌려준 매칭 작업에 AI의 답을 적용해 칠한다.

    - job: paint_psd가 알려 준 매칭 작업 폴더 경로
    - people: 원고 인물마다 {"id", "description", "pose", "reference_character",
              "parts": [{"part": 부위 이름, "regions": [번호…], "palette": "P3" 또는 "none"}]}
      같은 인물의 같은 부위는 같은 색으로 칠한다. 피부를 none으로 두면 피부색으로 칠한다.
    - objects: [{"name": 사물 이름, "regions": [번호…]}] (회색으로 자동 칠함), background: 배경 번호 목록 (칠하지 않음)
    - assignments: 결과를 고칠 때만. [{"region": 번호, "palette": "P3" 또는 "none"}] 이전 답에서 이 번호들만 바꾼다
    """
    folder = Path(job)
    info = json.loads((folder / "job.json").read_text())
    answer_file = folder / "answer.json"
    if people is not None:
        answer = {"people": people, "objects": objects or [], "background": background or []}
        if assignments:
            answer["corrections"] = assignments
    elif assignments and answer_file.exists():
        answer = json.loads(answer_file.read_text())
        answer["corrections"] = answer.get("corrections", []) + assignments
    else:
        answer = {"assignments": assignments or []}
    answer_file.write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    opts = Options(**info["options"])
    return _run(info["psd_path"], info["out_path"], opts)


def main():
    # psd-tools·scipy는 import 사슬이 깊다. 도구가 작업 스레드에서 돌 때 스택이 넘치지 않게
    # 스레드 스택을 늘리고 무거운 모듈은 미리 불러 둔다
    threading.stack_size(64 * 1024 * 1024)
    import psd_tools.composite  # noqa: F401
    import scipy.ndimage  # noqa: F401

    server.run("stdio")


if __name__ == "__main__":
    main()

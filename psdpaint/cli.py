"""여러 PSD를 순서대로 처리하는 명령줄 도구.

usage: python -m psdpaint a.psd b.psd ... [-o 출력폴더] [--offset 2]
"""
import argparse
import os
import sys
import time
from pathlib import Path

from .core import AwaitingAnswer, Options, PaintError, paint


def output_path(src, out_dir, suffix):
    src = Path(src)
    return Path(out_dir or src.parent) / f"{src.stem}{suffix}.psd"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="psdpaint", description="선화 PSD에 참고 레이어 힌트대로 밑색 레이어를 만든다")
    ap.add_argument("files", nargs="+", help="처리할 PSD 파일들")
    ap.add_argument("-o", "--out", help="결과 폴더 (기본: 원본과 같은 폴더)")
    ap.add_argument("--suffix", default="_밑색", help="결과 파일 이름 뒤에 붙일 말 (기본: _밑색)")
    ap.add_argument("--line", default=Options.line_group, help="선화 폴더 이름")
    ap.add_argument("--hint", default=Options.hint_group, help="참고(힌트) 폴더 이름")
    ap.add_argument("--offset", type=int, default=Options.offset_px, help="밑색 바깥 윤곽을 안쪽으로 줄이는 px")
    ap.add_argument("--boundary", action="append", default=[], help="선화 외에 경계로 쓸 레이어 이름 패턴 (여러 번 지정 가능)")
    ap.add_argument("--max-layers", type=int, default=Options.max_layers, help="밑색 레이어 수 상한 (0 = 제한 없음)")
    ap.add_argument("--no-auto", action="store_true", help="힌트 없는 영역을 자동으로 칠하지 않는다")
    ap.add_argument("--ref-image", help="레퍼런스 이미지(PNG·JPG). 다른 페이지면 AI 매칭")
    ap.add_argument("--ref-psd", help="레퍼런스 PSD(캐릭터 시트·채색 참고). 다른 페이지면 AI 매칭")
    ap.add_argument("--ai", choices=["off", "api", "files"], default="api",
                    help="다른 페이지 레퍼런스 처리: api(Claude API) · files(요청 폴더 만들기) · off")
    ap.add_argument("--ai-model", default=Options.ai_model)
    ap.add_argument("--no-zoom", action="store_true", help="인물별 확대 검토를 하지 않는다 (요청 1회 절약)")
    ap.add_argument("--no-review", action="store_true", help="AI 매칭 결과를 다시 검수하지 않는다 (요청 1회 절약)")
    ap.add_argument("--gap", type=int, default=Options.gap_close_px, help="선 틈 닫기 px")
    args = ap.parse_args(argv)

    opts = Options(line_group=args.line, hint_group=args.hint, offset_px=args.offset, gap_close_px=args.gap)
    opts.extra_boundary += args.boundary
    opts.max_layers = args.max_layers
    opts.auto_fill = not args.no_auto
    if args.ref_image or args.ref_psd:
        opts.hint_source = "image" if args.ref_image else "psd"
        opts.hint_path = args.ref_image or args.ref_psd
    opts.ai_mode, opts.ai_model, opts.ai_review = args.ai, args.ai_model, not args.no_review
    opts.ai_zoom = not args.no_zoom
    if args.ai == "api":
        from . import ai
        opts.ai_key = os.environ.get("ANTHROPIC_API_KEY") or ai.get_key("claude")
    failed = waiting = 0
    for i, f in enumerate(args.files, 1):
        dst = output_path(f, args.out, args.suffix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{i}/{len(args.files)}] {f}")
        t = time.time()
        try:
            r = paint(f, dst, opts, progress=lambda msg, frac: print(f"    {int(frac * 100):3d}% {msg}"))
        except AwaitingAnswer as e:
            waiting += 1
            print(f"    답 대기: {e}")
            continue
        except (PaintError, OSError) as e:
            failed += 1
            print(f"    실패: {e}")
            continue
        print(f"    → {r.out_path} ({time.time() - t:.1f}초)")
        if r.ai_usage:
            from . import ai
            print(f"    AI 매칭: {ai.describe_usage(r.ai_usage)}")
        print(f"    레이어: {', '.join(r.layers)} / 힌트 받은 영역 {r.hinted}개, 자동 칠한 영역 {r.auto}개")
        if r.merged:
            print(f"    '기타'로 합친 레이어: {', '.join(r.merged)}")
    print(f"완료 {len(args.files) - failed - waiting}개, 답 대기 {waiting}개, 실패 {failed}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

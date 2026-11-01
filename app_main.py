"""설치용 앱의 시작점.

- 인자 없이: 앱 창
- --mcp: MCP 서버 (Claude 데스크톱·Claude Code 커넥터용, stdio)
- --version: 버전 출력
"""
import sys


def main():
    if "--version" in sys.argv:
        from psdpaint import __version__
        if sys.stdout:  # 창 앱으로 빌드하면 Windows에서는 출력 창이 없다
            print(__version__)
        return 0
    if "--mcp" in sys.argv:
        from psdpaint.mcp_server import main as mcp_main
        mcp_main()
        return 0
    from psdpaint.gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())

"""선화 PSD 자동 밑색."""
__version__ = "0.3.0"  # 릴리스할 때 올리고, 같은 번호로 v태그를 푸시한다 (예: v0.1.0)

# HTTPS 인증서는 OS 인증서 저장소(macOS 키체인, Windows 인증서 저장소)로 확인한다.
# python.org 판 Python·패키징한 앱에는 인증서 묶음이 없어 AI 요청이 실패하기 때문이다.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    import os

    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
from .core import AwaitingAnswer, Options, PaintError, Result, paint

__all__ = ["__version__", "AwaitingAnswer", "Options", "PaintError", "Result", "paint"]

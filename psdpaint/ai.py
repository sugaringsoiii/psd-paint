"""AI 연결 설정: 제공자·모델·API 키 저장과 연결 테스트.

키는 OS 자격 증명 저장소(macOS 키체인, Windows 자격 증명 관리자)에 저장한다.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import keyring

SERVICE = "psd-paint"
PROVIDERS = {
    "claude": {"label": "Claude (Anthropic)", "model": "claude-opus-5-5",
               "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    "openai": {"label": "OpenAI (Codex)", "model": "gpt-5", "models": ["gpt-5", "gpt-5-mini"]},
    "gemini": {"label": "Google Gemini", "model": "gemini-2.5-pro", "models": ["gemini-2.5-pro", "gemini-2.5-flash"]},
}
# OpenAI·Gemini 기본 목록은 예시다. 실제로 쓸 수 있는 모델은 [목록 불러오기]로 키에서 받아 온다.

# 100만 토큰당 달러 (입력, 출력). 알고 있는 모델만 적는다. 없는 모델은 토큰 수만 보여 준다.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


def estimate_cost(usage):
    """토큰 사용량 → 예상 비용(달러). 단가를 모르는 모델이면 None."""
    price = PRICES.get(usage.get("model", ""))
    if not price:
        return None
    return (usage.get("input", 0) * price[0] + usage.get("output", 0) * price[1]) / 1_000_000


def describe_usage(usage):
    """로그에 쓸 한 줄: 모델 · 토큰 · 예상 비용 · 검수에서 고친 수."""
    text = f"{usage['model']} · 입력 {usage['input']:,} / 출력 {usage['output']:,} 토큰"
    if usage.get("calls", 1) > 1:
        text += f" (요청 {usage['calls']}회)"
    cost = estimate_cost(usage)
    text += f" · 약 ${cost:.3f}" if cost is not None else " · 단가 미등록(제공자 요금표 참고)"
    if "fixed" in usage:
        text += f" · 검수에서 {usage['fixed']}곳 고침"
    return text


def get_key(provider):
    try:
        return keyring.get_password(SERVICE, provider) or ""
    except keyring.errors.KeyringError:
        return ""


def set_key(provider, key):
    if key:
        keyring.set_password(SERVICE, provider, key)
    else:
        try:
            keyring.delete_password(SERVICE, provider)
        except keyring.errors.PasswordDeleteError:
            pass


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def test_connection(provider, key, model=""):
    """키가 유효한지 모델 정보 조회로 확인한다 (토큰을 쓰지 않는 요청). → (성공 여부, 메시지)"""
    if not key:
        return False, "API 키를 입력하세요"
    try:
        if provider == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=15)
            try:
                if model:
                    m = client.models.retrieve(model)
                    return True, f"연결됨 · {m.display_name}"
                client.models.list(limit=1)
                return True, "연결됨"
            except anthropic.AuthenticationError:
                return False, "API 키가 올바르지 않습니다"
            except anthropic.NotFoundError:
                return False, f"모델을 찾을 수 없습니다: {model}"
            except anthropic.APIConnectionError:
                return False, "네트워크에 연결할 수 없습니다"
            except anthropic.APIStatusError as e:
                return False, f"오류 {e.status_code}: {e.message}"
        if provider == "openai":
            path = f"/v1/models/{urllib.parse.quote(model)}" if model else "/v1/models"
            _get("https://api.openai.com" + path, {"Authorization": f"Bearer {key}"})
            return True, "연결됨" + (f" · {model}" if model else "")
        if provider == "gemini":
            path = f"/v1beta/models/{urllib.parse.quote(model)}" if model else "/v1beta/models?pageSize=1"
            _get("https://generativelanguage.googleapis.com" + path, {"x-goog-api-key": key})
            return True, "연결됨" + (f" · {model}" if model else "")
        return False, f"알 수 없는 제공자: {provider}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "API 키가 올바르지 않습니다"
        if e.code == 404:
            return False, f"모델을 찾을 수 없습니다: {model}"
        if e.code == 400:
            return False, "API 키가 올바르지 않거나 요청이 거부되었습니다"
        return False, f"오류 {e.code}"
    except urllib.error.URLError as e:
        if "CERTIFICATE" in str(e.reason):
            return False, "HTTPS 인증서를 확인하지 못했습니다 (회사 보안 프로그램이나 인증서 설정을 확인하세요)"
        return False, f"네트워크에 연결할 수 없습니다 ({e.reason})"


def list_models(provider, key):
    """키로 쓸 수 있는 모델 이름 목록 (이미지 입력이 되는 생성 모델 위주)."""
    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=15)
        return [m.id for m in client.models.list(limit=100)]
    if provider == "openai":
        data = _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
        ids = [m["id"] for m in data.get("data", [])]
        return sorted((i for i in ids if i.startswith(("gpt-", "o"))), reverse=True)
    if provider == "gemini":
        data = _get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {"x-goog-api-key": key})
        out = []
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in m["name"]:
                out.append(m["name"].removeprefix("models/"))
        return sorted(out, reverse=True)
    return []

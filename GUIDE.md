# psd-paint 사용 가이드

선화 PSD에 밑색 레이어를 자동으로 깔아 주는 도구입니다. 쓰는 방법은 두 가지입니다.

| 방법 | 이런 분께 | 필요한 것 |
|---|---|---|
| **A. 앱으로 쓰기** | 파일 여러 개를 한 번에 칠하고 싶을 때 | 이 저장소 다운로드 (+ 다른 페이지 레퍼런스를 쓰면 AI API 키) |
| **B. MCP로 연결하기** | Claude 데스크톱·Claude Code에게 말로 시키고 싶을 때 | 이 저장소 다운로드 + Claude 구독 (API 키 없이 됨) |

---

## 1. 다운로드와 설치

> macOS `.app` · Windows `.exe` 설치 파일은 준비 중입니다. 지금은 아래처럼 저장소를 받아 설치합니다.

### 필요한 것
- Python 3.11 이상 ([python.org](https://www.python.org/downloads/)에서 설치)
- Git (macOS는 처음 `git`을 실행하면 설치 창이 뜹니다)

### 설치 (macOS)

터미널을 열고 차례로 실행합니다.

```bash
git clone https://github.com/aejinyoo/psd-paint.git ~/psd-paint
```

```bash
cd ~/psd-paint && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 설치 (Windows)

PowerShell에서 실행합니다.

```powershell
git clone https://github.com/aejinyoo/psd-paint.git $HOME\psd-paint
cd $HOME\psd-paint; python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
```

### 업데이트

```bash
cd ~/psd-paint && git pull && .venv/bin/pip install -r requirements.txt
```

---

## 2. 방법 A — 앱으로 쓰기

### 실행

```bash
cd ~/psd-paint && .venv/bin/python -m psdpaint.gui
```

(Windows: `.venv\Scripts\python -m psdpaint.gui`)

### 칠하기
1. PSD 파일이나 폴더를 창에 끌어 놓습니다. 여러 개를 넣으면 순서대로 칠합니다.
2. **설정** 탭에서 참고 방식을 고릅니다.
   - **파일 안 참고 레이어**: PSD에 `참고` 폴더를 만들고, 그 안에 `머리`·`피부`·`재킷`처럼 원하는 레이어 이름으로 하위 레이어를 만든 뒤, 영역마다 원하는 색으로 점을 찍습니다. AI 없이 칠합니다.
   - **레퍼런스 이미지 / 레퍼런스 PSD**: 캐릭터 시트나 앞 회차의 칠한 페이지를 지정합니다. 다른 페이지면 AI가 인물과 부위를 맞춰 칠합니다.
3. **[밑색 칠하기]** 를 누릅니다. 결과는 `원본이름_밑색.psd`로 저장되고, 원본은 바뀌지 않습니다.
4. 완료된 파일을 고르면 미리보기가 나오고, **[파일 열기]** 로 클립스튜디오·포토샵에서 엽니다.

### AI 매칭 설정 (레퍼런스가 다른 페이지일 때)
설정 탭의 **AI 연결**에서 고릅니다.

| AI 매칭 | 설명 |
|---|---|
| API로 바로 매칭 | Claude·OpenAI·Gemini 중 하나의 API 키를 넣습니다. 구독과 별도로 쓴 만큼 요금이 나갑니다 |
| 요청 폴더 만들기 | API 키 없이, Claude Code·Codex에게 폴더를 넘겨 답을 받습니다 |
| 끄기 | 다른 페이지 레퍼런스면 칠하지 않고 알려 줍니다 |

- **[목록 불러오기]** 로 키에서 쓸 수 있는 모델을 받아 고릅니다.
- **검수**를 켜 두면 AI가 칠한 결과를 한 번 더 보고 틀린 곳을 고칩니다(요청 1회 추가).
- 로그에 토큰 수와 예상 비용이 나옵니다. 단가를 아는 모델(Claude)만 비용을 계산하고, 나머지는 토큰 수만 보여 줍니다.
- API 키는 macOS 키체인(Windows는 자격 증명 관리자)에 저장됩니다.

---

## 3. 방법 B — MCP로 연결하기 (Claude 데스크톱·Claude Code)

먼저 **1. 다운로드와 설치**를 마칩니다. 아래 경로의 `/Users/내이름/psd-paint`는 실제로 설치한 경로로 바꿉니다(터미널에서 `cd ~/psd-paint && pwd`로 확인).

### Claude 데스크톱

1. Claude 데스크톱 메뉴 **설정 → 개발자 → 설정 편집**을 누르거나, 아래 파일을 텍스트 편집기로 엽니다.
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. `mcpServers` 안에 `psd-paint`를 추가합니다. 다른 서버가 이미 있으면 그 옆에 쉼표로 이어 붙입니다.

```json
{
  "mcpServers": {
    "psd-paint": {
      "command": "/Users/내이름/psd-paint/.venv/bin/python",
      "args": ["-m", "psdpaint.mcp_server"],
      "env": {"PYTHONPATH": "/Users/내이름/psd-paint"}
    }
  }
}
```

Windows는 `command`를 `C:\\Users\\내이름\\psd-paint\\.venv\\Scripts\\python.exe`처럼 적습니다.

3. Claude 데스크톱을 **완전히 종료(⌘Q)** 하고 다시 엽니다.
4. 새 대화의 도구 목록(🔨)에 **psd-paint**가 보이면 연결된 것입니다.

### Claude Code

```bash
claude mcp add psd-paint -e PYTHONPATH=$HOME/psd-paint -- $HOME/psd-paint/.venv/bin/python -m psdpaint.mcp_server
```

### 쓰는 법

Claude에게 파일 경로와 함께 말합니다.

> "/Users/내이름/Downloads/1화_2.psd 를 /Users/내이름/Downloads/캐릭터시트.psd 레퍼런스로 밑색 칠해 줘"

Claude가 알아서 순서대로 진행합니다.
1. `paint_psd`: 원고를 영역으로 나누고 레퍼런스·번호 지도 이미지를 받아 봅니다.
2. `apply_match`: 번호마다 색을 골라 칠합니다.
3. 칠한 결과 지도를 보고 틀린 곳이 있으면 한 번 고칩니다.
4. 저장한 파일 경로와 미리보기를 알려 줍니다.

레퍼런스 없이 "이 PSD 밑색 칠해 줘"라고 하면 `참고` 폴더 힌트대로 칠하고, 힌트가 없는 영역은 회색으로 칠합니다.

| 도구 | 하는 일 |
|---|---|
| `inspect_psd` | PSD 레이어 구조를 보여 줍니다(선화·참고 폴더 이름 확인) |
| `paint_psd` | 밑색을 칠하거나, 다른 페이지 레퍼런스면 매칭 이미지를 돌려줍니다 |
| `apply_match` | AI가 고른 색을 적용해 칠하고 결과 지도를 돌려줍니다 |

---

## 4. PSD 준비 규칙

- 선화는 `선화` 폴더 안에 둡니다(이름은 설정에서 바꿀 수 있습니다).
- 컷 테두리 레이어 이름이 `컷…` 또는 `…테두리…`면 경계로 씁니다. 폰 같은 소재 레이어도 경계로 쓰려면 설정의 **경계 레이어**에 이름을 추가합니다.
- 배경 폴더(`배경…`, `BG…`)는 칠하지 않고, 결과 PSD에서 끕니다.
- 결과 PSD에는 `선화` 폴더 바로 아래에 `밑색` 폴더가 생기고, 부위별 레이어(마스크 없음)가 들어갑니다.

## 5. 문제 해결

| 증상 | 해결 |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` | 최신 버전으로 업데이트하세요. OS 인증서 저장소를 씁니다 |
| AI 매칭이 오래 걸림 | 이미지 여러 장을 보고 판단하느라 1~3분 걸릴 수 있습니다(제한 10분) |
| 인물 색이 뒤바뀜·엑스트라가 회색 | 레퍼런스에 그 인물이 나와야 색을 맞춥니다. 캐릭터 시트를 레퍼런스로 쓰거나 `참고` 폴더에 점을 찍어 보완하세요. 결과 폴더(`…_AI매칭/answer.json`)를 고쳐 '요청 폴더' 방식으로 다시 실행해도 됩니다 |
| MCP 도구가 안 보임 | 설정 파일 경로·JSON 문법을 확인하고 Claude 데스크톱을 완전히 종료 후 다시 여세요 |

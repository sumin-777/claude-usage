#!/usr/bin/env python3
"""
src/ 의 조각들을 합쳐 배포본 claude-usage.py 를 만든다.

    python3 build.py

- src/collect.py    : 트랜스크립트 파싱·집계 (단독 실행도 가능)
- src/server.py     : 대시보드 서버 + 증분 스캔 + CLI (@@CORE@@, @@HTML@@ 자리표시자)
- src/dashboard.html: 화면 (문서 골격 없이 본문만)
"""

from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "claude-usage.py"

html = (HERE / "src" / "dashboard.html").read_text(encoding="utf-8")
collect = (HERE / "src" / "collect.py").read_text(encoding="utf-8")
server = (HERE / "src" / "server.py").read_text(encoding="utf-8")

# collect.py 에서 재사용할 구간만 (임포트 ~ main 직전)
core = collect[collect.index("import argparse"):collect.index("def main():")].rstrip()
core = core.replace("import argparse\n", "")   # argparse 는 server.py 가 임포트한다

if '"""' in html:
    raise SystemExit("dashboard.html 에 삼중따옴표가 있으면 안 됩니다 (raw string 으로 박음).")

out = server.replace("@@CORE@@", core).replace("@@HTML@@", html)
OUT.write_text(out, encoding="utf-8")

compile(out, str(OUT), "exec")   # 문법 확인
print("wrote %s (%.0f KB)" % (OUT, OUT.stat().st_size / 1024))

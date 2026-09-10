# CLAUDE.md

이 저장소를 고칠 때 알아야 할 것들. 사용법은 README.md 를 보라.

## 구조

`claude-usage.py` 는 **빌드 산출물이다. 직접 고치지 마라.** `src/` 를 고치고 빌드한다.

```
src/collect.py      트랜스크립트 파싱·집계. 단독 실행도 됨
src/server.py       대시보드 서버 + 증분 스캔 + CLI. @@CORE@@, @@HTML@@ 자리표시자
src/dashboard.html  화면. 문서 골격(<html>/<head>/<body>) 없이 본문만
src/merge.py        여러 JSON 을 미리 합치는 보조 스크립트 (본체와 독립)
build.py            src/ → claude-usage.py
```

```bash
python3 build.py
```

`build.py` 는 `src/collect.py` 에서 `import argparse` 부터 `def main():` 직전까지를
잘라 `@@CORE@@` 에 넣는다. 그 구간의 함수 이름을 바꾸면 `server.py` 가 깨진다.
바꾼 뒤에는 반드시 빌드하고 커밋해야 배포본에 반영된다.

## 밟으면 아픈 것들

### 1. 날짜는 반드시 UTC 기준으로 계산한다

`new Date("2026-09-10T00:00:00")` 은 **로컬**로 파싱되는데 `toISOString()` 은
**UTC**로 되돌린다. UTC+9(한국)에서는 하루를 더해도 같은 날짜가 나온다.

```js
// 절대 이렇게 쓰지 마라 — UTC+9 에서 무한루프
var d = new Date(iso + "T00:00:00");
d.setDate(d.getDate() + n);
return d.toISOString().slice(0, 10);
```

이 버그로 기간 축 생성 루프가 끝나지 않아 브라우저 탭이 통째로 멈췄다.
`dashboard.html` 의 `dayParse` / `dayKey` / `dayAdd` / `weekdayOf` / `todayKey` 를 쓰고,
새 날짜 코드를 직접 쓰지 마라. 축 생성 루프에는 전진하지 않으면 빠져나오는
안전장치가 있다 — 제거하지 마라.

### 2. 테스트 환경이 UTC면 위 버그가 안 잡힌다

개발 샌드박스는 대개 UTC라 통과한다. 타임존을 바꿔서 확인해야 한다.

```js
await browser.newPage({ timezoneId: 'Asia/Seoul' })
```

최소한 `Asia/Seoul`(UTC+9), `UTC`, `America/Los_Angeles`(음수 오프셋),
`Pacific/Kiritimati`(UTC+14)에서 기간 버튼 전부를 눌러본다.

### 3. Python 3.6 문법만 쓴다

배포 대상에 Python 3.6 서버가 있다. 금지:

- `from __future__ import annotations`
- `datetime.fromisoformat` — `collect.py` 의 `parse_ts` 가 직접 파싱한다
- `str | None` 같은 3.10 어노테이션
- 대입 표현식(`:=`), f-string `=` 지정자

확인:

```bash
pip install vermin && vermin -t=3.6 claude-usage.py
```

### 4. `src/dashboard.html` 에 삼중따옴표(`"""`)를 넣지 마라

`server.py` 가 `PAGE = r"""..."""` 로 통째로 박는다. `build.py` 가 검사해서 막지만,
넣으면 빌드가 실패한다.

### 5. 스캔이 요청을 막으면 안 된다

`~/.claude/projects` 가 4GB인 사용자가 있다. 전체 파싱은 분 단위다.

- `collect_all()` 은 **절대 스캔을 기다리지 않는다.** 가진 것을 즉시 돌려주고,
  없으면 `scanning: true` 로 응답한다. 화면은 "스캔 중"을 띄우고 폴링한다.
- 스캔은 `_start_scan()` 을 통해 **동시에 하나만** 돈다. 이걸 어기면 4GB 파싱이
  여러 개 겹쳐 메모리를 먹고 PC가 멈춘다.
- 재스캔 간격은 `_rescan_interval()` 이 지난 스캔 시간의 10배로 늘린다.
  고정 30초로 되돌리면 스캔이 쉬지 않고 돌아 CPU를 계속 문다.

### 6. 캐시는 프로젝트 폴더당 파일 하나

세션 파일마다 캐시를 만들면 수천 개가 되고, Windows Defender 가 하나씩 검사해
첫 스캔이 크게 느려진다. `_load_dir_cache` / `_save_dir_cache` 단위를 유지하라.
변경 없는 항목은 **다시 쓰지 않는다** (`_dir_entries` 의 reused 분기).

### 7. 중복 제거를 빼면 조용히 과다 집계된다

세션을 재개하거나 분기하면 같은 assistant 응답이 여러 파일에 중복 기록된다.
키는 `message.id` + `requestId`. 에러가 안 나고 숫자만 부풀기 때문에 눈치채기 어렵다.

### 8. 증분 결과는 전체 파싱과 일치해야 한다

파싱·집계를 건드렸으면 두 경로를 비교하라. 한 번도 어긋난 적이 없다.

```python
a = cu.build_payload(ns)                 # 전체
b = cu.build_payload_incremental(ns)     # 증분
strip = lambda d: {k: v for k, v in d.items() if k not in ("generated_at", "source")}
assert strip(a) == strip(b)
```

새 레코드 1건을 붙였을 때 토큰이 정확히 그만큼만 느는지도 확인한다.

## 데이터 규약

수집 JSON 은 `schema: 1`. 형식은 README 참고. 키 약어는
`i` 입력 / `o` 출력 / `cw` 캐시 쓰기 / `cr` 캐시 읽기 / `m` 메시지 / `s` 세션.
**형식을 바꾸면 `schema` 를 올리고** `ingest()`(dashboard.html)와
`load_remote()`(server.py)의 검사도 같이 고쳐야 한다. 이미 배포된 머신들이
옛 형식을 보내온다.

## 숫자에 대한 태도

- 여기 수치는 **청구 기준도 구독 한도 기준도 아니다.** 그렇게 읽히게 쓰지 마라.
- 캐시 읽기가 총량의 90% 이상인 것은 정상이다. 총량만 크게 보여주고 끝내면
  오해를 부른다 — 화면에서 분리해 보여주는 이유다.
- 비용 단가는 내장하지 않는다. 자주 바뀌고 플랜마다 달라서, 그럴듯한 틀린 금액을
  보여주느니 사용자가 `--pricing` 으로 직접 주게 한다.
- 예시 데이터를 실제 수치처럼 보이게 두지 마라. 배너로 명시한다.

## 화면 계측

화면 맨 아래에 `계산 …ms · 그리기 …ms · 머신 N · 일자 N …` 이 항상 뜬다.
느리다는 제보를 받으면 이 값과 `--diag` 출력을 먼저 확인하라. 추측하지 말 것.

```bash
python3 claude-usage.py --diag
```

## 검증 체크리스트

코드를 고쳤으면:

1. `python3 build.py` — 빌드 통과
2. `vermin -t=3.6 claude-usage.py` — 3.6 유지
3. 증분 vs 전체 파싱 일치 (위 8번)
4. 타임존 4곳에서 기간 버튼 전부 (위 2번)
5. `--daemon` → `--status` → `--stop` 왕복
6. 라이트/다크 양쪽 렌더 확인

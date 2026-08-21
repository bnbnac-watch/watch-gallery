# watch-gallery

여러 이미지 URL을 다운로드해 그리드로 병합하고, 잠깐 퍼블릭 인터넷에 노출시킨 뒤 그 URL을 반환하는 내부 서비스.
크롤러가 추출한 이미지를 Slack 등에 공유하고 싶을 때 `watch-runner`가 호출한다.

## API

### POST /build

```json
{
  "image_urls": ["https://...", "https://..."],
  "cols": 4,
  "cell_w": 600,
  "cell_h": 800
}
```

`image_urls` 필수 — 비어 있으면 400. `cols`/`cell_w`/`cell_h`는 선택, 기본값은 위 예시와 같다.
`cell_w`/`cell_h`의 600x800(3:4)은 실제 폰 사진 세로 비율에 맞춘 값 — 정사각형이면 좌우로 레터박스
여백이 생기지만 이 비율은 딱 맞아 여백이 거의 없다. 그래도 안 맞는 비율의 이미지는 자르지 않고
레터박스(검은 여백)로 채운다 — 원본 내용을 보존하는 쪽을 고정값으로 택했다
(크롭 방식 선택지는 호출자가 하나뿐이라 YAGNI로 제거함).

응답:

```json
{
  "public_url": "https://bnbnac2.duckdns.org/<uuid>.jpg",
  "success_count": 8,
  "failed_urls": ["https://..."],
  "expose_seconds": 600
}
```

다운로드 URL 전부가 실패하면 422. 개별 실패는 `failed_urls`에 담겨 응답에 포함될 뿐 요청 자체는 성공 처리된다.

### GET /health

`{"status": "ok"}`

## 노출 방식

`PUBLIC_DOMAIN`은 정적 파일 서버가 상시 실행 중이라는 전제로 동작한다 (`watch-gallery-nginx` +
OCI 게이트웨이 nginx가 `PUBLIC_DOMAIN` → 이 서버의 `SERVE_DIR`로 향하는 경로를 항상 열어둔다 — 한 번만
설정, 이 서비스는 매번 건드리지 않음). 노출 제어는 서버가 아니라 **파일 존재 여부**로 한다 —
`/build`가 그리드 이미지를 `SERVE_DIR`에 쓰고 `public_url`을 즉시 반환한 뒤, 백그라운드에서
`EXPOSE_SECONDS` 뒤 그 파일을 삭제한다.

Slack의 Block Kit `image` 블록과 레거시 `attachments`는 도달성이 정상이어도 특정 도메인(예:
터널링 서비스) 이미지를 렌더링하지 않는 경우가 있다 — 이미지를 실제로 채널에 공유할 때는
`public_url`을 텍스트 메시지 본문에 넣어 Slack이 자체적으로 언퍼닝하게 하는 방식을 쓸 것.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SERVE_DIR` | `/serve` | 그리드 이미지를 쓰는 경로 — 정적 파일 서버가 서빙하는 디렉토리와 동일해야 함 |
| `PUBLIC_DOMAIN` | `bnbnac2.duckdns.org` | 응답의 `public_url`을 조합할 도메인 |
| `EXPOSE_SECONDS` | `600` | 파일을 쓴 뒤 삭제까지 대기하는 시간 |

## 포트

| 포트 | 용도 |
|---|---|
| 8080 | FastAPI — 컴포즈 내부에서만 노출 |

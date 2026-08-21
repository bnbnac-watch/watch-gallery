import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SERVE_DIR = Path(os.getenv("SERVE_DIR", "/serve"))
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "bnbnac2.duckdns.org")
EXPOSE_SECONDS = int(os.getenv("EXPOSE_SECONDS", "600"))
DOWNLOAD_TIMEOUT = 15
MAX_RETRIES = 2
BACKOFF_SECONDS = [1, 2]
GRID_GAP = 8  # 셀 사이 간격(px). 배경색과 동일한 검정이라 레터박스 여백과 시각적으로 자연스럽게 이어짐

_http_client: httpx.AsyncClient | None = None


class BuildRequest(BaseModel):
    image_urls: list[str]
    cols: int = 4
    cell_w: int = 600
    cell_h: int = 800


# ---------------------------------------------------------------------------
# 다운로드 (메모리에서만 처리, 디스크에 쓰지 않음)
# ---------------------------------------------------------------------------

async def _download_one(url: str) -> bytes:
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            res = await _http_client.get(url, timeout=DOWNLOAD_TIMEOUT)
            res.raise_for_status()
            return res.content
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BACKOFF_SECONDS[attempt])
    raise last_exc


async def _download_one_image(url: str) -> Image.Image | None:
    try:
        content = await _download_one(url)
        Image.open(BytesIO(content)).verify()  # 손상된 이미지 조기 검출 (verify 후 재사용 불가)
        return Image.open(BytesIO(content))
    except Exception as exc:
        logger.warning("다운로드 실패: %s (%s)", url, exc)
        return None


async def _download_images(urls: list[str]) -> tuple[list[Image.Image], list[str]]:
    # 순차 다운로드는 실패가 겹치면 (타임아웃 15s + 재시도 대기) x 이미지 수로 늘어나
    # watch-runner의 호출 타임아웃을 넘길 수 있어 동시에 받는다. 같은 CDN에 대한
    # 소수(보통 10장 내외) 동시 요청이라 부담도 크지 않다.
    results = await asyncio.gather(*[_download_one_image(url) for url in urls])
    images = [img for img in results if img is not None]
    failed_urls = [url for url, img in zip(urls, results) if img is None]
    return images, failed_urls


# ---------------------------------------------------------------------------
# 그리드 병합
# ---------------------------------------------------------------------------

def _fit_letterbox(image: Image.Image, cell_w: int, cell_h: int, bg_color=(0, 0, 0)) -> Image.Image:
    return ImageOps.pad(image, (cell_w, cell_h), method=Image.LANCZOS, color=bg_color)


def _build_grid(images: list[Image.Image], cols: int, cell_w: int, cell_h: int) -> Path:
    rows = (len(images) + cols - 1) // cols
    grid = Image.new(
        "RGB",
        (cols * cell_w + (cols - 1) * GRID_GAP, rows * cell_h + (rows - 1) * GRID_GAP),
        (0, 0, 0),
    )

    for idx, img in enumerate(images):
        try:
            rgb_img = img.convert("RGB")
        except Exception as exc:
            logger.warning("이미지 처리 실패, 건너뜀: %s", exc)
            continue

        cell_img = _fit_letterbox(rgb_img, cell_w, cell_h)
        row, col = divmod(idx, cols)
        grid.paste(cell_img, (col * (cell_w + GRID_GAP), row * (cell_h + GRID_GAP)))

    SERVE_DIR.mkdir(parents=True, exist_ok=True)
    output_path = SERVE_DIR / f"{uuid.uuid4().hex}.jpg"
    grid.save(output_path, "JPEG", quality=90)
    return output_path


# ---------------------------------------------------------------------------
# 노출 종료 (일정 시간 후 파일 삭제 - 서버는 상시 실행, 파일 존재 여부로 노출 제어)
# ---------------------------------------------------------------------------

async def _schedule_cleanup(path: Path, delay: int):
    await asyncio.sleep(delay)
    path.unlink(missing_ok=True)
    logger.info("노출 종료, 삭제됨: %s", path.name)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    async with httpx.AsyncClient() as client:
        _http_client = client
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/build")
async def build(req: BuildRequest):
    if not req.image_urls:
        raise HTTPException(status_code=400, detail="image_urls가 비어 있습니다.")

    images, failed_urls = await _download_images(req.image_urls)
    if not images:
        raise HTTPException(status_code=422, detail="다운로드에 성공한 이미지가 없습니다.")

    output_path = _build_grid(images, req.cols, req.cell_w, req.cell_h)
    public_url = f"https://{PUBLIC_DOMAIN}/{output_path.name}"

    asyncio.create_task(_schedule_cleanup(output_path, EXPOSE_SECONDS))

    logger.info(
        "빌드 완료: 성공 %d장, 실패 %d장 → %s (%d초 후 삭제)",
        len(images), len(failed_urls), public_url, EXPOSE_SECONDS,
    )
    return JSONResponse({
        "public_url": public_url,
        "success_count": len(images),
        "failed_urls": failed_urls,
        "expose_seconds": EXPOSE_SECONDS,
    })


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, loop="asyncio")

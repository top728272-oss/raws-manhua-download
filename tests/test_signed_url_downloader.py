"""End-to-end: signed, expiring image URLs through the whole download engine.

Replays the dumanwu/shimolife incident from manhua-dl.log against a local
"source site" + "CDN": the site's reader page embeds image URLs signed with
``x-expires``, and the CDN answers 403 "ACCESS DENIED" to every request whose
signature has lapsed - the same deterministic rejection that burned 15k failed
requests in one day.

Locked in here:

1. URLs that are already expired when resolved are NEVER requested - the run
   goes straight to recovery, which re-resolves and can pick up a regenerated
   mint, instead of burning the retry budget on dead URLs.
2. A 403 on a live URL is not retried (one request per page per resolution,
   not retries+1); a 429 still is, because it clears on its own.
3. A batch where only some URLs expired downloads the live pages, records the
   holes, and the recovery pass re-resolves and fills exactly those holes.

The artifact checked at the end is the downloaded chapter on disk plus the
CDN request log the local server records, so every assertion is reproducible
from files.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.config import AppConfig, DownloadConfig, NetworkConfig
from app.core.downloader import DownloadEngine
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, DownloadTask, OutputFormat, Page
from app.core.output import OutputWriter

PAGES = 4
STALE = "stale"
FRESH = "fresh"


class _State:
    """Shared between the test and the local server's handler thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.resolves = 0
        self.generation = 0
        # page index -> which mint generation it carries on every resolution
        self.mint_plan: dict[int, str] = {}
        self.image_status = 200
        self.image_requests: list[dict] = []


def _make_server(state: _State) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive with Content-Length; avoids
        # a close race that shows up as "Server disconnected" on Windows.

        def log_message(self, *args) -> None:  # keep the test output clean
            pass

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path.startswith("/read/"):
                with state.lock:
                    state.resolves += 1
                    state.generation = state.resolves
                ch = path.rsplit("/", 1)[-1]
                now = int(time.time())
                imgs = []
                for i in range(PAGES):
                    # The stale mint plan describes the FIRST resolution only;
                    # from the second one on the site regenerates signatures.
                    stale = state.mint_plan.get(i) == STALE and state.generation == 1
                    mint = now - 7 * 3600 if stale else now + 3600
                    imgs.append(
                        f'<img src="http://127.0.0.1:{self.server.server_port}'
                        f'/img/{ch}/{i}.webp?x-expires={mint}&x-signature=abc">'
                    )
                body = ("<html><body>" + "".join(imgs) + "</body></html>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path.startswith("/img/"):
                expires = int(re.search(r"x-expires=(\d+)", query).group(1))
                page = re.search(r"/img/[^/]+/(\d+)\.webp", path).group(1)
                with state.lock:
                    state.image_requests.append(
                        {
                            "page": page,
                            "generation": state.generation,
                            "mint": expires,
                            "at": time.time(),
                        }
                    )
                if expires <= time.time():
                    # What shimolife answers to a lapsed signature.
                    body = b"<html><h1>ERROR: ACCESS DENIED</h1></html>"
                    self.send_response(403)
                elif state.image_status != 200:
                    self.send_response(state.image_status)
                    body = b"rejected"
                else:
                    self.send_response(200)
                    body = f"IMG-BYTES-{page}".encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _cfg(tmp_path, retries: int = 3) -> AppConfig:
    return AppConfig(
        output_dir=str(tmp_path),
        download=DownloadConfig(retries=retries, backoff_seconds=0, max_concurrent=4),
        network=NetworkConfig(request_delay_ms=0),
    )


def _task() -> DownloadTask:
    chapter = Chapter(source_id="signed", manga_ref="m1", title="第1话", ref="c1", index=1)
    return DownloadTask(
        source_id="signed",
        manga_title="签到系列",
        chapter=chapter,
        formats=[OutputFormat.FOLDER],
    )


def _resolver(fetcher: HttpFetcher, base: str):
    """Mirrors the dumanwu plugin shape: fetch the reader page, extract URLs."""

    async def resolve(chapter: Chapter) -> list[Page]:
        html = await fetcher.get_text(f"{base}/read/{chapter.ref}")
        urls = re.findall(r'<img src="([^"]+)"', html)
        return [Page(index=i, image_url=u, referer=f"{base}/") for i, u in enumerate(urls)]

    return resolve


async def _run(tmp_path, state: _State, retries: int = 3):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(tmp_path, retries=retries)
    server = _make_server(state)
    base = f"http://127.0.0.1:{server.server_port}"
    fetcher = HttpFetcher(cfg)
    engine = DownloadEngine(cfg, fetcher=fetcher, writer=OutputWriter(cfg), fallback=None)
    task = _task()
    try:
        summary = await engine.run([task], resolve_pages=_resolver(fetcher, base))
    finally:
        await fetcher.aclose()
        server.shutdown()
        server.server_close()

    # The persisted artifact: every request the CDN saw, in order.
    artifact = tmp_path / "cdn-requests.json"
    artifact.write_text(json.dumps(state.image_requests, indent=1), encoding="utf-8")
    return task, summary, state


def _per_generation_page(state: _State) -> Counter:
    return Counter((r["generation"], r["page"]) for r in state.image_requests)


def _no_stale_reached_the_cdn(state: _State) -> bool:
    return all(r["mint"] > r["at"] for r in state.image_requests)


async def test_stale_mint_is_skipped_then_recovered_after_regeneration(tmp_path, caplog):
    # The first resolution hands out a fully dead mint (the site served a stale
    # cached page); every resolution after that regenerates fresh signatures.
    state = _State()
    state.mint_plan = {i: STALE for i in range(PAGES)}
    task, summary, state = await _run(tmp_path, state)

    # The dead URLs never reached the CDN.
    assert state.image_requests
    assert _no_stale_reached_the_cdn(state)

    # The recovery pass re-resolved, got the fresh mint and completed the chapter.
    assert task.complete is True
    assert task.pages_saved == PAGES
    assert summary.ok and summary.saved == 1
    chapter_dir = tmp_path / "signed" / "签到系列" / "第1话"
    saved = sorted(p.name for p in chapter_dir.glob("*.webp"))
    assert saved == ["001.webp", "002.webp", "003.webp", "004.webp"]
    assert (chapter_dir / "003.webp").read_bytes() == b"IMG-BYTES-2"

    # Exactly one request per fresh URL, and the log records why phase 1 was skipped.
    assert len(state.image_requests) == PAGES
    assert "signed url expired" in caplog.text


async def test_403_is_not_retried_but_429_is(tmp_path):
    state = _State()
    state.image_status = 403
    task, _summary, state = await _run(tmp_path, state, retries=3)

    # One request per page per resolution: the initial run and the recovery
    # pass each try every page once, and never burn the retry budget.
    counts = _per_generation_page(state)
    assert set(counts.values()) == {1}
    assert len(counts) == 2 * PAGES
    assert task.pages_saved == 0

    rate = _State()
    rate.image_status = 429
    task2, _summary2, rate = await _run(tmp_path / "rate", rate, retries=3)
    # 429 clears, so every page gets the full budget: retries + 1 in the run,
    # recovery_attempts in the recovery pass.
    counts2 = _per_generation_page(rate)
    assert {n for (gen, _page), n in counts2.items() if gen == 1} == {4}
    assert {n for (gen, _page), n in counts2.items() if gen == 2} == {3}
    assert task2.pages_saved == 0


async def test_mixed_batch_downloads_live_pages_and_recovery_fills_the_holes(tmp_path):
    state = _State()
    # Pages 0 and 1 carry the dead mint on the first resolution only.
    state.mint_plan = {0: STALE, 1: STALE}
    task, summary, state = await _run(tmp_path, state)

    chapter_dir = tmp_path / "signed" / "签到系列" / "第1话"
    saved = sorted(p.name for p in chapter_dir.glob("*.webp"))
    assert saved == ["001.webp", "002.webp", "003.webp", "004.webp"]
    assert (chapter_dir / "001.webp").read_bytes() == b"IMG-BYTES-0"
    assert (chapter_dir / "002.webp").read_bytes() == b"IMG-BYTES-1"
    assert task.complete is True
    assert summary.ok and summary.recovered == 1

    # Generation 1 fetched only the two live pages, recovery fetched only the
    # two holes, and no expired URL was ever requested.
    counts = _per_generation_page(state)
    assert {page for (gen, page), _n in counts.items() if gen == 1} == {"2", "3"}
    assert {page for (gen, page), _n in counts.items() if gen == 2} == {"0", "1"}
    assert _no_stale_reached_the_cdn(state)

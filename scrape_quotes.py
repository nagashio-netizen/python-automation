"""quotes.toscrape.com/js から名言のテキストと著者名を Playwright で収集する。

JavaScript で描画されるページのため、ブラウザで描画を待ってから取得する。

- ブラウザを画面に表示して実行する(headless=False)
- robots.txt を読み込み、禁止パスにはアクセスしない
- リクエスト間に 1〜3 秒のランダムな待機を入れる
- 接続エラー時はログにエラーを出力して終了する
- 結果を Markdown に、1 ページ目のスクリーンショットを PNG に保存する

使い方:
    python scrape_quotes.py               # 全ページを収集
    python scrape_quotes.py --max-pages 2  # 先頭 2 ページだけ収集
"""

import argparse
import logging
import random
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

BASE_URL = "https://quotes.toscrape.com/js/"
USER_AGENT = "python-automation-quote-scraper/1.0"
TIMEOUT_MS = 15_000
WAIT_RANGE = (1.0, 3.0)  # リクエスト間の待機時間(秒)

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = PROJECT_DIR / "logs"

logger = logging.getLogger("scrape_quotes")


class FetchError(Exception):
    """接続エラーや HTTP エラーでページを取得できなかったことを表す。"""


def setup_logging() -> None:
    """コンソールとログファイルの両方に出力する。"""
    LOG_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(
        LOG_DIR / "scrape_quotes.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class PoliteBrowser:
    """robots.txt の遵守とリクエスト間の待機を担当するブラウザ操作クラス。"""

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.robots = RobotFileParser()
        self._last_request = None  # 最後にリクエストした時刻
        self._load_robots(urljoin(base_url, "/robots.txt"))

    def _wait(self) -> None:
        """前回のリクエストから 1〜3 秒(ランダム)空ける。"""
        if self._last_request is None:
            return
        wait = random.uniform(*WAIT_RANGE)
        elapsed = time.monotonic() - self._last_request
        if wait > elapsed:
            time.sleep(wait - elapsed)

    def _load_robots(self, robots_url: str) -> None:
        """robots.txt を取得して解析する(urllib.robotparser と同じ扱い)。"""
        try:
            response = self.page.request.get(robots_url, timeout=TIMEOUT_MS)
        except PlaywrightError as e:
            raise FetchError(f"robots.txt を取得できませんでした: {e}") from e
        finally:
            self._last_request = time.monotonic()

        if response.status in (401, 403):
            logger.warning("robots.txt へのアクセスが拒否されました。全パスを禁止扱いにします")
            self.robots.disallow_all = True
        elif 400 <= response.status < 500:
            logger.info("robots.txt がありません(HTTP %d)。全パスを許可扱いにします",
                        response.status)
            self.robots.allow_all = True
        elif not response.ok:
            raise FetchError(f"robots.txt の取得で HTTP {response.status} が返りました")
        else:
            self.robots.parse(response.text().splitlines())
            logger.info("robots.txt を読み込みました: %s", robots_url)

    def open(self, url: str) -> bool:
        """許可されていればページを開いて名言の描画を待つ。禁止なら False。"""
        if not self.robots.can_fetch(USER_AGENT, url):
            logger.warning("robots.txt で禁止されているためスキップします: %s", url)
            return False

        self._wait()
        try:
            response = self.page.goto(url, timeout=TIMEOUT_MS)
            if response is not None and not response.ok:
                raise FetchError(f"HTTP {response.status} が返りました: {url}")
            # JavaScript で名言が描画されるまで待つ
            self.page.wait_for_selector("div.quote", timeout=TIMEOUT_MS)
        except PlaywrightError as e:
            raise FetchError(f"ページを取得できませんでした: {url}: {e}") from e
        finally:
            self._last_request = time.monotonic()
        return True


def parse_quotes(page: Page) -> list[dict[str, str]]:
    """描画済みのページから名言と著者名を抜き出す。"""
    quotes = []
    for quote in page.locator("div.quote").all():
        quotes.append({
            "text": quote.locator("span.text").inner_text().strip(),
            "author": quote.locator("small.author").inner_text().strip(),
        })
    return quotes


def scrape(max_pages: int | None, screenshot_path: Path) -> list[dict[str, str]]:
    quotes = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            client = PoliteBrowser(page, BASE_URL)
            url = BASE_URL
            page_no = 0

            while url and (max_pages is None or page_no < max_pages):
                if not client.open(url):
                    break
                page_no += 1
                if page_no == 1:
                    page.screenshot(path=screenshot_path, full_page=True)
                    logger.info("スクリーンショットを保存しました: %s", screenshot_path)

                page_quotes = parse_quotes(page)
                quotes.extend(page_quotes)
                logger.info("%d ページ目: %d 件取得 (%s)", page_no, len(page_quotes), url)

                next_link = page.locator("li.next > a")
                url = (urljoin(url, next_link.get_attribute("href"))
                       if next_link.count() else None)
        finally:
            browser.close()
    return quotes


def escape_md(text: str) -> str:
    """Markdown の表を崩さないよう | をエスケープする。"""
    return text.replace("|", "\\|")


def write_markdown(quotes: list[dict[str, str]], path: Path, today: date) -> None:
    lines = [
        f"# 名言一覧({today:%Y-%m-%d})",
        "",
        f"- 収集元: {BASE_URL}",
        f"- 件数: {len(quotes)} 件",
        f"- スクリーンショット: [{path.with_suffix('.png').name}]"
        f"({path.with_suffix('.png').name})",
        "",
        "| No. | 名言 | 著者 |",
        "| ---: | --- | --- |",
    ]
    for i, quote in enumerate(quotes, start=1):
        lines.append(
            f"| {i} | {escape_md(quote['text'])} | {escape_md(quote['author'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-pages", type=int, default=None,
                        help="収集する最大ページ数(省略時は全ページ)")
    args = parser.parse_args()

    setup_logging()
    logger.info("収集を開始します: %s", BASE_URL)

    today = date.today()
    OUTPUT_DIR.mkdir(exist_ok=True)
    md_path = OUTPUT_DIR / f"quotes_{today:%Y%m%d}.md"
    png_path = md_path.with_suffix(".png")

    try:
        quotes = scrape(args.max_pages, png_path)
    except FetchError as e:
        logger.error("接続エラーが発生しました: %s", e)
        return 1

    write_markdown(quotes, md_path, today)
    logger.info("%d 件を保存しました: %s", len(quotes), md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

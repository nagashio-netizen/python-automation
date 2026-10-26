"""quotes.toscrape.com/js から名言のテキストと著者名を BeautifulSoup で収集する。

このページは JavaScript で名言を描画するため、取得した HTML には名言の要素
(div.quote)がない。代わりに <script> 内に埋め込まれた `var data = [...]` の
JSON を BeautifulSoup で取り出して解析する(ブラウザ不要)。

- robots.txt を読み込み、禁止パスにはアクセスしない
- リクエスト間に 1〜3 秒のランダムな待機を入れる
- 接続エラー時はログにエラーを出力して終了する
- 結果を Markdown に保存する(Playwright 版と区別するため quotes_bs4_〔日付〕.md)

使い方:
    python scrape_quotes_bs4.py               # 全ページを収集
    python scrape_quotes_bs4.py --max-pages 2  # 先頭 2 ページだけ収集
"""

import argparse
import json
import logging
import random
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://quotes.toscrape.com/js/"
USER_AGENT = "python-automation-quote-scraper-bs4/1.0"
TIMEOUT = 10  # 秒
WAIT_RANGE = (1.0, 3.0)  # リクエスト間の待機時間(秒)

# <script> 内の `var data = [...];` から JSON 部分を取り出す
DATA_PATTERN = re.compile(r"var data = (\[.*?\]);", re.DOTALL)

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = PROJECT_DIR / "logs"

logger = logging.getLogger("scrape_quotes_bs4")


def setup_logging() -> None:
    """コンソールとログファイルの両方に出力する。"""
    LOG_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(
        LOG_DIR / "scrape_quotes_bs4.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


class PoliteSession:
    """robots.txt の遵守とリクエスト間の待機を担当するセッション。"""

    def __init__(self, base_url: str) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
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

    def _request(self, url: str) -> requests.Response:
        self._wait()
        try:
            return self.session.get(url, timeout=TIMEOUT)
        finally:
            self._last_request = time.monotonic()

    def _load_robots(self, robots_url: str) -> None:
        """robots.txt を取得して解析する(urllib.robotparser と同じ扱い)。"""
        response = self._request(robots_url)
        if response.status_code in (401, 403):
            logger.warning("robots.txt へのアクセスが拒否されました。全パスを禁止扱いにします")
            self.robots.disallow_all = True
        elif 400 <= response.status_code < 500:
            logger.info("robots.txt がありません(HTTP %d)。全パスを許可扱いにします",
                        response.status_code)
            self.robots.allow_all = True
        else:
            response.raise_for_status()
            self.robots.parse(response.text.splitlines())
            logger.info("robots.txt を読み込みました: %s", robots_url)

    def get_soup(self, url: str) -> BeautifulSoup | None:
        """許可されていればページを取得して BeautifulSoup を返す。禁止なら None。"""
        if not self.robots.can_fetch(USER_AGENT, url):
            logger.warning("robots.txt で禁止されているためスキップします: %s", url)
            return None
        response = self._request(url)
        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser")


def parse_quotes(soup: BeautifulSoup) -> list[dict[str, str]]:
    """<script> に埋め込まれた JSON から名言と著者名を抜き出す。"""
    script = soup.find("script", string=DATA_PATTERN)
    if script is None:
        logger.warning("名言データを含む <script> が見つかりませんでした")
        return []
    data = json.loads(DATA_PATTERN.search(script.string).group(1))
    return [
        {"text": item["text"], "author": item["author"]["name"]}
        for item in data
    ]


def scrape(max_pages: int | None) -> list[dict[str, str]]:
    client = PoliteSession(BASE_URL)
    quotes = []
    url = BASE_URL
    page = 0

    while url and (max_pages is None or page < max_pages):
        soup = client.get_soup(url)
        if soup is None:
            break
        page += 1
        page_quotes = parse_quotes(soup)
        quotes.extend(page_quotes)
        logger.info("%d ページ目: %d 件取得 (%s)", page, len(page_quotes), url)

        # 「Next」リンクは JavaScript ではなく HTML に直接書かれている
        next_link = soup.select_one("li.next > a")
        url = urljoin(url, next_link["href"]) if next_link else None

    return quotes


def escape_md(text: str) -> str:
    """Markdown の表を崩さないよう | をエスケープする。"""
    return text.replace("|", "\\|")


def write_markdown(quotes: list[dict[str, str]], today: date) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"quotes_bs4_{today:%Y%m%d}.md"

    lines = [
        f"# 名言一覧({today:%Y-%m-%d})",
        "",
        f"- 収集元: {BASE_URL}",
        "- 取得方法: BeautifulSoup(<script> 内の埋め込みデータを解析)",
        f"- 件数: {len(quotes)} 件",
        "",
        "| No. | 名言 | 著者 |",
        "| ---: | --- | --- |",
    ]
    for i, quote in enumerate(quotes, start=1):
        lines.append(
            f"| {i} | {escape_md(quote['text'])} | {escape_md(quote['author'])} |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-pages", type=int, default=None,
                        help="収集する最大ページ数(省略時は全ページ)")
    args = parser.parse_args()

    setup_logging()
    logger.info("収集を開始します: %s", BASE_URL)

    try:
        quotes = scrape(args.max_pages)
    except requests.exceptions.ConnectionError as e:
        logger.error("接続エラーが発生しました: %s", e)
        return 1
    except requests.exceptions.Timeout as e:
        logger.error("接続がタイムアウトしました: %s", e)
        return 1
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP エラーが発生しました: %s", e)
        return 1

    path = write_markdown(quotes, date.today())
    logger.info("%d 件を保存しました: %s", len(quotes), path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""quotes.toscrape.com に Playwright でログインし、人気タグを取得してログアウトする。

手順:
    1. ログインページを開き、ID・パスワードを入力してログイン
    2. トップページのサイドバー「Top Ten tags」から人気タグを取得
    3. ログアウトし、ログアウトできたことを確認

- ログイン情報は .env の QUOTES_USERNAME / QUOTES_PASSWORD から読み込む
- robots.txt を読み込み、禁止パスにはアクセスしない
- リクエスト間に 1〜3 秒のランダムな待機を入れる
- 接続エラーやログイン失敗時はログにエラーを出力して終了する
- 結果を Markdown に保存する(popular_tags_〔日付〕.md)

使い方:
    python login_popular_tags.py             # ブラウザを表示して実行
    python login_popular_tags.py --headless  # ブラウザを表示せずに実行
"""

import argparse
import logging
import os
import random
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

BASE_URL = "https://quotes.toscrape.com/"
LOGIN_URL = urljoin(BASE_URL, "/login")
LOGOUT_URL = urljoin(BASE_URL, "/logout")
USER_AGENT = "python-automation-login-scraper/1.0"
TIMEOUT_MS = 15_000
WAIT_RANGE = (1.0, 3.0)  # リクエスト間の待機時間(秒)

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"
LOG_DIR = PROJECT_DIR / "logs"

logger = logging.getLogger("login_popular_tags")


class FetchError(Exception):
    """接続エラーや HTTP エラーでページを取得できなかったことを表す。"""


class LoginError(Exception):
    """ログイン・ログアウトに失敗したことを表す。"""


def setup_logging() -> None:
    """コンソールとログファイルの両方に出力する。"""
    LOG_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(
        LOG_DIR / "login_popular_tags.log", encoding="utf-8"
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

    def check_allowed(self, url: str) -> None:
        """robots.txt で禁止されていれば例外を出す。"""
        if not self.robots.can_fetch(USER_AGENT, url):
            raise LoginError(f"robots.txt で禁止されているためアクセスしません: {url}")

    def navigate(self, url: str, action=None) -> None:
        """ページ遷移を行う。action を渡すとその操作(クリック等)で遷移させる。

        遷移先 URL の robots.txt チェックと、リクエスト前の待機を共通で行う。
        """
        self.check_allowed(url)
        self._wait()
        try:
            if action is None:
                response = self.page.goto(url, timeout=TIMEOUT_MS)
            else:
                with self.page.expect_navigation(timeout=TIMEOUT_MS) as nav:
                    action()
                response = nav.value
            if response is not None and not response.ok:
                raise FetchError(f"HTTP {response.status} が返りました: {url}")
        except PlaywrightError as e:
            raise FetchError(f"ページを取得できませんでした: {url}: {e}") from e
        finally:
            self._last_request = time.monotonic()


def is_logged_in(page: Page) -> bool:
    """ヘッダーに「Logout」リンクがあればログイン中と判断する。"""
    return page.locator("a[href='/logout']").count() > 0


def login(client: PoliteBrowser, username: str, password: str) -> None:
    page = client.page
    client.navigate(LOGIN_URL)
    page.fill("#username", username)
    page.fill("#password", password)
    # フォームの送信先(/login への POST)も robots.txt のチェック対象にする
    client.navigate(LOGIN_URL, action=lambda: page.click("input[type='submit']"))

    if not is_logged_in(page):
        error = page.locator("p.error")
        detail = error.inner_text().strip() if error.count() else "原因不明"
        raise LoginError(f"ログインに失敗しました: {detail}")
    logger.info("ログインしました(ユーザー: %s)", username)


def get_popular_tags(client: PoliteBrowser) -> list[dict[str, str]]:
    """トップページのサイドバー「Top Ten tags」から人気タグを取得する。

    タグの文字サイズ(font-size)が大きいほど人気が高い。
    """
    page = client.page
    if page.url != BASE_URL:
        client.navigate(BASE_URL)
    page.wait_for_selector("div.tags-box a.tag", timeout=TIMEOUT_MS)

    tags = []
    for link in page.locator("div.tags-box a.tag").all():
        size = re.search(r"font-size:\s*(\d+)px", link.get_attribute("style") or "")
        tags.append({
            "name": link.inner_text().strip(),
            "url": urljoin(BASE_URL, link.get_attribute("href")),
            "font_size": size.group(1) if size else "",
        })
    logger.info("人気タグを %d 件取得しました", len(tags))
    return tags


def logout(client: PoliteBrowser) -> None:
    page = client.page
    client.navigate(LOGOUT_URL, action=lambda: page.click("a[href='/logout']"))
    if is_logged_in(page):
        raise LoginError("ログアウトに失敗しました")
    logger.info("ログアウトしました")


def run(username: str, password: str, headless: bool) -> list[dict[str, str]]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            client = PoliteBrowser(page, BASE_URL)
            login(client, username, password)
            try:
                return get_popular_tags(client)
            finally:
                # タグ取得に失敗してもログアウトはしておく
                if is_logged_in(page):
                    logout(client)
        finally:
            browser.close()


def write_markdown(tags: list[dict[str, str]], today: date) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"popular_tags_{today:%Y%m%d}.md"

    lines = [
        f"# 人気タグ(Top Ten tags)({today:%Y-%m-%d})",
        "",
        f"- 収集元: {BASE_URL}(ログイン後に取得)",
        f"- 件数: {len(tags)} 件",
        "- 文字サイズが大きいほど人気が高い",
        "",
        "| 順位 | タグ | 文字サイズ(px) | URL |",
        "| ---: | --- | ---: | --- |",
    ]
    for i, tag in enumerate(tags, start=1):
        lines.append(f"| {i} | {tag['name']} | {tag['font_size']} | {tag['url']} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--headless", action="store_true",
                        help="ブラウザを画面に表示せずに実行する")
    args = parser.parse_args()

    setup_logging()
    load_dotenv(PROJECT_DIR / ".env", encoding="utf-8")
    username = os.getenv("QUOTES_USERNAME")
    password = os.getenv("QUOTES_PASSWORD")
    if not username or not password:
        logger.error(".env に QUOTES_USERNAME と QUOTES_PASSWORD を設定してください")
        return 1

    logger.info("処理を開始します: %s", BASE_URL)
    try:
        tags = run(username, password, args.headless)
    except FetchError as e:
        logger.error("接続エラーが発生しました: %s", e)
        return 1
    except LoginError as e:
        logger.error("%s", e)
        return 1

    path = write_markdown(tags, date.today())
    logger.info("%d 件を保存しました: %s", len(tags), path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests

HERE = Path(__file__).resolve().parent
PRICES_PATH = HERE / "prices.json"
META_PATH = HERE / "meta.json"
GAME_DUMP_PATH = HERE / "mm2_items_dump.json"
MISSING_PATH = HERE / "missing.txt"

BASE_PREFIX = "https://supremevalues.com/mm2/"

CATEGORIES = [
    "godlies", "ancients", "chromas", "vintages",
    "collectibles", "pets", "legendaries", "rares",
    "uncommons", "commons", "sets", "uniques", "evos",
    "misc", "untradables",
]
UNTRADABLE_CATEGORIES = {"untradables"}

MIN_ITEMS_SANITY = 50
DELAY_MIN_SECONDS = 2
DELAY_MAX_SECONDS = 4

PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
TRAILING_WEAPON_RE = re.compile(r"[\s\-–—]*(gun|knife)\s*$", re.IGNORECASE)
VALUE_PREFIX_RE = re.compile(r"^value\s*[-–—:]\s*", re.IGNORECASE)
STABILITY_SPLIT_RE = re.compile(r"\s+stability\b", re.IGNORECASE)
RARITY_WORDS_RE = re.compile(
    r"\b(legendaries|legendary|rares|rare|uncommons|uncommon|commons|common)\b",
    re.IGNORECASE
)

RARITY_MAP = {
    "common": {"commons", "collectibles"},
    "uncommon": {"uncommons", "collectibles"},
    "rare": {"rares", "collectibles"},
    "legendary": {"legendaries", "collectibles"},
    "godly": {"godlies", "chromas"},
    "ancient": {"ancients", "chromas", "evos"},
    "classic": {"vintages"},
    "vintage": {"vintages"},
    "unique": {"uniques"},
}


def make_soup(markup):
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:
        return BeautifulSoup(markup, "html.parser")


def norm_name(text):
    if not text:
        return ""
    s = str(text).lower().strip()
    s = PAREN_SUFFIX_RE.sub("", s).strip()
    s = TRAILING_WEAPON_RE.sub("", s).strip()
    s = re.sub(r"^c\.\s*", "chroma ", s)
    s = s.replace("'", "").replace("’", "").replace("-", " ")
    return " ".join(s.split())


def extract_asset_id(image_url):
    if not image_url:
        return None
    match = re.search(r"\b(\d{6,16})\b", str(image_url))
    return match.group(1) if match else None


def extract_value(col):
    lines = [ln.strip() for ln in col.get_text("\n").splitlines()]
    for idx, line in enumerate(lines):
        m = VALUE_PREFIX_RE.match(line)
        if m:
            rest = line[m.end():].strip()
        elif line.lower() == "value":
            rest = ""
        else:
            continue
        if not rest and idx + 1 < len(lines):
            rest = lines[idx + 1].strip()
        rest = STABILITY_SPLIT_RE.split(rest, maxsplit=1)[0].strip()
        rest = RARITY_WORDS_RE.sub("", rest).strip()
        return " ".join(rest.split()) or None
    return None


def detect_weapon_from_col(raw_name, col):
    text = (raw_name + " " + " ".join(col.get("class", []))).lower()
    if "knife" in text:
        return "Knife"
    if "gun" in text:
        return "Gun"
    return None


def parse_category_html(html, category):
    untradable = category in UNTRADABLE_CATEGORIES
    soup = make_soup(html)
    parsed = []
    for col in soup.select(".itemcolumn"):
        name_el = col.select_one(".itemhead")
        if not name_el:
            continue
        raw_name = name_el.get_text(" ", strip=True)
        val = "untradable" if untradable else extract_value(col) or "untradable"
        w_type = detect_weapon_from_col(raw_name, col)
        is_chroma = "chroma" in raw_name.lower() or category == "chromas"

        parsed.append({
            "raw_name": raw_name,
            "norm": norm_name(raw_name),
            "value": val,
            "type": w_type,
            "is_chroma": is_chroma,
            "category": category,
        })
    return parsed


def scrape_with_curl(proxy_url=None):
    items = []
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://supremevalues.com/",
    }

    with cf_requests.Session(proxies=proxies, headers=headers) as session:
        for i, category in enumerate(CATEGORIES):
            if i > 0:
                time.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
            url = f"{BASE_PREFIX}{category}"
            try:
                resp = session.get(url, impersonate="chrome", timeout=30)
                if resp.status_code == 403 and i == 0:
                    print(f"[{category}] статус 403 через curl_cffi — переключаюсь на Camoufox...")
                    return None
                if resp.status_code != 200:
                    print(f"[{category}] статус {resp.status_code}")
                    continue
            except Exception as exc:
                print(f"[{category}] ошибка: {exc}")
                if i == 0:
                    return None
                continue

            cat_items = parse_category_html(resp.text, category)
            items.extend(cat_items)
            print(f"[{category}] спарсено: {len(cat_items)}")
    return items


def scrape_with_camoufox(proxy_url=None):
    from camoufox.sync_api import Camoufox

    items = []
    headless_mode = "virtual" if os.name != "nt" else True
    proxy_cfg = None
    if proxy_url:
        proxy_cfg = {"server": proxy_url.replace("socks5h://", "socks5://")}

    def run_browser(p_cfg):
        collected = []
        with Camoufox(headless=headless_mode, proxy=p_cfg) as browser:
            page = browser.new_page()
            for i, category in enumerate(CATEGORIES):
                if i > 0:
                    time.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
                url = f"{BASE_PREFIX}{category}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_selector(".itemcolumn", timeout=25000)
                    html = page.content()
                    cat_items = parse_category_html(html, category)
                    collected.extend(cat_items)
                    print(f"[{category}] (Camoufox) спарсено: {len(cat_items)}")
                except Exception as exc:
                    print(f"[{category}] (Camoufox) ошибка: {exc}")
        return collected

    items = run_browser(proxy_cfg)
    if not items and proxy_cfg:
        print("Пробуем Camoufox напрямую без прокси...")
        items = run_browser(None)
    return items


def scrape():
    proxy_url = os.environ.get("PROXY_URL", "").strip() or None
    if proxy_url:
        print(f"Используется прокси: {proxy_url}")

    items = scrape_with_curl(proxy_url)
    if items is None or len(items) < MIN_ITEMS_SANITY:
        items = scrape_with_camoufox(proxy_url)
    return items


def match_with_game(site_items):
    if not GAME_DUMP_PATH.exists():
        return {"by_asset": {}, "by_code": {}}

    game_dump = json.loads(GAME_DUMP_PATH.read_text(encoding="utf-8"))
    by_asset = {}
    by_code = {}
    matched = 0
    unmatched_list = []

    for code_id, info in game_dump.items():
        g_name = norm_name(info.get("ItemName", ""))

        if "bats" in g_name:
            continue

        g_type = info.get("ItemType", "")
        g_chroma = bool(info.get("Chroma", False))
        g_rarity = str(info.get("Rarity", "")).lower()
        year = str(info.get("Year", "")).strip()
        asset_id = extract_asset_id(info.get("Image"))

        allowed_categories = RARITY_MAP.get(g_rarity, set()) | {"collectibles", "misc", "untradables"}
        if g_chroma:
            allowed_categories.add("chromas")

        g_base = re.sub(r"^chroma\s+", "", g_name).strip()
        short_year = year[-2:] if len(year) == 4 else year

        val = None

        if year:
            for s in site_items:
                s_base = re.sub(r"^chroma\s+", "", s["norm"]).strip()
                s_raw = s["raw_name"].lower()

                if (year in s_raw or f"'{short_year}" in s_raw) and g_base in s_base:
                    if g_chroma != s["is_chroma"]:
                        continue
                    if s["type"] and g_type and s["type"] != g_type:
                        continue
                    val = s["value"]
                    break

        if not val:
            for s in site_items:
                s_base = re.sub(r"^chroma\s+", "", s["norm"]).strip()
                if s_base != g_base:
                    continue
                if g_chroma != s["is_chroma"]:
                    continue
                if s["category"] not in allowed_categories:
                    continue
                if s["type"] and g_type and s["type"] != g_type:
                    continue
                val = s["value"]
                break

        if not val:
            for s in site_items:
                s_base = re.sub(r"^chroma\s+", "", s["norm"]).strip()
                if s_base == g_base and g_chroma == s["is_chroma"]:
                    if s["category"] in allowed_categories:
                        val = s["value"]
                        break

        if val:
            matched += 1
            by_code[code_id] = val
            if asset_id:
                by_asset[asset_id] = val
        else:
            unmatched_list.append(
                f"{code_id} | Name: {info.get('ItemName')} | Type: {g_type} | Rarity: {g_rarity} | Year: {year}"
            )

    if MISSING_PATH:
        MISSING_PATH.write_text("\n".join(unmatched_list), encoding="utf-8")

    print(f"Итого сматчено со скинами игры: {matched} из {len(game_dump)}")
    return {"by_asset": by_asset, "by_code": by_code}


def main():
    site_items = scrape()
    print(f"Всего получено предметов с сайта: {len(site_items)}")
    if len(site_items) < MIN_ITEMS_SANITY:
        print("Слишком мало предметов, отмена записи.")
        sys.exit(1)

    result = match_with_game(site_items)

    now = datetime.now(timezone.utc)
    PRICES_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    META_PATH.write_text(json.dumps({
        "updatedAt": now.isoformat(),
        "total_assets": len(result["by_asset"]),
        "total_codes": len(result["by_code"]),
        "site_items": len(site_items),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово! База записана в {PRICES_PATH}")


if __name__ == "__main__":
    main()

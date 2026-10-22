import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL = True
except ImportError:
    HAS_CURL = False

import requests as regular_requests

HERE = Path(__file__).resolve().parent
PRICES_PATH = HERE / "prices.json"
META_PATH = HERE / "meta.json"
GAME_DUMP_PATH = HERE / "mm2_items_dump.json"

BASE_PREFIX = "https://supremevalues.com/mm2/"

CATEGORIES = [
    "godlies", "ancients", "chromas", "vintages",
    "collectibles", "pets", "legendaries", "rares",
    "uncommons", "commons", "sets", "uniques", "evos",
    "misc", "untradables",
]
UNTRADABLE_CATEGORIES = {"untradables"}

VALUE_PREFIX_RE = re.compile(r"^value\s*[-–—:]\s*", re.IGNORECASE)
STABILITY_SPLIT_RE = re.compile(r"\s+stability\b", re.IGNORECASE)
RARITY_WORDS_RE = re.compile(
    r"\b(legendaries|legendary|rares|rare|uncommons|uncommon|commons|common)\b",
    re.IGNORECASE
)
PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
TRAILING_WEAPON_RE = re.compile(r"[\s\-–—]*(gun|knife)\s*$", re.IGNORECASE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def make_soup(markup):
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:
        return BeautifulSoup(markup, "html.parser")


def norm_name(text):
    if not text:
        return ""
    s = text.lower().strip()
    s = PAREN_SUFFIX_RE.sub("", s)
    s = TRAILING_WEAPON_RE.sub("", s)
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


def detect_weapon_type(raw_name, col):
    text = (raw_name + " " + " ".join(col.get("class", []))).lower()
    if "knife" in text:
        return "Knife"
    if "gun" in text:
        return "Gun"
    return None


def fetch_page(url, session):
    if HAS_CURL:
        try:
            resp = session.get(url, impersonate="chrome120", timeout=25, headers=HEADERS)
            if resp.status_code == 200 and len(resp.text) > 1000:
                return resp.text
            print(f"[parser] curl_cffi статус {resp.status_code} на {url}")
        except Exception as e:
            print(f"[parser] curl_cffi ошибка на {url}: {e}")

    try:
        resp = regular_requests.get(url, headers=HEADERS, timeout=25)
        if resp.status_code == 200:
            return resp.text
        print(f"[parser] regular_requests статус {resp.status_code} на {url}")
    except Exception as e:
        print(f"[parser] regular_requests ошибка на {url}: {e}")

    return None


def parse_site_items():
    parsed_items = []
    session = cf_requests.Session() if HAS_CURL else None

    for cat in CATEGORIES:
        time.sleep(random.uniform(1.2, 2.5))
        url = f"{BASE_PREFIX}{cat}"
        html = fetch_page(url, session)

        if not html:
            print(f"[parser] Пропуск категории {cat} — нет ответа")
            continue

        soup = make_soup(html)
        cols = soup.select(".itemcolumn")
        count = 0

        for col in cols:
            name_el = col.select_one(".itemhead")
            if not name_el:
                continue
            raw_name = name_el.get_text(" ", strip=True)
            val = "untradable" if cat in UNTRADABLE_CATEGORIES else extract_value(col) or "untradable"
            w_type = detect_weapon_type(raw_name, col)
            is_chroma = "chroma" in raw_name.lower() or "chromas" in cat.lower()

            parsed_items.append({
                "raw_name": raw_name,
                "norm_name": norm_name(raw_name),
                "value": val,
                "category": cat,
                "type": w_type,
                "is_chroma": is_chroma
            })
            count += 1

        print(f"[parser] {cat}: успешно спарсено {count} предметов")

    return parsed_items


def build_final_database(site_items):
    if not GAME_DUMP_PATH.exists():
        raise FileNotFoundError(f"Файл {GAME_DUMP_PATH} не найден!")

    game_dump = json.loads(GAME_DUMP_PATH.read_text(encoding="utf-8"))

    by_asset = {}
    by_code = {}
    matched = 0

    for code_id, info in game_dump.items():
        item_name = info.get("ItemName", "")
        item_type = info.get("ItemType", "")
        is_chroma = bool(info.get("Chroma", False))
        year = str(info.get("Year", ""))
        clean_g_name = norm_name(item_name)
        asset_id = extract_asset_id(info.get("Image"))

        best_val = None
        for s_item in site_items:
            if s_item["norm_name"] != clean_g_name:
                continue

            if s_item["is_chroma"] != is_chroma:
                continue

            # Если тип оружия известен на обеих сторонах — он должен совпадать
            if s_item["type"] and item_type and s_item["type"] != item_type:
                continue

            if year and year in s_item["raw_name"]:
                best_val = s_item["value"]
                break

            best_val = s_item["value"]
            break

        if best_val:
            matched += 1
            by_code[code_id] = best_val
            if asset_id:
                by_asset[asset_id] = best_val

    print(f"[matcher] Успешно сматчено скинов: {matched} из {len(game_dump)}")
    return {"by_asset": by_asset, "by_code": by_code}


def main():
    print("[main] Парсим SupremeValues...")
    site_items = parse_site_items()
    print(f"[main] Всего спарсено с сайта: {len(site_items)} записей")

    print("[main] Связываем с кодовой базой MM2...")
    result = build_final_database(site_items)

    now = datetime.now(timezone.utc)
    PRICES_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    META_PATH.write_text(json.dumps({
        "updatedAt": now.isoformat(),
        "total_assets": len(result["by_asset"]),
        "total_codes": len(result["by_code"])
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] Цены сохранены в {PRICES_PATH}")


if __name__ == "__main__":
    main()

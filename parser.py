import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests

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


def scrape():
    items = []
    with cf_requests.Session() as session:
        for i, category in enumerate(CATEGORIES):
            if i > 0:
                time.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
            url = f"{BASE_PREFIX}{category}"
            untradable = category in UNTRADABLE_CATEGORIES
            try:
                resp = session.get(url, impersonate="chrome", timeout=30)
                if resp.status_code != 200:
                    print(f"[{category}] статус {resp.status_code}")
                    continue
            except Exception as exc:
                print(f"[{category}] ошибка: {exc}")
                continue

            soup = make_soup(resp.text)
            count = 0
            for col in soup.select(".itemcolumn"):
                name_el = col.select_one(".itemhead")
                if not name_el:
                    continue
                raw_name = name_el.get_text(" ", strip=True)
                val = "untradable" if untradable else extract_value(col) or "untradable"
                w_type = detect_weapon_from_col(raw_name, col)
                is_chroma = "chroma" in raw_name.lower() or category == "chromas"

                items.append({
                    "raw_name": raw_name,
                    "norm": norm_name(raw_name),
                    "value": val,
                    "type": w_type,
                    "is_chroma": is_chroma,
                    "category": category
                })
                count += 1
            print(f"[{category}] спарсено: {count}")
    return items


def match_with_game(site_items):
    if not GAME_DUMP_PATH.exists():
        print(f"Внимание: {GAME_DUMP_PATH} не найден, сохраняем только плоский список")
        return {"by_asset": {}, "by_code": {}}

    game_dump = json.loads(GAME_DUMP_PATH.read_text(encoding="utf-8"))
    by_asset = {}
    by_code = {}
    matched = 0

    for code_id, info in game_dump.items():
        g_name = norm_name(info.get("ItemName", ""))
        g_type = info.get("ItemType", "")
        g_chroma = bool(info.get("Chroma", False))
        asset_id = extract_asset_id(info.get("Image"))

        # Ищем совпадение
        val = None
        for s in site_items:
            if s["norm"] == g_name:
                # Если скин хрома в игре — на сайте тоже должна быть хрома
                if g_chroma != s["is_chroma"]:
                    continue
                # Если у обоих известен тип (нож/пистолет) — сверяем его
                if s["type"] and g_type and s["type"] != g_type:
                    continue
                val = s["value"]
                break

        # Запасной поиск: без строгого типа, если не нашлось
        if not val:
            for s in site_items:
                if s["norm"] == g_name and g_chroma == s["is_chroma"]:
                    val = s["value"]
                    break

        if val:
            matched += 1
            by_code[code_id] = val
            if asset_id:
                by_asset[asset_id] = val

    print(f"Итого сматчено со скинами игры: {matched} из {len(game_dump)}")
    return {"by_asset": by_asset, "by_code": by_code}


def main():
    site_items = scrape()
    print(f"Всего получено предметов с сайта: {len(site_items)}")
    if len(site_items) < MIN_ITEMS_SANITY:
        print("Слишком мало предметов, отмена записи.")
        return

    result = match_with_game(site_items)

    now = datetime.now(timezone.utc)
    PRICES_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    META_PATH.write_text(json.dumps({
        "updatedAt": now.isoformat(),
        "total_assets": len(result["by_asset"]),
        "total_codes": len(result["by_code"]),
        "site_items": len(site_items)
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово! База записана в {PRICES_PATH}")


if __name__ == "__main__":
    main()

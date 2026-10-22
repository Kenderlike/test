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

RARITY_NORMALIZE = {
    "common": "common",
    "uncommon": "uncommon",
    "rare": "rare",
    "legendary": "legendary",
    "godly": "godly",
    "ancient": "ancient",
    "classic": "vintage",
    "vintage": "vintage",
    "unique": "unique",
    "halloween": "common",
    "christmas": "common"
}

VALUE_PREFIX_RE = re.compile(r"^value\s*[-–—:]\s*", re.IGNORECASE)
STABILITY_SPLIT_RE = re.compile(r"\s+stability\b", re.IGNORECASE)
RARITY_WORDS_RE = re.compile(
    r"\b(legendaries|legendary|rares|rare|uncommons|uncommon|commons|common)\b",
    re.IGNORECASE
)
PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
TRAILING_WEAPON_RE = re.compile(r"[\s\-–—]*(gun|knife)\s*$", re.IGNORECASE)


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
    # Нормализуем хромы и сокращения
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


def parse_site_items():
    if not HAS_CURL:
        raise RuntimeError("Установи curl_cffi: pip install curl_cffi")

    parsed_items = []
    with cf_requests.Session() as session:
        for cat in CATEGORIES:
            time.sleep(random.uniform(1.5, 3.0))
            url = f"{BASE_PREFIX}{cat}"
            try:
                resp = session.get(url, impersonate="chrome", timeout=30)
                if resp.status_code != 200:
                    continue
            except Exception as e:
                print(f"[parser] Ошибка загрузки {cat}: {e}")
                continue

            soup = make_soup(resp.text)
            for col in soup.select(".itemcolumn"):
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
            print(f"[parser] Категория {cat} успешно обработана")
    return parsed_items


def build_final_database(site_items):
    if not GAME_DUMP_PATH.exists():
        raise FileNotFoundError(f"Файл {GAME_DUMP_PATH} не найден! Загрузи его в репозиторий.")

    game_dump = json.loads(GAME_DUMP_PATH.read_text(encoding="utf-8"))

    by_asset = {}
    by_code = {}
    matched = 0

    # Создаём поисковый индекс по дампу игры
    for code_id, info in game_dump.items():
        item_name = info.get("ItemName", "")
        item_type = info.get("ItemType", "")
        rarity = RARITY_NORMALIZE.get(str(info.get("Rarity", "")).lower(), "unknown")
        is_chroma = bool(info.get("Chroma", False))
        year = str(info.get("Year", ""))
        clean_g_name = norm_name(item_name)

        asset_id = extract_asset_id(info.get("Image"))

        # Ищем наилучшее совпадение среди предметов сайта
        best_val = None
        for s_item in site_items:
            # 1. Проверяем название
            if s_item["norm_name"] != clean_g_name:
                continue

            # 2. Проверяем хрому
            if s_item["is_chroma"] != is_chroma:
                continue

            # 3. Проверяем тип оружия (если известен)
            if s_item["type"] and item_type and s_item["type"] != item_type:
                continue

            # 4. Проверяем год, если он фигурирует в названии сайта (например Cane 2018)
            if year and year in s_item["raw_name"] and not s_item["raw_name"].endswith(year):
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

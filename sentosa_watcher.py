#!/usr/bin/env python3
"""
Sentosa 4-Bedroom Rental Watcher
=================================
Мониторит появление 4-спальных квартир в указанном ценовом диапазоне
на Сентозе сразу на 4 сайтах (PropertyGuru, 99.co, SRX, Singapore Expats)
и шлёт мгновенное уведомление в Telegram с кнопкой "Написать агенту в WhatsApp".

ДВА РЕЖИМА РАБОТЫ (выбирается переменной окружения RUN_MODE):
  - RUN_MODE=once  → один прогон и выход. Так запускает GitHub Actions по расписанию.
  - RUN_MODE=loop  → бесконечный цикл с паузой (для запуска на маке/VPS вручную).
По умолчанию: loop при ручном запуске, once в GitHub Actions (там переменная задаётся в workflow).

ВАЖНО:
- Сайты защищены от ботов (особенно PropertyGuru/Cloudflare) и могут блокировать
  облачные IP. Скрипт использует Playwright (реальный браузер). Если какой-то сайт
  стабильно отдаёт 0 — он усилил защиту или сменил разметку. Остальные при этом работают.
- Чем больше сайтов, тем чаще что-то может ломаться при их редизайне — это нормально.
- Личное некоммерческое использование. Не уменьшай интервал ниже 30 мин.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ============================================================
#  НАСТРОЙКИ
#  В GitHub Actions токен и chat_id приходят из Secrets (переменные окружения).
#  Локально можно вписать прямо сюда или задать через export.
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TG_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
TELEGRAM_CHAT_ID   = os.environ.get("TG_CHAT_ID", "ВСТАВЬ_СЮДА_CHAT_ID")

PRICE_MIN = 8000
PRICE_MAX = 11500

CHECK_INTERVAL_MINUTES = 30  # используется только в режиме loop
RUN_MODE = os.environ.get("RUN_MODE", "loop").lower()
# ============================================================

# ============================================================
#  ИСТОЧНИКИ — 4 сайта.
#  Каждый сайт имеет ОДНУ страницу поиска "4-спальные на Сентозе/в Sentosa Cove
#  в нужном ценовом диапазоне". Поиск по всему району, а не по каждому кондо
#  отдельно — так надёжнее и проще. Фильтр по цене ещё раз применяется в коде.
#
#  Если какой-то сайт стабильно отдаёт 0 — он, скорее всего, сменил верстку
#  или заблокировал. Это не ломает остальные: каждый сайт обрабатывается отдельно.
# ============================================================
SOURCES = {
    "PropertyGuru": "https://www.propertyguru.com.sg/property-for-rent?market=residential&listing_type=rent&district_code[]=SENTOSA&bedrooms[]=4&minprice={pmin}&maxprice={pmax}&freetext=Sentosa+Cove",
    "99.co":        "https://www.99.co/singapore/rent/property?query_ids=dtdistrict4&query_type=district&main_category=residential&rental_type=unit&num_beds=4&price_min={pmin}&price_max={pmax}",
    "SRX":          "https://www.srx.com.sg/rent/search?propertyTypeGroup=Non-Landed&districtIds=04&minBeds=4&maxBeds=4&minPrice={pmin}&maxPrice={pmax}",
    "SingaporeExpats": "https://property.singaporeexpats.com/search?type=rent&bedrooms=4&min_price={pmin}&max_price={pmax}&keyword=Sentosa",
}

STATE_FILE = Path(__file__).parent / "seen_listings.json"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def log(msg: str):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_seen() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


# Готовый текст агенту (английский). %0A — перенос строки в URL WhatsApp.
AGENT_MESSAGE = ("Hi, I'd like to view this apartment. Could you let me know "
                 "when a viewing is possible, and whether the owner is open to "
                 "negotiation on the price? Thank you.")


def build_whatsapp_url(phone: str, listing_url: str) -> str:
    """Формирует ссылку wa.me с готовым текстом + ссылкой на листинг."""
    text = AGENT_MESSAGE + "\n\n" + listing_url
    # URL-кодируем текст
    encoded = requests.utils.quote(text)
    # phone уже нормализован (только цифры, с кодом страны)
    return f"https://wa.me/{phone}?text={encoded}"


def send_telegram(text: str, buttons: list | None = None):
    """buttons: список словарей {'text': ..., 'url': ...} для inline-кнопок."""
    if "ВСТАВЬ" in TELEGRAM_BOT_TOKEN or "ВСТАВЬ" in TELEGRAM_CHAT_ID:
        log("⚠️  Telegram не настроен — вывожу в терминал вместо отправки:")
        print(text)
        if buttons:
            for b in buttons:
                print(f"  [Кнопка] {b['text']} → {b['url']}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if buttons:
        # Каждая кнопка — отдельным рядом (вертикально)
        keyboard = [[{"text": b["text"], "url": b["url"]}] for b in buttons]
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            log(f"⚠️  Telegram вернул {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"⚠️  Не удалось отправить в Telegram: {e}")


def parse_price(text: str):
    m = re.search(r"S\$\s*([\d,]+)\s*/", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def extract_phone(text: str):
    """Пытается найти телефон агента в тексте карточки.
    Возвращает номер в международном формате для wa.me (только цифры, с кодом 65),
    либо None если номер не найден / ненадёжен.
    Сингапурские мобильные: 8 цифр, начинаются на 8 или 9."""
    if not text:
        return None
    # Ищем явный +65 ... или 8-значный мобильный
    # 1) формат +65 XXXX XXXX или 65XXXXXXXX
    m = re.search(r"\+?65[\s\-]?([89]\d{3})[\s\-]?(\d{4})", text)
    if m:
        return "65" + m.group(1) + m.group(2)
    # 2) голый 8-значный мобильный (8 или 9 в начале), не часть большего числа
    m = re.search(r"(?<!\d)([89]\d{3})[\s\-]?(\d{4})(?!\d)", text)
    if m:
        return "65" + m.group(1) + m.group(2)
    return None


def _domain_of(site: str) -> str:
    return {
        "PropertyGuru": "https://www.propertyguru.com.sg",
        "99.co": "https://www.99.co",
        "SRX": "https://www.srx.com.sg",
        "SingaporeExpats": "https://property.singaporeexpats.com",
    }.get(site, "")


def _listing_id_from_href(site: str, href: str):
    """Вытаскивает уникальный id листинга из ссылки. У каждого сайта свой формат."""
    if site == "PropertyGuru":
        m = re.search(r"-(\d{6,})$", href)
    elif site == "99.co":
        m = re.search(r"/([A-Za-z0-9]{6,})(?:\?|$)", href)
    elif site == "SRX":
        m = re.search(r"(\d{6,})", href)
    elif site == "SingaporeExpats":
        m = re.search(r"property-listing/(\d{4,})", href)
    else:
        m = re.search(r"(\d{6,})", href)
    return m.group(1) if m else None


async def fetch_listings(page, site: str, url: str):
    """Универсальный парсер для одного сайта. Возвращает список листингов
    в нужном ценовом диапазоне. Логика устойчивая: ищем ссылки на листинги,
    поднимаемся к карточке, читаем её текст, достаём цену и телефон."""
    results = []
    domain = _domain_of(site)
    try:
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)  # дать прогрузиться JS

        # Набор селекторов ссылок на листинги — по сайтам
        link_selectors = {
            "PropertyGuru": 'a[href*="/listing/"]',
            "99.co": 'a[href*="/singapore/rent/property/"], a[href*="/singapore/listings/"]',
            "SRX": 'a[href*="/rent/"][href*="/listing"], a[href*="/Listing/"]',
            "SingaporeExpats": 'a[href*="property-listing/"]',
        }
        selector = link_selectors.get(site, 'a[href]')
        anchors = await page.query_selector_all(selector)
        seen_ids_local = set()

        for a in anchors:
            href = await a.get_attribute("href")
            if not href:
                continue
            listing_id = _listing_id_from_href(site, href)
            if not listing_id or listing_id in seen_ids_local:
                continue

            # Поднимаемся к карточке и читаем текст
            card_text = ""
            try:
                handle = a
                for _ in range(6):
                    parent = await handle.evaluate_handle("el => el.parentElement")
                    if not parent:
                        break
                    el = parent.as_element()
                    if el is None:
                        break
                    handle = el
                    txt = await el.inner_text()
                    # Признак, что это карточка листinga: есть цена и площадь/спальни
                    if ("$" in txt) and ("sqft" in txt.lower() or "bed" in txt.lower() or "/mo" in txt.lower()):
                        card_text = txt
                        break
            except Exception:
                pass

            if not card_text:
                try:
                    card_text = await a.inner_text()
                except Exception:
                    continue

            price = parse_price(card_text)
            if price is None:
                continue
            if not (PRICE_MIN <= price <= PRICE_MAX):
                continue

            full_url = href if href.startswith("http") else f"{domain}{href}"
            seen_ids_local.add(listing_id)
            results.append({
                "id": f"{site}#{listing_id}",
                "site": site,
                "price": price,
                "url": full_url,
                "phone": extract_phone(card_text),
            })
    except Exception as e:
        log(f"  ✗ {site}: ошибка загрузки ({type(e).__name__})")
    return results


async def check_all():
    found = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=USER_AGENT,
                                         viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        for site, url_tpl in SOURCES.items():
            url = url_tpl.format(pmin=PRICE_MIN, pmax=PRICE_MAX)
            log(f"  Проверяю сайт: {site}")
            listings = await fetch_listings(page, site, url)
            log(f"    {site}: найдено в диапазоне — {len(listings)}")
            found.extend(listings)
            await page.wait_for_timeout(2500)  # пауза между сайтами
        await browser.close()
    return found


def run_once(seen: set) -> int:
    found = asyncio.run(check_all())
    new = [l for l in found if l["id"] not in seen]

    if found:
        log(f"  Всего в диапазоне S${PRICE_MIN:,}–{PRICE_MAX:,}: {len(found)}, из них новых: {len(new)}")
    else:
        log("  В диапазоне ничего не найдено (или сайт заблокировал запрос)")

    for l in new:
        buttons = []
        if l.get("phone"):
            wa_url = build_whatsapp_url(l["phone"], l["url"])
            buttons.append({"text": "💬 Написать агенту в WhatsApp", "url": wa_url})
            buttons.append({"text": "🔗 Открыть листинг", "url": l["url"]})
            msg = (f"🏠 <b>Новая 4-спальная на Сентозе!</b>\n\n"
                   f"💰 S$ {l['price']:,} /мес\n"
                   f"🌐 Источник: {l['site']}\n\n"
                   f"Жми кнопку ниже — откроется WhatsApp с готовым сообщением агенту.")
        else:
            buttons.append({"text": "🔗 Открыть листинг (там телефон агента)", "url": l["url"]})
            msg = (f"🏠 <b>Новая 4-спальная на Сентозе!</b>\n\n"
                   f"💰 S$ {l['price']:,} /мес\n"
                   f"🌐 Источник: {l['site']}\n\n"
                   f"Телефон агента не удалось достать автоматически — открой листинг кнопкой ниже.\n\n"
                   f"<b>Готовый текст агенту (скопируй):</b>\n"
                   f"<code>{AGENT_MESSAGE}</code>")
        send_telegram(msg, buttons=buttons)
        log(f"  🔔 ОТПРАВЛЕНО: {l['site']} — S${l['price']:,} "
            f"({'WhatsApp' if l.get('phone') else 'листинг'})")
        seen.add(l["id"])

    if new:
        save_seen(seen)
    return len(new)


def main():
    log(f"Sentosa 4BR Watcher запущен | режим: {RUN_MODE}")
    log(f"Диапазон цен: S${PRICE_MIN:,}–{PRICE_MAX:,} | сайтов: {len(SOURCES)}")

    seen = load_seen()
    first_run = not seen
    if first_run:
        log("Первый запуск: текущие листинги пометятся как 'виденные', "
            "уведомления придут только на НОВЫЕ.")

    if RUN_MODE == "once":
        # Режим GitHub Actions: один прогон и выход.
        # При самом первом запуске (нет seen-файла) — помечаем текущее как виденное без спама.
        if first_run:
            found = asyncio.run(check_all())
            seen = {l["id"] for l in found}
            save_seen(seen)
            log(f"  Базовая инициализация: {len(seen)} текущих листингов записаны как виденные.")
        else:
            run_once(seen)
        log("Прогон завершён (RUN_MODE=once).")
        return

    # Режим loop: для мака/VPS
    send_telegram("✅ Sentosa Watcher запущен и следит за 4-спальными квартирами.")
    while True:
        try:
            log("── Проверка ──")
            run_once(seen)
        except KeyboardInterrupt:
            log("Остановлено пользователем.")
            sys.exit(0)
        except Exception as e:
            log(f"Непредвиденная ошибка цикла: {e}")
        log(f"Следующая проверка через {CHECK_INTERVAL_MINUTES} мин.\n")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()

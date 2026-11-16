#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер страницы судебного делопроизводства сайта sudrf.ru (ГС "Правосудие").

Разбирает HTML-ответ вида:
    https://<subdomain>.sudrf.ru/modules.php?name=sud_delo&name_op=case&...
и приводит его к единой JSON-структуре, описанной в файле case_schema.json.

Использование:
    python sudrf_parser.py                              # разбор дела по умолчанию
    python sudrf_parser.py <URL_дела>                   # разбор произвольного URL
    python sudrf_parser.py <URL_дела> -o result.json    # сохранить результат в файл
    python sudrf_parser.py --html page.html             # разбор сохранённого HTML-файла
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = (
    "https://moskovsky.twr.sudrf.ru/modules.php?name=sud_delo&name_op=case"
    "&_uid=d654c086-c0d1-427c-b9d8-5b353cdaf93c&_deloId=1540005"
    "&_caseType=0&_new=0&srv_num=3&_hideJudge=0"
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
TIME_RE = re.compile(r"\d{2}:\d{2}")


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

def clean(text: str | None) -> str:
    """Нормализует текст ячейки: неразрывные пробелы, схлопывание пробелов."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def norm_date(value: str) -> str | None:
    """'14.09.2026' -> '2026-09-14'; пустое/некорректное значение -> None."""
    value = clean(value)
    if DATE_RE.fullmatch(value):
        dd, mm, yyyy = value.split(".")
        return f"{yyyy}-{mm}-{dd}"
    return None


def norm_time(value: str) -> str | None:
    """'16:37' -> '16:37'; иначе None."""
    value = clean(value)
    return value if TIME_RE.fullmatch(value) else None


def split_datetime(value: str) -> tuple[str | None, str | None]:
    """Извлекает из строки дату и время (например '14.09.2026 18:21')."""
    m_date = DATE_RE.search(value or "")
    m_time = TIME_RE.search(value or "")
    return (norm_date(m_date.group()) if m_date else None,
            m_time.group() if m_time else None)


def table_to_pairs(table) -> list[tuple[str, str]]:
    """Двухколоночная таблица 'поле — значение' -> список пар."""
    pairs = []
    for tr in table.find_all("tr"):
        cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        if len(cells) >= 2 and cells[0]:
            pairs.append((cells[0], cells[1]))
    return pairs


def table_to_records(table) -> list[dict[str, str]]:
    """Таблица с шапкой -> список записей {заголовок_колонки: значение}."""
    rows = table.find_all("tr")
    if not rows:
        return []
    headers = [clean(th.get_text(" ")) for th in rows[0].find_all(["td", "th"])]
    records = []
    for tr in rows[1:]:
        cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        rec = {}
        for i, header in enumerate(headers):
            key = header.split(" Информация")[0]  # длинный подвал у колонки
            rec[key] = cells[i] if i < len(cells) else ""
        records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# Парсинг разделов
# --------------------------------------------------------------------------- #

def parse_court(soup, url: str) -> dict[str, Any]:
    """Сведения о суде (шапка сайта)."""
    court: dict[str, Any] = {"name": clean(soup.title.get_text()) if soup.title else None}

    h1 = soup.select_one(".header .logo, #maintitle, h1")
    if h1:
        name = clean(h1.get_text(" "))
        if name:
            court["name"] = name

    addr_block = (soup.find(id="show") or soup.find(id="addrblock")
                  or soup.find(id="address_assist"))
    if addr_block:
        lines = [clean(x) for x in addr_block.get_text("\n").split("\n") if clean(x)]
        lines = [x for x in lines if x not in ("развернуть", "свернуть")]
        address_parts, phones = [], []
        for line in lines:
            if line.lower().startswith("тел"):
                phones.append(re.sub(r"^Тел\.?\s*:?\s*", "", line, flags=re.I))
            elif "@" in line and "." in line and not line.startswith(("http", "www")):
                court["email"] = line
            else:
                address_parts.append(line)
        if address_parts:
            court["address"] = address_parts[0]
        if phones:
            court["phone"] = "; ".join(phones)
        mail = addr_block.find("a", href=re.compile(r"^mailto:", re.I))
        if mail:
            court["email"] = mail["href"][len("mailto:"):].strip()

    parsed = urlparse(url)
    if parsed.netloc:
        court["website"] = f"{parsed.scheme}://{parsed.netloc}"
    else:
        court["website"] = None
    return court


def parse_case(soup) -> dict[str, Any]:
    """Основная вкладка 'Дело' (div#tab_content_Case)."""
    container = soup.find(id="tab_content_Case") or soup.find(
        "div", class_="lawcase-tab-content")
    case: dict[str, Any] = {}

    num_div = (container.find(class_="case-num") if container else None) \
        or soup.find(class_="case-num")
    if num_div:
        raw = clean(num_div.get_text(" "))
        raw = re.sub(r"^ДЕЛО\s*№\s*", "", raw, flags=re.I)
        parts = [p.strip() for p in raw.split("~")]
        case["case_number"] = parts[0] or None
        if len(parts) > 1:
            case["registration_number"] = parts[1] or None

    inst_div = soup.find(class_="name-instanse")
    if inst_div:
        case["instance"] = clean(inst_div.get_text(" "))

    if container:
        table = container.find("table")
        if table:
            for key, value in table_to_pairs(table):
                if key == "Уникальный идентификатор дела":
                    case["uid"] = value
                elif key == "Дата поступления":
                    case["date_received"] = norm_date(value)
                elif key == "Категория дела":
                    case["case_category"] = value
                    case["case_category_parts"] = [
                        p.strip() for p in re.split(r"[→>]", value) if p.strip()
                    ]
                elif key == "Сущность заявленного требования":
                    case["claim_summary"] = value
                elif key == "Судья":
                    case["judge"] = value
                elif key == "Признак рассмотрения дела":
                    case["review_type"] = value

    publish = soup.find(class_="publishInfo")
    if publish:
        text = clean(publish.get_text(" "))
        m_pub = re.search(r"опубликовано\s+([\d.]+\s+\d{2}:\d{2})", text, re.I)
        m_chg = re.search(r"изменено\s+([\d.]+\s+\d{2}:\d{2})", text, re.I)
        if m_pub:
            d, t = split_datetime(m_pub.group(1))
            case["published_at"] = {"date": d, "time": t}
        if m_chg:
            d, t = split_datetime(m_chg.group(1))
            case["modified_at"] = {"date": d, "time": t}

    return case


def parse_events(soup) -> list[dict[str, Any]]:
    """Вкладка 'Движение дела' (div#tab_content_EventList)."""
    events: list[dict[str, Any]] = []
    container = soup.find(id="tab_content_EventList")
    if not container:
        return events
    table = container.find("table", class_="list") or container.find("table")
    if not table:
        return events

    for rec in table_to_records(table):
        event = {
            "name": rec.get("Наименование события") or None,
            "date": norm_date(rec.get("Дата", "")),
            "time": norm_time(rec.get("Время слушания", "")),
            "location": rec.get("Место проведения") or None,
            "result": rec.get("Результат события") or None,
            "reason": rec.get("Основание для выбранного результата события") or None,
            "note": rec.get("Примечание") or None,
            "published_at": norm_date(rec.get("Дата размещения", "")),
        }
        events.append(event)
    return events


def parse_parties(soup) -> list[dict[str, Any]]:
    """Вкладка 'Стороны' (div#tab_content_PersonList)."""
    parties: list[dict[str, Any]] = []
    container = soup.find(id="tab_content_PersonList")
    if not container:
        return parties

    table = container.find("table", class_="list")
    if table:
        for rec in table_to_records(table):
            party = {
                "type": (rec.get("Вид лица, участвующего в деле") or "").upper() or None,
                "name": rec.get("Фамилия / наименование") or None,
                "inn": rec.get("ИНН") or None,
                "kpp": rec.get("КПП") or None,
                "ogrn": rec.get("ОГРН") or None,
                "ogrnip": rec.get("ОГРНИП") or None,
            }
            parties.append(party)
        return parties

    # резервный вариант: мобильные карточки (таблицы поле-значение)
    for table in container.find_all("table", class_="mobile-lawcase-item") or []:
        data = dict(table_to_pairs(table))
        parties.append({
            "type": (data.get("Вид лица, участвующего в деле") or "").upper() or None,
            "name": data.get("Фамилия / наименование") or None,
            "inn": data.get("ИНН") or None,
            "kpp": data.get("КПП") or None,
            "ogrn": data.get("ОГРН") or None,
            "ogrnip": data.get("ОГРНИП") or None,
        })
    return parties


def build_result(html: str, url: str | None) -> dict[str, Any]:
    """Собирает итоговый JSON из HTML-страницы."""
    soup = BeautifulSoup(html, "lxml")

    source: dict[str, Any] = {"url": url}
    if url:
        parsed = urlparse(url)
        source["court_host"] = parsed.netloc or None
        qs = parsed.query
        for param in ("_uid", "_deloId", "_caseType", "srv_num"):
            m = re.search(rf"{param}=([^&]*)", qs)
            if m:
                source[param] = m.group(1)

    result = {
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "court": parse_court(soup, url or ""),
        "case": parse_case(soup),
        "case_movement": parse_events(soup),
        "parties": parse_parties(soup),
    }
    return result


# --------------------------------------------------------------------------- #
# Ввод/вывод
# --------------------------------------------------------------------------- #

def fetch_html(url: str, timeout: int = 30) -> str:
    """Загружает страницу и возвращает её HTML (с учётом кодировки sudrf.ru)."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    encoding = resp.apparent_encoding or "windows-1251"
    if encoding.lower() in ("ascii", "utf-8", "utf_8"):
        # сайты ГАС «Правосудие» отдают windows-1251 независимо от заголовков
        try:
            encoding = "windows-1251" if "windows-1251" in resp.text[:500] or \
                       "windows-1251" in resp.content[:500].decode("latin-1") else encoding
        except Exception:
            pass
    return resp.content.decode(encoding, errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("url", nargs="?", default=DEFAULT_URL,
                        help="URL страницы дела на sudrf.ru")
    parser.add_argument("--html", metavar="FILE",
                        help="разбор ранее сохранённого HTML-файла вместо загрузки по URL")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="файл для записи JSON (по умолчанию — stdout)")
    args = parser.parse_args(argv)

    if args.html:
        with open(args.html, "rb") as fh:
            raw = fh.read()
        html = raw.decode("cp1251", errors="replace")
    else:
        html = fetch_html(args.url)

    result = build_result(html, args.url if not args.html else None)
    text = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Сохранено: {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

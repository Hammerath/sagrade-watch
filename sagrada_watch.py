#!/usr/bin/env python3
"""
Sagrada Familia Ticket-Wächter
Prüft regelmäßig die offizielle Ticketseite und schickt eine Push-Nachricht
aufs Handy (über die kostenlose App "ntfy"), sobald ein passender Zeitslot frei wird.

Einrichtung (einmalig):
  pip install playwright requests
  playwright install chromium
  Benachrichtigung: Telegram-Bot (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) oder ntfy (NTFY_TOPIC)

Start:
  python sagrada_watch.py           normaler Betrieb (läuft bis zum letzten Wunschtermin)
  python sagrada_watch.py --once    nur ein Durchlauf, gut zum Testen
  python sagrada_watch.py --debug   sichtbarer Browser, Screenshots und API-Mitschnitt im Ordner ./debug
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- Einstellungen
URL = "https://tickets.sagradafamilia.org/en/1-individual/4375-sagrada-familia"

NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "sagrada-BITTE-AENDERN-7x3k9q"   # eigenen, schwer erratbaren Namen wählen!

# Telegram (empfohlen, zuverlässiger als ntfy). Wenn beides gesetzt ist, wird Telegram genutzt.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")                    # vom @BotFather, als GitHub-Secret
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "8965207955"     # Mikeys Chat-ID

INTERVAL_MIN = 5          # Minuten zwischen zwei Prüfungen
YEAR = 2026

# Datum -> frühester akzeptabler Startzeitpunkt ("HH:MM"), None = ganztags
TARGETS = {date(YEAR, 10, 18): None}
for d in range(19, 24):
    TARGETS[date(YEAR, 10, d)] = "15:00"      # hier ggf. "16:00" oder früher eintragen
for d in range(24, 31):
    TARGETS[date(YEAR, 10, d)] = None

# TEST: hier sind noch Slots frei, damit lässt sich prüfen, ob der Bot funktioniert.
# Nach erfolgreichem Test diese beiden Zeilen löschen.
TEST_DATES = {date(YEAR, 11, 9)}
TARGETS.update({d: None for d in TEST_DATES})

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "sagrada_state.json"
DEBUG_DIR = BASE / "debug"

TIME_RE = r"^\s*(\d{1,2})[:.](\d{2})"
SOLD_RE = r"sold\s*out|agotad|esgotad|complet|no\s+disponible|not\s+available|full"


# ---------------------------------------------------------------- Hilfsfunktionen
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title, message, priority=4, tags=("church",)):
    """Push-Nachricht über Telegram (falls eingerichtet), sonst über ntfy."""
    try:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"{title}\n\n{message}\n\n{URL}",
                "disable_notification": priority <= 2,   # Info-Nachrichten lautlos, Ticket-Alarm mit Ton
                "disable_web_page_preview": True,
            }, timeout=15)
            if not r.ok:
                log(f"Telegram-Fehler: {r.text}")
        else:
            requests.post(NTFY_SERVER, json={
                "topic": NTFY_TOPIC, "title": title, "message": message,
                "priority": priority, "tags": list(tags), "click": URL,
            }, timeout=15)
    except Exception as e:
        log(f"Push fehlgeschlagen: {e}")


def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_state(slots):
    STATE_FILE.write_text(json.dumps(sorted(slots), indent=1))


def dismiss_cookies(page):
    for pat in [r"accept", r"acept", r"accepta", r"akzept", r"agree", r"^ok$"]:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I))
            if btn.count():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


# Sucht im Kalender die Zelle für einen Tag, markiert sie und meldet, ob sie anklickbar ist.
JS_FIND_DAY = r"""
(day) => {
  document.querySelectorAll('[data-sfw]').forEach(e => e.removeAttribute('data-sfw'));
  const cls = e => { const c = e.className; return (c && c.baseVal !== undefined) ? c.baseVal : (c || ''); };
  const up = (el, n, fn) => { for (let e = el, i = 0; e && i < n; e = e.parentElement, i++) if (fn(e)) return true; return false; };
  const disabled = el => up(el, 3, e =>
      e.disabled || e.getAttribute('aria-disabled') === 'true' ||
      /disabled|unavailable|not-available|sold|blocked|past|inactive|no-?disp|locked/i.test(cls(e)))
      || getComputedStyle(el).pointerEvents === 'none';
  const otherMonth = el => up(el, 3, e => /outside|other-month|neighbo|prev-month|next-month|adjacent/i.test(cls(e)));
  const inCalendar = el => up(el, 8, e =>
      /calend|datepick|month|day-picker|daypicker/i.test(cls(e)) || e.getAttribute('role') === 'grid');
  const cands = [...document.querySelectorAll('button, td, [role=gridcell], [role=button], a, li, div, span')]
    .filter(el => el.offsetParent !== null && el.innerText && el.innerText.length < 25
                  && el.innerText.trim().split(/\s+/)[0] === String(day) && !otherMonth(el));
  if (!cands.length) return {found: false};
  const score = el => (inCalendar(el) ? 10 : 0) + (/^(BUTTON|TD)$/.test(el.tagName) || el.getAttribute('role') ? 3 : 0);
  cands.sort((a, b) => score(b) - score(a));
  const el = cands[0];
  el.setAttribute('data-sfw', 'day');
  return {found: true, disabled: disabled(el), calendar: inCalendar(el)};
}
"""

# Liest alle sichtbaren Uhrzeit-Slots aus und prüft, ob sie buchbar wirken.
JS_SLOTS = r"""
([timeRe, soldRe]) => {
  const tr = new RegExp(timeRe), sr = new RegExp(soldRe, 'i');
  const cls = e => { const c = e.className; return (c && c.baseVal !== undefined) ? c.baseVal : (c || ''); };
  const out = {};
  for (const el of document.querySelectorAll('button, label, li, a, div, span, [role=button], [role=radio], [role=option]')) {
    if (el.offsetParent === null || !el.innerText || el.innerText.length > 40) continue;
    const m = el.innerText.match(tr);
    if (!m || el.querySelector('button, label, li')) continue;
    const t = m[1].padStart(2, '0') + ':' + m[2];
    let bad = false;
    for (let e = el, i = 0; e && i < 3; e = e.parentElement, i++) {
      if (e.disabled || e.getAttribute('aria-disabled') === 'true' ||
          /disabled|unavailable|not-available|sold|full|complet|blocked|inactive/i.test(cls(e)) ||
          sr.test(e.innerText || '')) { bad = true; break; }
    }
    const st = getComputedStyle(el);
    if (st.pointerEvents === 'none' || parseFloat(st.opacity) < 0.5) bad = true;
    out[t] = (out[t] === true) || !bad;
  }
  return out;
}
"""


MONTH_NAMES = {
    10: r"october|octubre|oktober",
    11: r"november|noviembre|novembre",
    12: r"december|diciembre|desembre|dezember",
}


def goto_month(page, month):
    """Blättert im Kalender vorwärts, bis der gewünschte Monat angezeigt wird."""
    for _ in range(4):
        if re.search(MONTH_NAMES[month], page.inner_text("body"), re.I):
            return True
        nxt = page.get_by_role("button", name=re.compile(r"next|siguiente|seg[üu]ent|weiter", re.I))
        if not nxt.count():
            nxt = page.locator("[class*=next], [aria-label*=next i]")
        if not nxt.count():
            return False
        nxt.first.click()
        page.wait_for_timeout(1500)
    return bool(re.search(MONTH_NAMES[month], page.inner_text("body"), re.I))


ANY_MONTH = r"january|february|march|april|may|june|july|august|september|october|november|december"


def open_page(page, month):
    # Nicht auf "networkidle" warten: Die Seite lädt im Hintergrund ständig nach (Tracking usw.).
    page.goto(URL, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    # Warten, bis die App den Kalender gezeichnet hat (ein Monatsname taucht auf)
    try:
        page.wait_for_function(f"() => /{ANY_MONTH}/i.test(document.body.innerText)", timeout=45000)
    except Exception:
        log("Kalender nach 45 s noch nicht sichtbar")
    page.wait_for_timeout(1500)
    dismiss_cookies(page)
    return goto_month(page, month)


def diagnose(page, label):
    """Schreibt ins Protokoll, was die Seite gerade anzeigt, und speichert einen Screenshot."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
        text = re.sub(r"\s+", " ", page.inner_text("body"))[:600]
        log(f"Diagnose [{label}] URL: {page.url} | Titel: {page.title()!r}")
        log(f"Diagnose [{label}] Seitentext: {text!r}")
    except Exception as e:
        log(f"Diagnose nicht möglich: {e}")


def check_once(debug=False):
    """Gibt (Menge freier passender Slots, Liste von Hinweisen) zurück."""
    today = date.today()
    free, notes = set(), []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not debug)
        ctx = browser.new_context(
            locale="en-GB", timezone_id="Europe/Madrid",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        capture = debug or os.environ.get("GITHUB_ACTIONS") == "true"   # auf GitHub immer mitschneiden

        if capture:
            DEBUG_DIR.mkdir(exist_ok=True)
            counter = {"n": 0}

            def on_response(resp):
                if "json" in (resp.headers.get("content-type") or ""):
                    counter["n"] += 1
                    try:
                        (DEBUG_DIR / f"api_{counter['n']:03}.json").write_text(
                            json.dumps({"url": resp.url, "body": resp.json()}, indent=1, ensure_ascii=False))
                    except Exception:
                        pass
            page.on("response", on_response)

        current_month = None
        found_any = False
        try:
            for d, earliest in sorted(TARGETS.items()):
                if d < today:
                    continue
                if d.month != current_month:              # neuer Monat: Seite frisch laden und hinblättern
                    current_month = d.month
                    if not open_page(page, d.month):
                        notes.append(f"{d:%m.%Y}: Monat im Kalender nicht gefunden")
                    if capture:
                        page.screenshot(path=str(DEBUG_DIR / f"00_monat_{d.month}.png"), full_page=True)
                info = page.evaluate(JS_FIND_DAY, d.day)
                if not info.get("found"):
                    open_page(page, d.month)              # evtl. hat ein Klick die Ansicht gewechselt
                    info = page.evaluate(JS_FIND_DAY, d.day)
                if not info.get("found"):
                    notes.append(f"{d:%d.%m.}: Tag im Kalender nicht gefunden")
                    continue
                found_any = True
                if info.get("disabled"):
                    log(f"{d:%d.%m.}: ausgebucht / gesperrt")
                    continue

                page.click("[data-sfw=day]")
                page.wait_for_timeout(2500)
                slots = page.evaluate(JS_SLOTS, [TIME_RE, SOLD_RE])
                if capture:
                    page.screenshot(path=str(DEBUG_DIR / f"{d:%m%d}.png"), full_page=True)

                ok = sorted(t for t, avail in slots.items() if avail and (earliest is None or t >= earliest))
                log(f"{d:%d.%m.}: {len(slots)} Slots erkannt, passend frei: {', '.join(ok) or '-'}")
                if not slots:
                    notes.append(f"{d:%d.%m.}: Tag anklickbar, aber keine Uhrzeiten erkannt")
                for t in ok:
                    free.add(f"{d.isoformat()} {t}")

        except Exception:
            diagnose(page, "fehler")
            browser.close()
            raise
        if not found_any:
            diagnose(page, "kein_tag_gefunden")
        browser.close()
    if not found_any:
        raise RuntimeError("Kein einziger Kalendertag gefunden, Seitenaufbau hat sich wohl geändert. "
                           "Mit --debug starten und Screenshots prüfen.")
    return free, notes


# ---------------------------------------------------------------- Hauptprogramm
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) and "BITTE-AENDERN" in NTFY_TOPIC:
        sys.exit("Bitte zuerst Telegram (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) oder NTFY_TOPIC einrichten.")

    last_free = load_state()
    errors = 0
    if not args.once:
        notify("Sagrada-Wächter gestartet",
               f"Beobachte {len(TARGETS)} Tage, Prüfung alle {INTERVAL_MIN} Min.", priority=2, tags=("robot",))

    while True:
        if date.today() > max(TARGETS):
            if args.once:                     # auf GitHub: still beenden, kein Push bei jedem Lauf
                log("Alle Wunschtermine vorbei, nichts mehr zu tun.")
                break
            notify("Sagrada-Wächter beendet", "Alle Wunschtermine liegen in der Vergangenheit.", priority=2)
            break
        try:
            free, notes = check_once(debug=args.debug)
            errors = 0
            for n in notes:
                log("Hinweis: " + n)
            new = sorted(free - last_free)
            if new:
                def fmt(slot):
                    dt = datetime.strptime(slot, "%Y-%m-%d %H:%M")
                    return dt.strftime("%a %d.%m. %H:%M") + (" (Test-Termin)" if dt.date() in TEST_DATES else "")
                lines = "\n".join(fmt(x) for x in new)
                log("NEU FREI:\n" + lines)
                notify("🎟️ Sagrada Familia: Slot frei!",
                       f"{lines}\n\nSchnell buchen (2 Tickets), tippen öffnet die Seite.", priority=5,
                       tags=("tada", "church"))
            last_free = free
            save_state(free)
        except Exception as e:
            errors += 1
            log(f"Fehler ({errors}): {e}")
            if args.once:
                sys.exit(1)          # GitHub markiert den Lauf dann als fehlgeschlagen und schickt eine Mail
            if errors == 3:
                notify("Sagrada-Wächter hat Probleme", f"3 Fehler in Folge: {e}", priority=3, tags=("warning",))

        if args.once:
            break
        time.sleep(INTERVAL_MIN * 60 + random.randint(-30, 60))


if __name__ == "__main__":
    main()

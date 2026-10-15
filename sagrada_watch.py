#!/usr/bin/env python3
"""
Sagrada Familia Ticket-Wächter
Prüft regelmäßig die offizielle Ticketseite und schickt eine Push-Nachricht
aufs Handy (über die kostenlose App "ntfy"), sobald ein passender Zeitslot frei wird.

Einrichtung (einmalig):
  pip install playwright requests
  playwright install chromium
  Handy: App "ntfy" installieren, "+" tippen, den Topic-Namen aus NTFY_TOPIC abonnieren

Start:
  python sagrada_watch.py           normaler Betrieb (läuft bis zum letzten Wunschtermin)
  python sagrada_watch.py --once    nur ein Durchlauf, gut zum Testen
  python sagrada_watch.py --debug   sichtbarer Browser, Screenshots und API-Mitschnitt im Ordner ./debug
"""

import argparse
import json
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
NTFY_TOPIC = "sagrada-mikey-lisa-k93jf2"   # eigenen, schwer erratbaren Namen wählen!

INTERVAL_MIN = 5          # Minuten zwischen zwei Prüfungen
YEAR = 2026

# Datum -> frühester akzeptabler Startzeitpunkt ("HH:MM"), None = ganztags
TARGETS = {date(YEAR, 10, 18): None}
for d in range(19, 24):
    TARGETS[date(YEAR, 10, d)] = "15:00"      # hier ggf. "16:00" oder früher eintragen
for d in range(24, 31):
    TARGETS[date(YEAR, 10, d)] = None

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "sagrada_state.json"
DEBUG_DIR = BASE / "debug"

TIME_RE = r"^\s*(\d{1,2})[:.](\d{2})"
SOLD_RE = r"sold\s*out|agotad|esgotad|complet|no\s+disponible|not\s+available|full"


# ---------------------------------------------------------------- Hilfsfunktionen
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title, message, priority=4, tags=("church",)):
    """Push-Nachricht über ntfy (JSON-Variante, damit Umlaute sicher ankommen)."""
    try:
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


def ensure_october(page):
    for _ in range(3):
        if re.search(r"october|octubre|octubre|oktober", page.inner_text("body"), re.I):
            return
        nxt = page.get_by_role("button", name=re.compile(r"next|siguiente|seg[üu]ent|weiter", re.I))
        if not nxt.count():
            return
        nxt.first.click()
        page.wait_for_timeout(1500)


def open_page(page):
    page.goto(URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    dismiss_cookies(page)
    ensure_october(page)


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

        if debug:
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

        open_page(page)
        if debug:
            page.screenshot(path=str(DEBUG_DIR / "00_start.png"), full_page=True)

        found_any = False
        for d, earliest in sorted(TARGETS.items()):
            if d < today:
                continue
            info = page.evaluate(JS_FIND_DAY, d.day)
            if not info.get("found"):
                open_page(page)                       # evtl. hat ein Klick die Ansicht gewechselt
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
            if debug:
                page.screenshot(path=str(DEBUG_DIR / f"{d:%m%d}.png"), full_page=True)

            ok = sorted(t for t, avail in slots.items() if avail and (earliest is None or t >= earliest))
            log(f"{d:%d.%m.}: {len(slots)} Slots erkannt, passend frei: {', '.join(ok) or '-'}")
            if not slots:
                notes.append(f"{d:%d.%m.}: Tag anklickbar, aber keine Uhrzeiten erkannt")
            for t in ok:
                free.add(f"{d.isoformat()} {t}")

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

    if "BITTE-AENDERN" in NTFY_TOPIC:
        sys.exit("Bitte zuerst NTFY_TOPIC im Skript auf einen eigenen Namen setzen.")

    last_free = load_state()
    errors = 0
    notify("Sagrada-Wächter gestartet",
           f"Beobachte {len(TARGETS)} Tage, Prüfung alle {INTERVAL_MIN} Min.", priority=2, tags=("robot",))

    while True:
        if date.today() > max(TARGETS):
            notify("Sagrada-Wächter beendet", "Alle Wunschtermine liegen in der Vergangenheit.", priority=2)
            break
        try:
            free, notes = check_once(debug=args.debug)
            errors = 0
            for n in notes:
                log("Hinweis: " + n)
            new = sorted(free - last_free)
            if new:
                lines = "\n".join(datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%a %d.%m. %H:%M") for s in new)
                log("NEU FREI:\n" + lines)
                notify("🎟️ Sagrada Familia: Slot frei!",
                       f"{lines}\n\nSchnell buchen (2 Tickets), tippen öffnet die Seite.", priority=5,
                       tags=("tada", "church"))
            last_free = free
            save_state(free)
        except Exception as e:
            errors += 1
            log(f"Fehler ({errors}): {e}")
            if errors == 3:
                notify("Sagrada-Wächter hat Probleme", f"3 Fehler in Folge: {e}", priority=3, tags=("warning",))

        if args.once:
            break
        time.sleep(INTERVAL_MIN * 60 + random.randint(-30, 60))


if __name__ == "__main__":
    main()
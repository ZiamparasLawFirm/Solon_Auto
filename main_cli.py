# -*- coding: utf-8 -*-
"""
ΣΟΛΩΝ Αυτόματες ενημερώσεις — CLI Batch v1.0.0
----------------------------------------------
Χρήση:
    python cli_batch.py SOLON_INPUT.xlsx

Τι κάνει:
- Διαβάζει πελάτες από Excel (κεφαλίδες: Πελάτης, Δικαστήριο, Γ.Α.Κ. Αριθμός, Γ.Α.Κ. Έτος).
- Εκτελεί αναζήτηση στο SOLON και τυπώνει ΑΡΙΘΜΗΜΕΝΑ αποτελέσματα στην κονσόλα
  με τη μορφή:
      1. ΟΝΟΜΑ — ΔΙΚΑΣΤΗΡΙΟ — ΓΑΚ num/έτος
         Αριθμός Aπόφασης/'Ετος - Είδος Διατακτικού: <αποτέλεσμα ή — κενό —> (email ok/failed...)
- Στέλνει email ΜΟΝΟ όταν υπάρχει ουσιαστικό αποτέλεσμα.

Απαιτήσεις:
    pip install playwright pandas openpyxl python-dotenv
    python -m playwright install

Ρυθμίσεις στο .env (δίπλα στο cli_batch.py):
    SENDER_EMAIL=...
    RECEIVER_EMAIL=...
    GOOGLE_APP_PASSWORD=...    # ή GMAIL_APP_PASSWORD / APP_PASSWORD

    # Απόδοση:
    HEADLESS=1
    BLOCK_MEDIA=1
    FAST_MODE=1
    RESULT_TIMEOUT_MS=60000
    EARLY_NO_DATA_MS=4000

    # Παράλληλα browsers (προεπιλογή 4):
    BATCH_WORKERS=4

    # Excel (προαιρετικά):
    EXCEL_SHEET=Sheet1
    COL_CLIENT=Πελάτης
    COL_COURT=Δικαστήριο
    COL_GAK_NUM=Γ.Α.Κ. Αριθμός
    COL_GAK_YEAR=Γ.Α.Κ. Έτος

    # Διαγνωστικά:
    DEBUG_ARTIFACTS=0
"""

import sys, os, json, time, re, unicodedata, smtplib
from email.message import EmailMessage
from queue import Queue
from threading import Thread

import pandas as pd
from dotenv import load_dotenv, find_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import subprocess

import time, atexit

_t=time.perf_counter()
# ------------------------ Περιβάλλον ------------------------ #
load_dotenv(find_dotenv(), override=True)

URL = "https://extapps.solon.gov.gr/mojwp/faces/TrackLdoPublic"

# ADF selectors (πρόσεχε τα ':' → CSS escapes)
SEL_KATASTIMA   = "#courtOfficeOC\\:\\:content"
SEL_GAK_NUMBER  = "#it1\\:\\:content"
SEL_GAK_YEAR    = "#it2\\:\\:content"
SEL_SEARCH_BTN  = "#ldoSearch a"

# Grid / spinner
SEL_GRID        = "#pc1\\:ldoTable"
SEL_GRID_DB     = "#pc1\\:ldoTable\\:\\:db"
SEL_GRID_HDR    = "#pc1\\:ldoTable\\:\\:hdr"
SEL_GRID_SPIN   = "#pc1\\:ldoTable\\:\\:sm"
SEL_GRID_TABLE  = "#pc1\\:ldoTable"

DEFAULT_TIMEOUT   = 30_000
RESULT_TIMEOUT    = int(os.getenv("RESULT_TIMEOUT_MS", "60000"))
EARLY_NO_DATA_MS  = int(os.getenv("EARLY_NO_DATA_MS", "4000"))

EXCEL_SHEET   = os.getenv("EXCEL_SHEET")
BATCH_WORKERS = max(1, int(os.getenv("BATCH_WORKERS", "4")))

RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "dimitris.ziamparas@gmail.com")
SENDER_EMAIL   = os.getenv("SENDER_EMAIL",   RECEIVER_EMAIL)
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "465"))
APP_PASSWORD   = (
    os.getenv("GOOGLE_APP_PASSWORD")
    or os.getenv("GMAIL_APP_PASSWORD")
    or os.getenv("APP_PASSWORD")
)

HEADLESS        = os.getenv("HEADLESS", "1").lower() not in ("0","false","no")
BLOCK_MEDIA     = os.getenv("BLOCK_MEDIA", "0").lower() in ("1","true","yes")
FAST_MODE       = os.getenv("FAST_MODE", "0").lower() in ("1","true","yes")
DEBUG_ARTIFACTS = os.getenv("DEBUG_ARTIFACTS", "0").lower() in ("1","true","yes")

ENV_COL_CLIENT = os.getenv("COL_CLIENT")
ENV_COL_COURT  = os.getenv("COL_COURT")
ENV_COL_GAKNUM = os.getenv("COL_GAK_NUM")
ENV_COL_GAKYEAR= os.getenv("COL_GAK_YEAR")

# ------------------------ Utils ------------------------ #
def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.replace("\u200b", "")
    s = re.sub(r"[^\w\s\u0370-\u03FF]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def _looks_like_header(text: str) -> bool:
    n = _normalize(text)
    return ("αριθμος αποφασης" in n) and ("ειδος διατακτικου" in n)

def _is_meaningful_result(text):
    t = (text or "").strip()
    if not t: return False
    if _looks_like_header(t): return False
    return True

def _ensure_artifacts_dir():
    if DEBUG_ARTIFACTS and not os.path.exists("artifacts"):
        os.makedirs("artifacts", exist_ok=True)

def _dump_dom(page, base_name: str):
    if not DEBUG_ARTIFACTS: return
    _ensure_artifacts_dir()
    try:
        hdr = page.evaluate("(sel)=>{const n=document.querySelector(sel); return n? n.outerHTML : ''}", SEL_GRID_HDR)
        db  = page.evaluate("(sel)=>{const n=document.querySelector(sel); return n? n.outerHTML : ''}", SEL_GRID_DB)
        tbl = page.evaluate("(sel)=>{const n=document.querySelector(sel); return n? n.outerHTML : ''}", SEL_GRID_TABLE)
        with open(f"artifacts/{base_name}_hdr.html", "w", encoding="utf-8") as f: f.write(hdr or "")
        with open(f"artifacts/{base_name}_db.html",  "w", encoding="utf-8") as f: f.write(db or "")
        with open(f"artifacts/{base_name}_table.html","w", encoding="utf-8") as f: f.write(tbl or "")
    except Exception:
        pass

def _route_blocker(route):
    try:
        if route.request.resource_type in ("image","media","font"):
            return route.abort()
        return route.continue_()
    except Exception:
        try: route.continue_()
        except Exception: pass

def _accept_cookies_if_present(page):
    for loc in [
        page.get_by_role("button", name="Αποδοχή"),
        page.get_by_role("button", name="Αποδέχομαι"),
        page.get_by_role("button", name="Συμφωνώ"),
        page.get_by_role("button", name="Accept"),
        page.get_by_role("button", name="Accept all"),
    ]:
        try:
            if loc.count() and loc.first.is_visible() and loc.first.is_enabled():
                loc.first.click(); page.wait_for_timeout(150); break
        except Exception:
            pass

def _attach_dialog_autoaccept(page):
    try:
        page.on("dialog", lambda d: (d.accept() if hasattr(d, "accept") else None))
    except Exception:
        pass

def _get_db_text(page) -> str:
    try:
        return page.evaluate("(sel)=>{const n=document.querySelector(sel); return n?(n.textContent||'').trim():'';}", SEL_GRID_DB)
    except Exception:
        return ""

def _wait_spinner_cycle_if_any(page):
    try: page.wait_for_selector(SEL_GRID_SPIN, state="visible", timeout=2_000)
    except Exception: pass
    try: page.wait_for_selector(SEL_GRID_SPIN, state="hidden", timeout=RESULT_TIMEOUT)
    except Exception: pass

def _wait_for_table_ready(page, timeout_ms=RESULT_TIMEOUT):
    page.wait_for_selector(SEL_GRID, state="visible", timeout=DEFAULT_TIMEOUT)
    page.wait_for_function(
        """
        (dbSel) => {
            const db = document.querySelector(dbSel);
            if (!db) return false;
            const txt = (db.textContent || "").trim();
            const hasNoData = txt.includes("Δεν υπάρχουν δεδομένα");
            const hasTd = !!db.querySelector("td");
            return hasNoData || hasTd;
        }
        """,
        arg=SEL_GRID_DB,
        timeout=timeout_ms
    )

def _wait_for_table_change(page, prev_sig: str, timeout_ms=RESULT_TIMEOUT):
    try:
        page.wait_for_function(
            """
            (args) => {
              const [dbSel, prev] = args;
              const db = document.querySelector(dbSel);
              if (!db) return false;
              const now = (db.textContent || "").trim();
              return now && now !== prev;
            }
            """,
            arg=[SEL_GRID_DB, prev_sig],
            timeout=timeout_ms // 2
        )
    except Exception:
        pass

def _wait_clickable(page, selector: str, timeout_ms=5_000):
    page.locator(selector).scroll_into_view_if_needed()
    page.wait_for_function(
        """
        (sel) => {
          const el = document.querySelector(sel);
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const cx = r.left + r.width/2;
          const cy = r.top  + r.height/2;
          const top = document.elementFromPoint(cx, cy);
          return top && (top === el || el.contains(top));
        }
        """,
        arg=selector,
        timeout=timeout_ms
    )

def _set_input_value(page, selector: str, value: str):
    try: _wait_clickable(page, selector, timeout_ms=5_000)
    except Exception: pass
    ok = False
    try:
        ok = page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return false;
                el.focus(); el.value = '';
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.value = String(args.val ?? '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }""",
            {"sel": selector, "val": str(value)}
        ) or False
        page.wait_for_timeout(40)
        try: page.locator(selector).press("Tab")
        except Exception: pass
        page.wait_for_timeout(60)
    except Exception: ok = False
    if ok: return
    loc = page.locator(selector)
    try:
        loc.fill(str(value)); page.wait_for_timeout(60)
        try: loc.press("Tab")
        except Exception: pass
        return
    except Exception: pass
    try:
        loc.click(force=True); loc.press("Control+A"); loc.press("Delete")
        if FAST_MODE: loc.type(str(value))
        else: loc.type(str(value), delay=10)
        page.wait_for_timeout(60)
        try: loc.press("Tab")
        except Exception: pass
    except Exception:
        page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return false;
                el.value = String(args.val ?? '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }""",
            {"sel": selector, "val": str(value)}
        )
        page.wait_for_timeout(60)

def _click_search(page):
    btn = page.locator(SEL_SEARCH_BTN).first
    try: _wait_clickable(page, SEL_SEARCH_BTN, timeout_ms=5_000)
    except Exception: pass
    for _ in range(3):
        try:
            btn.click(timeout=2000); return True
        except Exception:
            _wait_spinner_cycle_if_any(page); page.wait_for_timeout(120)
    try:
        page.evaluate("(sel)=>{const el=document.querySelector(sel); if(el){el.focus(); el.click();}}", SEL_SEARCH_BTN)
        return True
    except Exception: pass
    try:
        page.locator(SEL_GAK_YEAR).focus(); page.keyboard.press("Enter"); return True
    except Exception: pass
    btn.click(timeout=2000, force=True); return True

def _dismiss_known_overlay(page):
    ok_labels = ["OK", "ΟΚ", "Ok", "ok"]
    msg_snippets = [
        "Δεν βρέθηκαν δεδομένα",
        "Δεν υπάρχουν αποτελέσματα",
        "Ελέγξτε τα κριτήρια",
    ]
    try:
        for name in ok_labels:
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                btn.first.click(); page.wait_for_timeout(100); return True
        for snippet in msg_snippets:
            loc = page.locator(f"text={snippet}")
            if loc.count() and loc.first.is_visible():
                btn = page.locator("//button[normalize-space()='OK' or normalize-space()='ΟΚ']")
                if btn.count() and btn.first.is_visible():
                    btn.first.click(); page.wait_for_timeout(100); return True
        try:
            page.keyboard.press("Enter"); page.wait_for_timeout(80); return True
        except Exception: pass
        try:
            page.keyboard.press("Escape"); page.wait_for_timeout(80); return True
        except Exception: pass
    except Exception:
        pass
    return False

# ------------- Court list ------------- #
def _build_court_map(page):
    options = page.locator(f"{SEL_KATASTIMA} option")
    texts   = options.all_text_contents()
    values  = options.evaluate_all("els => els.map(e => e.value)")
    out = {}
    for t, v in zip(texts, values):
        out[_normalize(t)] = (t.strip(), v)
    return out

def _get_court_value(court_map, label):
    n = _normalize(label)
    if n in court_map:
        return court_map[n][1]
    for key,(t,v) in court_map.items():
        if n and n in key: return v
    raise ValueError("Δεν βρέθηκε το ζητούμενο δικαστήριο στη λίστα του SOLON.")

# ------------- Ανάγνωση από τον πίνακα ------------- #
def _wait_for_target_row_and_read(page, gak_num: str, gak_year: str, timeout_ms=RESULT_TIMEOUT) -> str:
    js = r"""
    (args) => {
      const { dbSel, num, year } = args;
      const db = document.querySelector(dbSel);
      if (!db) return { found:false };

      const norm = s => (s||'').toString().replace(/\u00A0/g,' ').replace(/\s+/g,' ').trim();
      const needleNum  = norm(num);
      const needleYear = norm(year);
      const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
      const rxCombined = new RegExp('^\\s*'+esc(needleNum)+'\\s*/\\s*'+esc(needleYear)+'\\s*$');

      const rows = Array.from(db.querySelectorAll('tr'));
      for (const tr of rows) {
        const tds = Array.from(tr.querySelectorAll('td'));
        if (!tds.length) continue;

        const decTd = tr.querySelector("td[id$=':c10']");
        const otherTds = tds.filter(td => td !== decTd);
        const texts = otherTds.map(td => norm(td.innerText));

        const hasNumExact  = texts.some(t => t === needleNum);
        const hasYearExact = texts.some(t => t === needleYear);
        const hasCombined  = texts.some(t => rxCombined.test(t));

        if ((hasNumExact && hasYearExact) || hasCombined) {
          const val = decTd ? norm(decTd.innerText) : "";
          return { found:true, value: val };
        }
      }
      const noData = (db.textContent||'').includes('Δεν υπάρχουν δεδομένα');
      return { found:false, noData };
    }
    """
    deadline = time.time() + (timeout_ms/1000.0)
    first_no_data_at = None
    while time.time() < deadline:
        res = page.evaluate(js, {"dbSel": SEL_GRID_DB, "num": str(gak_num).strip(), "year": str(gak_year).strip()})
        if res and res.get("found"):
            val = (res.get("value") or "").strip()
            if _is_meaningful_result(val): return val
        if res and res.get("noData"):
            if FAST_MODE:
                if first_no_data_at is None: first_no_data_at = time.time()
                elif (time.time() - first_no_data_at) * 1000 >= EARLY_NO_DATA_MS: return ""
        else:
            first_no_data_at = None
        _wait_spinner_cycle_if_any(page)
        page.wait_for_timeout(200 if FAST_MODE else 300)
    return ""

# ------------- Scrape helpers (batch) ------------- #
def _prepare_page_for_batch(context):
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT)
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    _attach_dialog_autoaccept(page)
    _accept_cookies_if_present(page)
    court_map = _build_court_map(page)
    return page, court_map

def _search_on_prepared_page(page, court_value: str, gak_num: str, gak_year: str):
    base_art = f"after_{gak_num}_{gak_year}"
    try:
        current = page.locator(SEL_KATASTIMA).evaluate("el => el.value")
        if current != court_value:
            page.select_option(SEL_KATASTIMA, value=court_value)
            page.wait_for_timeout(60)

        _set_input_value(page, SEL_GAK_NUMBER, str(gak_num).strip())
        _set_input_value(page, SEL_GAK_YEAR,   str(gak_year).strip())

        prev_sig = _get_db_text(page)
        _click_search(page)
        _dismiss_known_overlay(page)

        _wait_for_table_ready(page, timeout_ms=RESULT_TIMEOUT)
        _wait_for_table_change(page, prev_sig, timeout_ms=RESULT_TIMEOUT)
        _wait_spinner_cycle_if_any(page)
        _dismiss_known_overlay(page)

        result = _wait_for_target_row_and_read(page, gak_num, gak_year, timeout_ms=RESULT_TIMEOUT)
        return {"ok": True, "result": result}
    finally:
        if DEBUG_ARTIFACTS: _dump_dom(page, base_art)

# ---------------- Email ---------------- #
def _send_email(subject: str, body: str):
    if not APP_PASSWORD:
        return (False, "Λείπει το GOOGLE_APP_PASSWORD/GMAIL_APP_PASSWORD στο .env.")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.login(SENDER_EMAIL, APP_PASSWORD)
            msg = EmailMessage()
            msg["From"] = SENDER_EMAIL
            msg["To"] = RECEIVER_EMAIL
            msg["Subject"] = subject
            msg.set_content(body)
            smtp.send_message(msg)
        return (True, "email ok")
    except Exception as e:
        return (False, f"email failed: {e}")

# ---------------- Excel ---------------- #
HEADER_ALIASES = {
    "Πελάτης": {"πελατης","pelatis","client","customer","onoma","onoma pelati","pelaths","pelatis name","onoma pelath","πελάτης"},
    "Δικαστήριο": {"δικαστηριο","dikasthrio","court","δικαστήριο"},
    "Γ.Α.Κ. Αριθμός": {"γακ αριθμος","gak αριθμος","g a k αριθμος","g a k number","g.a.k αριθμος","gak number","gak no","γακ αριθμος","γακ"},
    "Γ.Α.Κ. Έτος": {"γακ ετος","gak ετος","g a k ετος","gak year","g a k year","ετος"},
}

def _normalize_header_map(df: pd.DataFrame):
    norm_cols = {col: _normalize(str(col)) for col in df.columns}
    mapping = {}
    for canonical, aliases in HEADER_ALIASES.items():
        found = None
        for col, norm in norm_cols.items():
            if norm in aliases:
                found = col; break
        if not found: return None
        mapping[canonical] = found
    return mapping

def _try_read_sheet_with_header_guess(xls: pd.ExcelFile, sheet_name):
    df0 = pd.read_excel(xls, sheet_name=sheet_name, engine="openpyxl", dtype=str).fillna("")
    mapping = _normalize_header_map(df0)
    if mapping:
        return df0[[mapping["Πελάτης"], mapping["Δικαστήριο"], mapping["Γ.Α.Κ. Αριθμός"], mapping["Γ.Α.Κ. Έτος"]]] \
            .rename(columns={v: k for k, v in mapping.items()}) \
            .to_dict(orient="records")
    df_raw = pd.read_excel(xls, sheet_name=sheet_name, engine="openpyxl", dtype=str, header=None).fillna("")
    max_rows = min(10, len(df_raw))
    for hdr in range(max_rows):
        test = pd.read_excel(xls, sheet_name=sheet_name, engine="openpyxl", dtype=str, header=hdr).fillna("")
        mapping = _normalize_header_map(test)
        if mapping:
            return test[[mapping["Πελάτης"], mapping["Δικαστήριο"], mapping["Γ.Α.Κ. Αριθμός"], mapping["Γ.Α.Κ. Έτος"]]] \
                .rename(columns={v: k for k, v in mapping.items()}) \
                .to_dict(orient="records")
    raise ValueError(f"Δεν βρέθηκαν οι απαιτούμενες κεφαλίδες στο φύλλο «{sheet_name}». "
                     f"Αναμενόμενες: Πελάτης, Δικαστήριο, Γ.Α.Κ. Αριθμός, Γ.Α.Κ. Έτος.")

def _load_excel_rows_with_env_mapping(path: str):
    if not (ENV_COL_CLIENT and ENV_COL_COURT and ENV_COL_GAKNUM and ENV_COL_GAKYEAR):
        raise ValueError("Incomplete .env mapping.")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Δεν βρέθηκε το {path}")
    df = pd.read_excel(path, engine="openpyxl", dtype=str, sheet_name=EXCEL_SHEET).fillna("")
    if not set([ENV_COL_CLIENT, ENV_COL_COURT, ENV_COL_GAKNUM, ENV_COL_GAKYEAR]).issubset(set(df.columns)):
        df_all = pd.read_excel(path, engine="openpyxl", dtype=str, sheet_name=EXCEL_SHEET, header=None)
        raw = df_all.fillna("").astype(str)
        header_idx = 0
        target = [_normalize(x) for x in [ENV_COL_CLIENT, ENV_COL_COURT, ENV_COL_GAKNUM, ENV_COL_GAKYEAR]]
        for r in range(min(10, len(raw))):
            row_norm = [_normalize(x) for x in list(raw.iloc[r])]
            hits = sum(t in row_norm for t in target)
            if hits >= 2:
                header_idx = r; break
        df = pd.read_excel(path, engine="openpyxl", dtype=str, sheet_name=EXCEL_SHEET, header=header_idx).fillna("")
    missing = [c for c in [ENV_COL_CLIENT, ENV_COL_COURT, ENV_COL_GAKNUM, ENV_COL_GAKYEAR] if c not in df.columns]
    if missing:
        raise ValueError("Με .env mapping, λείπουν στήλες: " + ", ".join(missing))
    sub = df[[ENV_COL_CLIENT, ENV_COL_COURT, ENV_COL_GAKNUM, ENV_COL_GAKYEAR]].rename(columns={
        ENV_COL_CLIENT: "Πελάτης",
        ENV_COL_COURT:  "Δικαστήριο",
        ENV_COL_GAKNUM: "Γ.Α.Κ. Αριθμός",
        ENV_COL_GAKYEAR:"Γ.Α.Κ. Έτος",
    })
    return sub.to_dict(orient="records")

def _load_excel_rows(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Δεν βρέθηκε το {path}")
    if ENV_COL_CLIENT and ENV_COL_COURT and ENV_COL_GAKNUM and ENV_COL_GAKYEAR:
        return _load_excel_rows_with_env_mapping(path)
    xls = pd.ExcelFile(path, engine="openpyxl")
    if EXCEL_SHEET:
        return _try_read_sheet_with_header_guess(xls, EXCEL_SHEET)
    errors = []
    for sheet in xls.sheet_names:
        try:
            return _try_read_sheet_with_header_guess(xls, sheet)
        except Exception as e:
            errors.append(f"{sheet}: {e}")
    raise ValueError("Δεν εντοπίστηκαν οι απαιτούμενες στήλες σε κανένα φύλλο του Excel.\n" + "\n".join(errors))

# ------------------------ Main (CLI) ------------------------ #
def main():
    if len(sys.argv) != 2:
        print("Χρήση: python cli_batch.py <αρχείο_excel.xlsx>")
        sys.exit(2)
    excel_path = sys.argv[1]

    try:
        rows = _load_excel_rows(excel_path)
    except Exception as e:
        print(f"Σφάλμα ανάγνωσης Excel: {e}")
        sys.exit(1)

    in_q  = Queue()
    out_q = Queue()

    for i, r in enumerate(rows, start=1):
        in_q.put((i, r))
    for _ in range(BATCH_WORKERS):
        in_q.put(None)

    def worker(worker_id: int):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(locale="el-GR", viewport={"width":1500,"height":950})
            if BLOCK_MEDIA:
                context.route("**/*", _route_blocker)
            try:
                page, court_map = _prepare_page_for_batch(context)
                while True:
                    item = in_q.get()
                    if item is None:
                        break
                    row_index, r = item
                    payload = {**r, "_row_index": row_index}
                    try:
                        court_value = _get_court_value(court_map, r.get("Δικαστήριο",""))
                        res = _search_on_prepared_page(
                            page, court_value,
                            r.get("Γ.Α.Κ. Αριθμός",""),
                            r.get("Γ.Α.Κ. Έτος","")
                        )
                        payload.update(res)

                        if DEBUG_ARTIFACTS:
                            _ensure_artifacts_dir()
                            base = f"cli_row_{row_index}_{r.get('Γ.Α.Κ. Αριθμός','')}_{r.get('Γ.Α.Κ. Έτος','')}_w{worker_id}"
                            with open(f"artifacts/{base}.json","w",encoding="utf-8") as f:
                                json.dump(res, f, ensure_ascii=False, indent=2)
                            _dump_dom(page, base)

                        email_status = None
                        try:
                            if payload.get("ok") and _is_meaningful_result(payload.get("result")):
                                subject = (
                                    f"ΣΟΛΩΝ • {r.get('Πελάτης','')}"
                                    f" • {r.get('Δικαστήριο','')}"
                                    f" • ΓΑΚ {r.get('Γ.Α.Κ. Αριθμός','')}/{r.get('Γ.Α.Κ. Έτος','')}"
                                )
                                body = (
                                    f"Πελάτης: {r.get('Πελάτης','')}\n"
                                    f"Δικαστήριο: {r.get('Δικαστήριο','')}\n"
                                    f"Γ.Α.Κ.: {r.get('Γ.Α.Κ. Αριθμός','')}\n"
                                    f"Έτος: {r.get('Γ.Α.Κ. Έτος','')}\n\n"
                                    f"Αριθμός Απόφασης/Έτος - Είδος Διατακτικού:\n{payload.get('result','')}\n"
                                )
                                ok, msg = _send_email(subject, body)
                                email_status = msg
                        except Exception as e:
                            email_status = f"email skipped: {e}"

                        if email_status:
                            payload["email_status"] = email_status

                    except PWTimeout as e:
                        payload.update({"ok": False, "error": f"Timeout: {e}"})
                        if DEBUG_ARTIFACTS:
                            base = f"cli_row_{row_index}_{r.get('Γ.Α.Κ. Αριθμός','')}_{r.get('Γ.Α.Κ. Έτος','')}_w{worker_id}"
                            _dump_dom(page, base)
                    except Exception as e:
                        payload.update({"ok": False, "error": f"Σφάλμα: {e}"})
                        if DEBUG_ARTIFACTS:
                            base = f"cli_row_{row_index}_{r.get('Γ.Α.Κ. Αριθμός','')}_{r.get('Γ.Α.Κ. Έτος','')}_w{worker_id}"
                            _dump_dom(page, base)

                    out_q.put(payload)
            finally:
                context.close(); browser.close()
        out_q.put({"__worker_done__": worker_id})

    threads = [Thread(target=worker, args=(wid,), daemon=True) for wid in range(1, BATCH_WORKERS+1)]
    for t in threads: t.start()

    printed = 0
    done_workers = 0
    while done_workers < BATCH_WORKERS:
        item = out_q.get()
        if isinstance(item, dict) and item.get("__worker_done__"):
            done_workers += 1
            continue

        printed += 1
        pel = item.get("Πελάτης","—")
        dik = item.get("Δικαστήριο","—")
        num = item.get("Γ.Α.Κ. Αριθμός","—")
        yr  = item.get("Γ.Α.Κ. Έτος","—")

        if item.get("ok"):
            res = item.get("result") or "— κενό —"
            mail = f" ({item.get('email_status')})" if item.get("email_status") else ""
            print(f"{printed}. {pel} — {dik} — ΓΑΚ {num}/{yr}\n   Αριθμός Aπόφασης/'Ετος - Είδος Διατακτικού: {res}{mail}\n", flush=True)
        else:
            err = item.get("error","Άγνωστο σφάλμα")
            print(f"{printed}. {pel} — {dik} — ΓΑΚ {num}/{yr}\n   Αριθμός Aπόφασης/'Ετος - Είδος Διατακτικού: Σφάλμα: {err}\n", flush=True)


if __name__ == "__main__":
    main()
    subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
    atexit.register(lambda: print(f"⏱️ Elapsed: {(time.perf_counter() - _t) / 60:.2f} min"))

# -*- coding: utf-8 -*-
"""
ΣΟΛΩΝ Αυτόματες ενημερώσεις — v1.0.0
Single-browser app: single + batch από Excel + email + .env options
(Χωρίς προεπισκόπηση Excel)

Απαιτήσεις:
    pip install flask playwright pandas openpyxl python-dotenv
    python -m playwright install

.env (δίπλα στο app.py):
    SENDER_EMAIL=your@gmail.com
    RECEIVER_EMAIL=your@gmail.com
    GOOGLE_APP_PASSWORD=app_password
    # Απόδοση:
    HEADLESS=1
    BLOCK_MEDIA=1
    FAST_MODE=1
    RESULT_TIMEOUT_MS=60000
    EARLY_NO_DATA_MS=4000
    # Excel (προαιρετικά):
    EXCEL_SHEET=Sheet1
    COL_CLIENT=Πελάτης
    COL_COURT=Δικαστήριο
    COL_GAK_NUM=Γ.Α.Κ. Αριθμός
    COL_GAK_YEAR=Γ.Α.Κ. Έτος
    # Debug:
    DEBUG_ARTIFACTS=0
"""

from flask import Flask, request, render_template_string, jsonify, Response
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from email.message import EmailMessage
from dotenv import load_dotenv, find_dotenv
import pandas as pd
import unicodedata, re, os, json, smtplib, time

load_dotenv(find_dotenv(), override=True)

URL = "https://extapps.solon.gov.gr/mojwp/faces/TrackLdoPublic"

# ADF ids (προσοχή στα ':' → CSS escapes με '\\:')
SEL_KATASTIMA   = "#courtOfficeOC\\:\\:content"
SEL_GAK_NUMBER  = "#it1\\:\\:content"
SEL_GAK_YEAR    = "#it2\\:\\:content"
SEL_SEARCH_BTN  = "#ldoSearch a"

# Grid / spinner
SEL_GRID        = "#pc1\\:ldoTable"
SEL_GRID_DB     = "#pc1\\:ldoTable\\:\\:db"
SEL_GRID_HDR    = "#pc1\\:ldoTable\\:\\:hdr"
SEL_GRID_SPIN   = "#pc1\\:ldoTable\\:\\:sm"   # «Ανάκτηση δεδομένων...»
SEL_GRID_TABLE  = "#pc1\\:ldoTable"

# Χρόνοι από .env
DEFAULT_TIMEOUT = 30_000
RESULT_TIMEOUT  = int(os.getenv("RESULT_TIMEOUT_MS", "60000"))
EARLY_NO_DATA_MS = int(os.getenv("EARLY_NO_DATA_MS", "4000"))

EXCEL_FILE  = "SOLON_INPUT.xlsx"
EXCEL_SHEET = os.getenv("EXCEL_SHEET")

RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "dimitris.ziamparas@gmail.com")
SENDER_EMAIL   = os.getenv("SENDER_EMAIL",   RECEIVER_EMAIL)
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "465"))
APP_PASSWORD   = (
    os.getenv("GOOGLE_APP_PASSWORD")
    or os.getenv("GMAIL_APP_PASSWORD")
    or os.getenv("APP_PASSWORD")
)

HEADLESS      = os.getenv("HEADLESS", "1").lower() not in ("0","false","no")
BLOCK_MEDIA   = os.getenv("BLOCK_MEDIA", "0").lower() in ("1","true","yes")
FAST_MODE     = os.getenv("FAST_MODE", "0").lower() in ("1","true","yes")
DEBUG_ARTIFACTS = os.getenv("DEBUG_ARTIFACTS", "0").lower() in ("1","true","yes")

ENV_COL_CLIENT = os.getenv("COL_CLIENT")
ENV_COL_COURT  = os.getenv("COL_COURT")
ENV_COL_GAKNUM = os.getenv("COL_GAK_NUM")
ENV_COL_GAKYEAR= os.getenv("COL_GAK_YEAR")

app = Flask(__name__)

# ---------------- Helpers ---------------- #
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
                loc.first.click()
                page.wait_for_timeout(150)
                break
        except Exception:
            pass

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
    if not DEBUG_ARTIFACTS:
        return
    _ensure_artifacts_dir()
    try:
        hdr = page.evaluate("(sel)=>{const n=document.querySelector(sel); return n? n.outerHTML : ''}", SEL_GRID_HDR)
        db  = page.evaluate("(sel)=>{const n=document.querySelector(sel); return n? n.outerHTML : ''}", SEL_GRID_DB)
        tbl = page.evaluate("(sel)=>{const n=document.querySelector(sel); return n? n.outerHTML : ''}", SEL_GRID_TABLE)
        with open(f"artifacts/{base_name}_hdr.html", "w", encoding="utf-8") as f:
            f.write(hdr or "")
        with open(f"artifacts/{base_name}_db.html", "w", encoding="utf-8") as f:
            f.write(db or "")
        with open(f"artifacts/{base_name}_table.html", "w", encoding="utf-8") as f:
            f.write(tbl or "")
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

def _get_db_text(page) -> str:
    try:
        return page.evaluate("(sel)=>{const n=document.querySelector(sel); return n?(n.textContent||'').trim():'';}", SEL_GRID_DB)
    except Exception:
        return ""

def _wait_spinner_cycle_if_any(page):
    try:
        page.wait_for_selector(SEL_GRID_SPIN, state="visible", timeout=2_000)
    except Exception:
        pass
    try:
        page.wait_for_selector(SEL_GRID_SPIN, state="hidden", timeout=RESULT_TIMEOUT)
    except Exception:
        pass

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
    # αξιόπιστη εισαγωγή τιμής χωρίς να κολλάμε σε overlays (ADF)
    try:
        _wait_clickable(page, selector, timeout_ms=5_000)
    except Exception:
        pass
    ok = False
    try:
        ok = page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return false;
                el.focus();
                el.value = '';
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
    except Exception:
        ok = False
    if ok: return
    loc = page.locator(selector)
    try:
        loc.fill(str(value))
        page.wait_for_timeout(60)
        try: loc.press("Tab")
        except Exception: pass
        return
    except Exception:
        pass
    try:
        loc.click(force=True)
        loc.press("Control+A"); loc.press("Delete")
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

# ---------------- Court cache ---------------- #
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
        if n and n in key:
            return v
    raise ValueError("Δεν βρέθηκε το ζητούμενο δικαστήριο στη λίστα του SOLON.")

# ---------- Matchers στο ::db ---------- #
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
            if _is_meaningful_result(val):
                return val

        if res and res.get("noData"):
            if FAST_MODE:
                if first_no_data_at is None:
                    first_no_data_at = time.time()
                elif (time.time() - first_no_data_at) * 1000 >= EARLY_NO_DATA_MS:
                    return ""
        else:
            first_no_data_at = None

        _wait_spinner_cycle_if_any(page)
        page.wait_for_timeout(200 if FAST_MODE else 300)

    return ""

# ---------- SCRAPE (single-run) ---------- #
def _scrape_one(page, court_label: str, gak_num: str, gak_year: str):
    base_art = f"after_{gak_num}_{gak_year}"
    try:
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        _accept_cookies_if_present(page)

        court_map = _build_court_map(page)
        value = _get_court_value(court_map, court_label)
        page.select_option(SEL_KATASTIMA, value=value)
        page.wait_for_timeout(80)

        _set_input_value(page, SEL_GAK_NUMBER, str(gak_num).strip())
        _set_input_value(page, SEL_GAK_YEAR,   str(gak_year).strip())

        prev_sig = _get_db_text(page)

        btn = page.locator(SEL_SEARCH_BTN)
        if not (btn.count() and btn.first.is_visible()):
            return {"ok": False, "error": "Δεν βρέθηκε το κουμπί «Αναζήτηση»."}
        btn.first.click()

        _wait_for_table_ready(page, timeout_ms=RESULT_TIMEOUT)
        _wait_for_table_change(page, prev_sig, timeout_ms=RESULT_TIMEOUT)
        _wait_spinner_cycle_if_any(page)

        result = _wait_for_target_row_and_read(page, gak_num, gak_year, timeout_ms=RESULT_TIMEOUT)
        return {"ok": True, "result": result}
    finally:
        if DEBUG_ARTIFACTS:
            _dump_dom(page, base_art)

# ---------- SCRAPE (batch, single navigation) ---------- #
def _prepare_page_for_batch(context):
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT)
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
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

        btn = page.locator(SEL_SEARCH_BTN)
        if not (btn.count() and btn.first.is_visible()):
            return {"ok": False, "error": "Δεν βρέθηκε το κουμπί «Αναζήτηση»."}
        btn.first.click()

        _wait_for_table_ready(page, timeout_ms=RESULT_TIMEOUT)
        _wait_for_table_change(page, prev_sig, timeout_ms=RESULT_TIMEOUT)
        _wait_spinner_cycle_if_any(page)

        result = _wait_for_target_row_and_read(page, gak_num, gak_year, timeout_ms=RESULT_TIMEOUT)
        return {"ok": True, "result": result}
    finally:
        if DEBUG_ARTIFACTS:
            _dump_dom(page, base_art)

# ---------- Email ---------- #
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
        return (True, "Στάλθηκε.")
    except Exception as e:
        return (False, f"Σφάλμα αποστολής: {e}")

# ---------- Excel ---------- #
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

def _load_excel_rows(path=EXCEL_FILE):
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

# ---------------- WEB UI (single-column layout) ---------------- #
PAGE_HTML = """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<title>ΣΟΛΩΝ Αυτόματες ενημερώσεις — v1.0.0</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f6f7fb;margin:0}
  .wrap{max-width:1000px;margin:40px auto;padding:24px;background:#fff;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.06)}
  h1{margin:0 0 12px;font-size:22px}
  h3{margin:18px 0 10px}
  label{display:block;font-weight:600;margin:12px 0 6px}
  input,button{font-size:16px}
  input[type=text]{width:100%;padding:10px 12px;border:1px solid #ccd2dd;border-radius:10px;background:#fbfcfe}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .actions{display:flex;gap:12px;flex-wrap:wrap}
  button{padding:12px 16px;border:none;border-radius:12px;background:#2d6cdf;color:#fff;cursor:pointer}
  button.secondary{background:#556}
  button:disabled{opacity:.6;cursor:not-allowed}
  .result{margin-top:16px;padding:14px;border-radius:12px;background:#f0f6ff;border:1px solid #dce7ff}
  .error{background:#fff4f4;border-color:#ffdada}
  .list{margin-top:18px;border:1px solid #e6e6e6;border-radius:12px;background:#fafafa;max-height:420px;overflow:auto}
  .item{padding:10px 12px;border-bottom:1px solid #eee}
  .item:last-child{border-bottom:none}
  .muted{color:#777}
  code{background:#f3f3f3;padding:2px 6px;border-radius:6px}
</style>
</head>
<body>
<div class="wrap">
  <h1>ΣΟΛΩΝ Αυτόματες ενημερώσεις — v1.0.0</h1>

  <h3>Μονή Αναζήτηση</h3>
  <form id="single">
    <label for="court">Δικαστήριο</label>
    <input id="court" name="court" type="text" placeholder="π.χ. ΠΡΩΤΟΔΙΚΕΙΟ ΑΘΗΝΩΝ" required>
    <div class="row">
      <div>
        <label for="gak_num">Γ.Α.Κ. Αριθμός</label>
        <input id="gak_num" name="gak_num" type="text" required>
      </div>
      <div>
        <label for="gak_year">Γ.Α.Κ. Έτος</label>
        <input id="gak_year" name="gak_year" type="text" required>
      </div>
    </div>
    <div class="actions">
      <button type="submit" id="go">Αναζήτηση</button>
    </div>
  </form>
  <div id="out" class="result" style="display:none"></div>

  <hr style="margin:28px 0">

  <h3>Batch από Excel</h3>
  <p><b>Αρχείο: <code>SOLON_INPUT.xlsx</code> για μαζική εισαγωγή ΓΑΚ (στον ίδιο φάκελο)</b>.</p>
  <div class="actions">
    <button id="runBatch">Τρέξε από Excel</button>
  </div>
  <div id="stream" class="list" style="display:none"></div>

  <p class="muted">Emails στέλνονται ΜΟΝΟ για μη κενά/ουσιαστικά αποτελέσματα στον
    <code>{{recv}}</code>.</p>
</div>

<script>
const form = document.getElementById('single');
const go = document.getElementById('go');
const out = document.getElementById('out');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  out.style.display='none'; out.classList.remove('error');
  go.disabled = true; go.textContent = 'Αναζήτηση…';
  const payload = {
    court: document.getElementById('court').value,
    gak_num: document.getElementById('gak_num').value,
    gak_year: document.getElementById('gak_year').value
  };
  try {
    const r = await fetch('/api/search', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await r.json();
    out.style.display='block';
    if (data.ok) {
      out.innerHTML = '<b>Αριθμός Aπόφασης/\\'Ετος - Είδος Διατακτικού:</b> ' + (data.result || '<span class="muted">— κενό —</span>');
    } else {
      out.classList.add('error');
      out.innerHTML = '<b>Σφάλμα:</b> ' + (data.error || 'Άγνωστο σφάλμα');
    }
  } catch (err) {
    out.style.display='block'; out.classList.add('error');
    out.textContent = 'Σφάλμα δικτύου/διακομιστή.';
  } finally {
    go.disabled=false; go.textContent='Αναζήτηση';
  }
});

const runBtn = document.getElementById('runBatch');
const streamBox = document.getElementById('stream');

runBtn.addEventListener('click', async () => {
  runBtn.disabled = true; runBtn.textContent = 'Εκτέλεση…';
  streamBox.style.display = 'block'; streamBox.innerHTML = '';
  const es = new EventSource('/api/batch');
  es.onmessage = (ev) => {
    try {
      const row = JSON.parse(ev.data);

      if (row && row.type === 'error') {
        const div = document.createElement('div');
        div.className = 'item';
        div.innerHTML = '<b>Σφάλμα batch:</b> ' + (row.error || 'Άγνωστο σφάλμα');
        streamBox.appendChild(div);
        streamBox.scrollTop = streamBox.scrollHeight;
        es.close();
        runBtn.disabled = false; runBtn.textContent = 'Τρέξε από Excel';
        return;
      }

      const div = document.createElement('div');
      div.className = 'item';
      const Pelatis   = row['Πελάτης'] || '—';
      const Dikastirio= row['Δικαστήριο'] || '—';
      const GakNum    = row['Γ.Α.Κ. Αριθμός'] || '—';
      const GakYear   = row['Γ.Α.Κ. Έτος'] || '—';
      const res = row.ok ? (row.result || '<span class="muted">— κενό —</span>')
                         : ('<span class="muted">Σφάλμα: '+(row.error||'')+'</span>');
      const mail = row.email_status ? (' <span class="muted">('+row.email_status+')</span>') : '';
      div.innerHTML = '<b>'+Pelatis+'</b> — '+Dikastirio+' — ΓΑΚ '+GakNum+'/'+GakYear+
                      '<br><b>Αριθμός Aπόφασης/\\'Ετος - Είδος Διατακτικού:</b> '+res+mail;
      streamBox.appendChild(div);
      streamBox.scrollTop = streamBox.scrollHeight;
    } catch(e){}
  };
  es.onerror = () => {
    es.close();
    runBtn.disabled = false; runBtn.textContent = 'Τρέξε από Excel';
  };
});
</script>
</body>
</html>
"""

# ---------------- ROUTES ---------------- #
@app.get("/")
def index():
    return render_template_string(PAGE_HTML, recv=RECEIVER_EMAIL)

@app.post("/api/search")
def api_search():
    data = request.get_json(force=True, silent=True) or {}
    court   = (data.get("court") or "").strip()
    gak_num = (data.get("gak_num") or "").strip()
    gak_year= (data.get("gak_year") or "").strip()
    if not court or not gak_num or not gak_year:
        return jsonify({"ok": False, "error": "Συμπλήρωσε όλα τα πεδία."})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(locale="el-GR", viewport={"width":1500,"height":950})
        if BLOCK_MEDIA:
            context.route("**/*", _route_blocker)
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)
        try:
            res = _scrape_one(page, court, gak_num, gak_year)
        except PWTimeout as e:
            res = {"ok": False, "error": f"Timeout: {e}"}
        except Exception as e:
            res = {"ok": False, "error": f"Σφάλμα: {e}"}
        finally:
            context.close(); browser.close()
    return jsonify(res)

@app.get("/api/batch")
def api_batch():
    def _stream():
        try:
            rows = _load_excel_rows(EXCEL_FILE)
        except Exception as e:
            yield "data: " + json.dumps({"type": "error", "ok": False, "error": str(e)}, ensure_ascii=False) + "\n\n"
            return

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context(locale="el-GR", viewport={"width":1500,"height":950})
            if BLOCK_MEDIA:
                context.route("**/*", _route_blocker)

            try:
                # Προετοιμασία: μία φορά navigation και cache courts
                page, court_map = _prepare_page_for_batch(context)

                for i, r in enumerate(rows, start=1):
                    payload = {**r}
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
                            base = f"row_{i}_{r.get('Γ.Α.Κ. Αριθμός','')}_{r.get('Γ.Α.Κ. Έτος','')}"
                            with open(f"artifacts/{base}.json","w",encoding="utf-8") as f:
                                json.dump(res, f, ensure_ascii=False, indent=2)
                            _dump_dom(page, base)

                    except PWTimeout as e:
                        payload.update({"ok": False, "error": f"Timeout: {e}"})
                        if DEBUG_ARTIFACTS:
                            base = f"row_{i}_{r.get('Γ.Α.Κ. Αριθμός','')}_{r.get('Γ.Α.Κ. Έτος','')}"
                            _dump_dom(page, base)
                    except Exception as e:
                        payload.update({"ok": False, "error": f"Σφάλμα: {e}"})
                        if DEBUG_ARTIFACTS:
                            base = f"row_{i}_{r.get('Γ.Α.Κ. Αριθμός','')}_{r.get('Γ.Α.Κ. Έτος','')}"
                            _dump_dom(page, base)

                    # Email μόνο για ουσιαστικό αποτέλεσμα
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
                            email_status = "email ok" if ok else f"email failed: {msg}"
                    except Exception as e:
                        email_status = f"email skipped: {e}"

                    if email_status:
                        payload["email_status"] = email_status

                    yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

            finally:
                context.close(); browser.close()
    return Response(_stream(), mimetype="text/event-stream")

# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    app.run(debug=True)

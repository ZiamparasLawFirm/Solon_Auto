# -*- coding: utf-8 -*-
"""
SOLON Web App (single + batch από Excel + email + Excel preview + .env column mapping)

Fix:
- Playwright wait_for_function: περνάμε το arg ΜΟΝΟ ως keyword (arg=...).
- Πάντα σώζουμε artifacts (hdr/db/table) με DEBUG_ARTIFACTS=1 ακόμη κι αν προκύψει σφάλμα.
- Matcher χωρίς headers, αναμονή spinner/cycle και αναζήτηση ακριβούς γραμμής ΓΑΚ/Έτος.

Εγκατάσταση:
    pip install flask playwright pandas openpyxl python-dotenv
    python -m playwright install

.env (δίπλα στο app.py):
    SENDER_EMAIL=your@gmail.com
    RECEIVER_EMAIL=your@gmail.com
    GOOGLE_APP_PASSWORD=app_password
    # Optional:
    # SMTP_HOST=smtp.gmail.com
    # SMTP_PORT=465
    # HEADLESS=1            # 0 για ορατό browser
    # EXCEL_SHEET=Sheet1
    # COL_CLIENT=Πελάτης
    # COL_COURT=Δικαστήριο
    # COL_GAK_NUM=Γ.Α.Κ. Αριθμός
    # COL_GAK_YEAR=Γ.Α.Κ. Έτος
    # DEBUG_ARTIFACTS=1
"""

from flask import Flask, request, render_template_string, jsonify, Response
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from email.message import EmailMessage
from dotenv import load_dotenv, find_dotenv
import pandas as pd
import unicodedata, re, os, json, smtplib, time

# Φόρτωση .env
load_dotenv(find_dotenv(), override=True)

# ---------------- ΡΥΘΜΙΣΕΙΣ / SELECTORS ---------------- #
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

DEFAULT_TIMEOUT = 30_000   # ms
RESULT_TIMEOUT  = 60_000   # ms

EXCEL_FILE  = "SOLON_INPUT.xlsx"
EXCEL_SHEET = os.getenv("EXCEL_SHEET")  # optional συγκεκριμένο φύλλο

# Email από .env (defaults)
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "dimitris.ziamparas@gmail.com")
SENDER_EMAIL   = os.getenv("SENDER_EMAIL",   RECEIVER_EMAIL)
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "465"))
APP_PASSWORD   = (
    os.getenv("GOOGLE_APP_PASSWORD")
    or os.getenv("GMAIL_APP_PASSWORD")
    or os.getenv("APP_PASSWORD")
)

# Headless toggle (HEADLESS=0 για ορατό browser)
HEADLESS = os.getenv("HEADLESS", "1").lower() not in ("0", "false", "no")

# Debug artifacts
DEBUG_ARTIFACTS = os.getenv("DEBUG_ARTIFACTS", "0").lower() in ("1","true","yes")

# Προαιρετικό manual mapping από .env
ENV_COL_CLIENT = os.getenv("COL_CLIENT")
ENV_COL_COURT  = os.getenv("COL_COURT")
ENV_COL_GAKNUM = os.getenv("COL_GAK_NUM")
ENV_COL_GAKYEAR= os.getenv("COL_GAK_YEAR")

app = Flask(__name__)

# ---------------- ΒΟΗΘΗΤΙΚΑ ---------------- #
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
                page.wait_for_timeout(200)
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

def _choose_option_value(page, select_css: str, desired_label: str) -> str:
    desired_norm = _normalize(desired_label)
    options = page.locator(f"{select_css} option")
    texts   = options.all_text_contents()
    values  = options.evaluate_all("els => els.map(e => e.value)")
    for t, v in zip(texts, values):
        if _normalize(t) == desired_norm:
            return v
    for t, v in zip(texts, values):
        if desired_norm and desired_norm in _normalize(t):
            return v
    raise ValueError("Δεν βρέθηκε το ζητούμενο δικαστήριο στη λίστα του SOLON.")

def _get_db_text(page) -> str:
    try:
        return page.evaluate("(sel)=>{const n=document.querySelector(sel); return n?(n.textContent||'').trim():'';}", SEL_GRID_DB)
    except Exception:
        return ""

def _wait_spinner_cycle_if_any(page):
    # Προσπάθησε να δεις spinner -> κρύψιμο
    try:
        page.wait_for_selector(SEL_GRID_SPIN, state="visible", timeout=2_000)
    except Exception:
        pass
    try:
        page.wait_for_selector(SEL_GRID_SPIN, state="hidden", timeout=RESULT_TIMEOUT)
    except Exception:
        pass

def _wait_for_table_ready(page, timeout_ms=RESULT_TIMEOUT):
    """Περιμένει να είναι έτοιμο το data-body: είτε έχει td είτε γράφει 'Δεν υπάρχουν δεδομένα'."""
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
    """Περιμένει να αλλάξει το textContent του ::db σε σχέση με το προηγούμενο."""
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
        pass  # αν δεν αλλάξει, συνεχίζουμε

def _ensure_artifacts_dir():
    if DEBUG_ARTIFACTS and not os.path.exists("artifacts"):
        os.makedirs("artifacts", exist_ok=True)

def _dump_dom(page, base_name: str):
    """Σώζει hdr, db και full table σε ξεχωριστά html αρχεία όταν DEBUG_ARTIFACTS=1."""
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

def _clear_and_fill(page, selector: str, value: str):
    loc = page.locator(selector)
    loc.click()
    try:
        loc.fill("")  # καθάρισμα
    except Exception:
        pass
    loc.type(str(value), delay=10)  # λίγο delay για ADF
    page.wait_for_timeout(60)

# ---------- Κύριος matcher ΜΟΝΟ από ::db (χωρίς headers) ---------- #
def _read_decision_from_db(page, gak_num: str, gak_year: str) -> str:
    """
    Βρες στο ::db τη γραμμή (tr) όπου:
      - είτε ΥΠΑΡΧΟΥΝ ΚΕΛΙΑ με ακριβώς `gak_num` ΚΑΙ ακριβώς `gak_year`
      - είτε υπάρχει κελί με pattern `gak_num/gak_year`
    Μετά πάρε το διατακτικό από c10 (ή κενό αν δεν υπάρχει).
    """
    js = r"""
    (args) => {
      const { dbSel, num, year } = args;
      const db = document.querySelector(dbSel);
      if (!db) return null;

      const norm = s => (s||'').toString().replace(/\u00A0/g,' ').replace(/\s+/g,' ').trim();
      const needleNum  = norm(num);
      const needleYear = norm(year);
      const rxCombined = new RegExp('^\\s*'+needleNum.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s*/\\s*'+needleYear.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s*$');

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
          return val || "";
        }
      }
      return null;
    }
    """
    try:
        val = page.evaluate(js, {"dbSel": SEL_GRID_DB, "num": str(gak_num).strip(), "year": str(gak_year).strip()})
        if val and _is_meaningful_result(val):
            return val
    except Exception:
        pass
    return ""

def _wait_for_target_row_and_read(page, gak_num: str, gak_year: str, timeout_ms=RESULT_TIMEOUT) -> str:
    """
    Περιμένει μέχρι να εμφανιστεί στο ::db γραμμή που:
    - έχει κελί ακριβώς 'gak_num/gak_year', ή
    - έχει *ξεχωριστά* κελιά με ακριβώς gak_num και ακριβώς gak_year.
    Όταν τη βρει, επιστρέφει το κείμενο της στήλης c10 της ίδιας γραμμής (ή κενό).
    """
    js = r"""
    (args) => {
      const { dbSel, num, year } = args;
      const db = document.querySelector(dbSel);
      if (!db) return { found:false };

      const norm = s => (s||'').toString().replace(/\u00A0/g,' ').replace(/\s+/g,' ').trim();
      const needleNum  = norm(num);
      const needleYear = norm(year);
      const rxCombined = new RegExp('^\\s*'+needleNum.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s*/\\s*'+needleYear.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s*$');

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
    while time.time() < deadline:
        res = page.evaluate(js, {"dbSel": SEL_GRID_DB, "num": str(gak_num).strip(), "year": str(gak_year).strip()})
        if res and res.get("found") and _is_meaningful_result(res.get("value","")):
            return res.get("value","").strip()

        _wait_spinner_cycle_if_any(page)
        page.wait_for_timeout(250)

    return ""

# ---------- SCRAPE ---------- #
def _scrape_one(page, court_label: str, gak_num: str, gak_year: str):
    """Τρέχει ΜΙΑ αναζήτηση στο SOLON με ήδη ανοιχτό page."""
    base_art = f"after_{gak_num}_{gak_year}"
    try:
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        _accept_cookies_if_present(page)

        # Επιλογή δικαστηρίου (native <select>)
        page.wait_for_selector(SEL_KATASTIMA, state="visible", timeout=DEFAULT_TIMEOUT)
        value = _choose_option_value(page, SEL_KATASTIMA, court_label)
        page.select_option(SEL_KATASTIMA, value=value)
        page.wait_for_timeout(120)  # μικρή ανάσα

        # Συμπλήρωση ΓΑΚ/Έτος + blur
        _clear_and_fill(page, SEL_GAK_NUMBER, str(gak_num).strip())
        _clear_and_fill(page, SEL_GAK_YEAR,   str(gak_year).strip())
        page.locator(SEL_GAK_YEAR).press("Tab")
        page.wait_for_timeout(120)

        # Υπόμνημα πριν το κλικ
        prev_sig = _get_db_text(page)

        # Κλικ «Αναζήτηση»
        btn = page.locator(SEL_SEARCH_BTN)
        if not (btn.count() and btn.first.is_visible()):
            return {"ok": False, "error": "Δεν βρέθηκε το κουμπί «Αναζήτηση»."}
        btn.first.click()

        # Περιμένω έτοιμο πίνακα & πιθανό spinner
        _wait_for_table_ready(page, timeout_ms=RESULT_TIMEOUT)
        _wait_for_table_change(page, prev_sig, timeout_ms=RESULT_TIMEOUT)
        _wait_spinner_cycle_if_any(page)

        # Περιμένω τη συγκεκριμένη γραμμή κι επιστρέφω διατακτικό
        result = _wait_for_target_row_and_read(page, gak_num, gak_year, timeout_ms=RESULT_TIMEOUT)

        return {"ok": True, "result": result}
    finally:
        if DEBUG_ARTIFACTS:
            _dump_dom(page, base_art)

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

# ---------- Excel helpers ---------- #
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

def _excel_preview(path=EXCEL_FILE):
    if not os.path.exists(path):
        return {"ok": False, "error": f"Δεν βρέθηκε το {path}"}
    try:
        xls = pd.ExcelFile(path, engine="openpyxl")
    except Exception as e:
        return {"ok": False, "error": f"Σφάλμα ανάγνωσης Excel: {e}"}
    out = {"ok": True, "sheets": []}
    for sheet in xls.sheet_names:
        try:
            df0 = pd.read_excel(xls, sheet_name=sheet, engine="openpyxl", dtype=str).fillna("")
            headers0 = list(df0.columns.astype(str))
        except Exception:
            headers0 = []
        try:
            df_raw = pd.read_excel(xls, sheet_name=sheet, engine="openpyxl", dtype=str, header=None).fillna("")
            first10 = df_raw.head(10).astype(str).values.tolist()
        except Exception:
            first10 = []
        out["sheets"].append({
            "name": sheet,
            "headers_guess": headers0,
            "first10rows": first10
        })
    return out

# ---------------- WEB UI ---------------- #
PAGE_HTML = """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<title>SOLON – Γ.Α.Κ. Αναζήτηση</title>
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
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{border:1px solid #e8e8e8;padding:6px 8px;text-align:left}
  .scroll{max-height:260px;overflow:auto;border:1px solid #eee;border-radius:8px}
</style>
</head>
<body>
<div class="wrap">
  <h1>SOLON – «Αριθμός Απόφασης/Έτος - Είδος Διατακτικού»</h1>

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
      <button type="button" id="preview" class="secondary">Προεπισκόπηση Excel</button>
    </div>
  </form>
  <div id="out" class="result" style="display:none"></div>

  <div id="previewBox" class="result" style="display:none;margin-top:12px"></div>

  <hr style="margin:28px 0">

  <h3>Batch από Excel</h3>
  <p>Αρχείο: <code>SOLON_INPUT.xlsx</code> (στον ίδιο φάκελο). Μπορείς είτε να:
     <br>• αφήσεις το app να κάνει auto-detect κεφαλίδων, είτε
     <br>• ορίσεις στο <code>.env</code> τα <code>COL_CLIENT</code>, <code>COL_COURT</code>, <code>COL_GAK_NUM</code>, <code>COL_GAK_YEAR</code>.</p>
  <div class="actions">
    <button id="runBatch">Τρέξε από Excel</button>
  </div>
  <div id="stream" class="list" style="display:none"></div>

  <p class="muted">Emails στέλνονται ΜΟΝΟ για μη κενά/ουσιαστικά αποτελέσματα στον
    <code>{{recv}}</code>. Ρύθμισε το <code>GOOGLE_APP_PASSWORD</code> (ή GMAIL_APP_PASSWORD) στο .env.</p>
</div>

<script>
const form = document.getElementById('single');
const go = document.getElementById('go');
const out = document.getElementById('out');
const previewBtn = document.getElementById('preview');
const previewBox = document.getElementById('previewBox');

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
      out.innerHTML = '<b>Αποτέλεσμα:</b> ' + (data.result || '<span class="muted">— κενό —</span>');
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

previewBtn.addEventListener('click', async () => {
  previewBox.style.display = 'block';
  previewBox.innerHTML = 'Φόρτωση προεπισκόπησης…';
  try {
    const r = await fetch('/api/preview_excel');
    const data = await r.json();
    if (!data.ok) {
      previewBox.innerHTML = '<b>Σφάλμα:</b> ' + (data.error||'');
      return;
    }
    let html = '';
    data.sheets.forEach(s => {
      html += '<h4>Φύλλο: '+s.name+'</h4>';
      html += '<div><b>Κεφαλίδες (header=0):</b> '+ (s.headers_guess && s.headers_guess.length ? s.headers_guess.join(' | ') : '<i>—</i>') + '</div>';
      if (s.first10rows && s.first10rows.length) {
        html += '<div class="scroll"><table><thead><tr><th>#</th><th>Στήλες τιμών (πρώτες 10 γραμμές, raw)</th></tr></thead><tbody>';
        s.first10rows.forEach((row,i)=>{
          const rowText = row.join(' | ');
          html += '<tr><td>'+i+'</td><td>'+rowText+'</td></tr>';
        });
        html += '</tbody></table></div>';
      }
    });
    html += '<p class="muted">Αν δεν αναγνωρίζονται οι στήλες, βάλε στο .env τις μεταβλητές <code>COL_CLIENT</code>, <code>COL_COURT</code>, <code>COL_GAK_NUM</code>, <code>COL_GAK_YEAR</code> με <b>ακριβώς</b> τα ονόματα όπως εμφανίζονται στις κεφαλίδες του Excel.</p>';
    previewBox.innerHTML = html;
  } catch (err) {
    previewBox.innerHTML = '<b>Σφάλμα προεπισκόπησης.</b>';
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
      div.innerHTML = '<b>'+Pelatis+'</b> — '+Dikastirio+' — ΓΑΚ '+GakNum+'/'+GakYear+'<br>Αποτέλεσμα: '+res+mail;
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

@app.get("/api/preview_excel")
def api_preview_excel():
    return jsonify(_excel_preview(EXCEL_FILE))

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
        ctx = browser.new_context(locale="el-GR", viewport={"width":1500,"height":950})
        page = ctx.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)
        try:
            res = _scrape_one(page, court, gak_num, gak_year)
        except PWTimeout as e:
            res = {"ok": False, "error": f"Timeout: {e}"}
        except Exception as e:
            res = {"ok": False, "error": f"Σφάλμα: {e}"}
        finally:
            ctx.close(); browser.close()
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
            ctx = browser.new_context(locale="el-GR", viewport={"width":1500,"height":950})
            page = ctx.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT)
            try:
                for i, r in enumerate(rows, start=1):
                    payload = {**r}
                    try:
                        res = _scrape_one(page, r.get("Δικαστήριο",""), r.get("Γ.Α.Κ. Αριθμός",""), r.get("Γ.Α.Κ. Έτος",""))
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

                    # Email ΜΟΝΟ για ουσιαστικό αποτέλεσμα
                    email_status = None
                    try:
                        if payload.get("ok") and _is_meaningful_result(payload.get("result")):
                            subject = (
                                f"SOLON • {r.get('Πελάτης','')}"
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
                ctx.close(); browser.close()
    return Response(_stream(), mimetype="text/event-stream")

# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    app.run(debug=True)

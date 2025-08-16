# -*- coding: utf-8 -*-
"""
Web App: Αναζήτηση στο SOLON και επιστροφή
«Αριθμός Απόφασης/Έτος - Είδος Διατακτικού».

Τεχνικά:
- Flask για το web UI/API
- Playwright (Chromium) για αυτόματη πλοήγηση
- Native <select> για «Κατάστημα» (id: courtOfficeOC::content)
- Πίνακας αποτελεσμάτων: pc1:ldoTable ; κελί 1ης γραμμής στήλης c10 => id "pc1:ldoTable:0:c10"

Σημειώσεις:
- Το πεδίο "Δικαστήριο" δέχεται ΕΛΕΥΘΕΡΟ κείμενο. Ο κώδικας διαβάζει
  όλες τις <option> από το dropdown της σελίδας και βρίσκει την καλύτερη αντιστοίχιση:
  1) ακριβές ταίριασμα (χωρίς διάκριση πεζών/κεφαλαίων/τονισμών),
  2) αλλιώς "περιέχει",
  3) αλλιώς σφάλμα.
- Χρόνοι/timeouts είναι ήπιοι· αν η γραμμή σου είναι αργή, αύξησέ τους.
"""

from flask import Flask, request, render_template_string, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import unicodedata, re, os, html

URL = "https://extapps.solon.gov.gr/mojwp/faces/TrackLdoPublic"

# CSS selectors με σωστά escape για τα ADF ids (προσοχή στα ':')
SEL_KATASTIMA = "#courtOfficeOC\\:\\:content"    # <select id="courtOfficeOC::content">
SEL_GAK_NUMBER = "#it1\\:\\:content"             # Γ.Α.Κ. Αριθμός
SEL_GAK_YEAR   = "#it2\\:\\:content"             # Γ.Α.Κ. Έτος
SEL_SEARCH_BTN = "#ldoSearch a"                   # «Αναζήτηση»

SEL_GRID       = "#pc1\\:ldoTable"
SEL_GRID_DB    = "#pc1\\:ldoTable\\:\\:db"
SEL_DECISION_DIRECT = "#pc1\\:ldoTable\\:0\\:c10"     # 1η γραμμή, στήλη c10
SEL_DECISION_ANYROW = "[id^='pc1:ldoTable:'][id$=':c10']"  # fallback

DEFAULT_TIMEOUT = 30_000   # ms
RESULT_TIMEOUT  = 60_000   # ms

app = Flask(__name__)

# ---------------- Βοηθητικά ---------------- #

def _ensure_artifacts():
    os.makedirs("artifacts", exist_ok=True)

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
    """Αφαίρεση τόνων/διαλυτικών, trim, συμπίεση κενών, κεφαλαιοποίηση."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s

def _choose_option_value(page, select_css: str, desired_label: str):
    """
    Διαβάζει όλα τα <option> του <select> και βρίσκει την καλύτερη αντιστοίχιση
    για το κείμενο που έδωσε ο χρήστης. Επιστρέφει το value που θα περάσουμε
    στο select_option (είναι το πιο αξιόπιστο).
    """
    desired_norm = _normalize(desired_label)

    options = page.locator(f"{select_css} option")
    texts = options.all_text_contents()
    values = options.evaluate_all("els => els.map(e => e.value)")

    # 1) Ακριβές ταίριασμα (normalized)
    for t, v in zip(texts, values):
        if _normalize(t) == desired_norm:
            return v

    # 2) Περιέχει (normalized)
    for t, v in zip(texts, values):
        if desired_norm and desired_norm in _normalize(t):
            return v

    # 3) Αποτυχία
    raise ValueError(
        "Δεν βρέθηκε το ζητούμενο δικαστήριο στη λίστα. "
        "Δοκίμασε να γράψεις ακριβώς όπως εμφανίζεται στο dropdown της σελίδας."
    )

def _wait_for_table_data(page, timeout_ms=RESULT_TIMEOUT):
    # Grid ορατό
    page.wait_for_selector(SEL_GRID, state="visible", timeout=DEFAULT_TIMEOUT)
    # Περιμένουμε να γεμίσει με <td> και να φύγει το "Δεν υπάρχουν δεδομένα"
    page.wait_for_function(
        """
        (dbSel) => {
            const db = document.querySelector(dbSel);
            if (!db) return false;
            if (db.textContent && db.textContent.trim().includes("Δεν υπάρχουν δεδομένα")) return false;
            return !!db.querySelector("td");
        }
        """,
        arg=SEL_GRID_DB,
        timeout=timeout_ms
    )

def _read_decision_text(page):
    # 1) Στοχευμένα στο κελί 1ης γραμμής, στήλη c10
    try:
        page.wait_for_selector(SEL_DECISION_DIRECT, state="visible", timeout=5_000)
        txt = page.locator(SEL_DECISION_DIRECT).inner_text().strip()
        if txt:
            return txt
    except Exception:
        pass

    # 2) Πάρε το πρώτο κελί οποιασδήποτε γραμμής της στήλης c10
    try:
        cell = page.locator(SEL_DECISION_ANYROW).first
        if cell.count():
            txt = cell.inner_text().strip()
            if txt:
                return txt
    except Exception:
        pass

    return None

def scrape_solon(court_label: str, gak_number: str, gak_year: str):
    """
    Πυρήνας scraping. Επιστρέφει dict με:
      {"ok": True, "result": "..."} ή {"ok": False, "error": "..."}
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # headless για server/web
        ctx = browser.new_context(locale="el-GR", viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)

        try:
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            _accept_cookies_if_present(page)

            # Επιλογή Δικαστηρίου
            page.wait_for_selector(SEL_KATASTIMA, state="visible", timeout=DEFAULT_TIMEOUT)
            value = _choose_option_value(page, SEL_KATASTIMA, court_label)
            page.select_option(SEL_KATASTIMA, value=value)

            # Γ.Α.Κ. πεδία
            page.fill(SEL_GAK_NUMBER, str(gak_number).strip())
            page.fill(SEL_GAK_YEAR, str(gak_year).strip())

            # Αναζήτηση
            btn = page.locator(SEL_SEARCH_BTN)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
            else:
                return {"ok": False, "error": "Δεν βρέθηκε το κουμπί «Αναζήτηση»."}

            # Αναμονή αποτελεσμάτων
            _wait_for_table_data(page, timeout_ms=RESULT_TIMEOUT)

            # Ανάγνωση πεδίου στο grid
            result = _read_decision_text(page)
            if not result:
                # αποθήκευση για debug
                _ensure_artifacts()
                with open("artifacts/debug_after.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                page.screenshot(path="artifacts/solon_after_search.png", full_page=True)
                return {"ok": False, "error": "Δεν βρέθηκε κείμενο στη στήλη αποτελέσματος."}

            return {"ok": True, "result": result}

        except PWTimeout as e:
            return {"ok": False, "error": f"Timeout: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"Σφάλμα: {e}"}
        finally:
            ctx.close()
            browser.close()

# ---------------- Web UI ---------------- #

PAGE_HTML = """
<!doctype html>
<html lang="el">
<head>
  <meta charset="utf-8">
  <title>SOLON – Γ.Α.Κ. Αναζήτηση</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f6f7fb;margin:0;padding:0}
    .wrap{max-width:760px;margin:40px auto;padding:24px;background:#fff;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,0.06)}
    h1{margin:0 0 16px;font-size:22px}
    p.hint{color:#666;margin-top:0}
    label{display:block;font-weight:600;margin:12px 0 6px}
    input,button{font-size:16px}
    input[type=text], input[type=number]{width:100%;padding:10px 12px;border:1px solid #ccd2dd;border-radius:10px;background:#fbfcfe}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    button{margin-top:16px;padding:12px 16px;border:none;border-radius:12px;background:#2d6cdf;color:#fff;cursor:pointer}
    button:disabled{opacity:.6;cursor:not-allowed}
    .result{margin-top:22px;padding:14px;border-radius:12px;background:#f0f6ff;border:1px solid #dce7ff}
    .error{background:#fff4f4;border-color:#ffdada}
    code{background:#f3f3f3;padding:2px 6px;border-radius:6px}
    footer{margin-top:24px;color:#8a8a8a;font-size:13px}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Πορεία Υπόθεσης (SOLON) → «Αριθμός Απόφασης/Έτος - Είδος Διατακτικού»</h1>
    <p class="hint">Συμπλήρωσε τα στοιχεία και πάτα <b>Αναζήτηση</b>. Το app θα ανοίξει το SOLON,
      θα βρει τα δεδομένα και θα επιστρέψει το πεδίο της απόφασης.</p>

    <form id="f">
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

      <button type="submit" id="go">Αναζήτηση</button>
    </form>

    <div id="out" class="result" style="display:none"></div>

    <footer>Tip: Γράψε το δικαστήριο όπως εμφανίζεται στη λίστα του SOLON
      (π.χ. <code>ΠΡΩΤΟΔΙΚΕΙΟ ΑΘΗΝΩΝ</code>, <code>ΕΙΡΗΝΟΔΙΚΕΙΟ ΑΘΗΝΩΝ</code>).</footer>
  </div>

<script>
const form = document.getElementById('f');
const go = document.getElementById('go');
const out = document.getElementById('out');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  out.style.display = 'none';
  out.classList.remove('error');
  go.disabled = true; go.textContent = 'Αναζήτηση…';

  const payload = {
    court:   document.getElementById('court').value,
    gak_num: document.getElementById('gak_num').value,
    gak_year:document.getElementById('gak_year').value
  };

  try {
    const r = await fetch('/api/search', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await r.json();
    out.style.display = 'block';
    if (data.ok) {
      out.innerHTML = '<b>Αποτέλεσμα:</b> ' + data.result;
    } else {
      out.classList.add('error');
      out.innerHTML = '<b>Σφάλμα:</b> ' + (data.error || 'Άγνωστο σφάλμα');
    }
  } catch (err) {
    out.style.display = 'block';
    out.classList.add('error');
    out.textContent = 'Σφάλμα δικτύου/διακομιστή.';
  } finally {
    go.disabled = false; go.textContent = 'Αναζήτηση';
  }
});
</script>
</body>
</html>
"""

@app.get("/")
def index():
    return render_template_string(PAGE_HTML)

@app.post("/api/search")
def api_search():
    data = request.get_json(force=True, silent=True) or {}
    court   = (data.get("court") or "").strip()
    gak_num = (data.get("gak_num") or "").strip()
    gak_year= (data.get("gak_year") or "").strip()

    if not court or not gak_num or not gak_year:
        return jsonify({"ok": False, "error": "Συμπλήρωσε όλα τα πεδία."})

    res = scrape_solon(court, gak_num, gak_year)
    return jsonify(res)

if __name__ == "__main__":
    # Το Flask dev server είναι ΟΚ για local χρήση.
    # Αν το τρέξεις σε production, βάλε gunicorn/uwsgi + Playwright pool.
    app.run(debug=True)

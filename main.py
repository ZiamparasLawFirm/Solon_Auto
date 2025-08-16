from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from typing import Optional
import sys, os, time

URL = "https://extapps.solon.gov.gr/mojwp/faces/TrackLdoPublic"

# DOM selectors (προκύπτουν από το δικό σου debug HTML)
SEL_KATASTIMA = "#courtOfficeOC\\:\\:content"     # <select id="courtOfficeOC::content">  (Κατάστημα)  [debug]
SEL_GAK_NUMBER = "#it1\\:\\:content"              # <input id="it1::content">              [debug]
SEL_GAK_YEAR   = "#it2\\:\\:content"              # <input id="it2::content">              [debug]
SEL_SEARCH_BTN = "#ldoSearch a"                   # <div id="ldoSearch"><a>Αναζήτηση</a>   [debug]

SEL_GRID       = "#pc1\\:ldoTable"                # <div id="pc1:ldoTable" role="grid">    [debug]
SEL_GRID_DB    = "#pc1\\:ldoTable\\:\\:db"        # data body του πίνακα                   [debug]
# Κελί-στόχος (1η γραμμή, στήλη c10 = «Αριθμός Απόφασης/Έτος - Είδος Διατακτικού»)
SEL_DECISION_DIRECT = "#pc1\\:ldoTable\\:0\\:c10" # <td id="pc1:ldoTable:0:c10">           [debug]
# Fallback: οποιοδήποτε κελί της στήλης c10 (θα πάρουμε της 1ης γραμμής)
SEL_DECISION_ANYROW = "[id^='pc1:ldoTable:'][id$=':c10']"

DEFAULT_TIMEOUT = 30_000
RESULT_TIMEOUT  = 60_000

KATASTHMA_LABEL = "ΠΡΩΤΟΔΙΚΕΙΟ ΑΘΗΝΩΝ"
KATASTHMA_VALUE = "50"  # <option value="50">ΠΡΩΤΟΔΙΚΕΙΟ ΑΘΗΝΩΝ</option>  [debug]

def ensure_artifacts():
    os.makedirs("artifacts", exist_ok=True)

def accept_cookies_if_present(page):
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

def select_katastima(page):
    page.wait_for_selector(SEL_KATASTIMA, state="visible", timeout=DEFAULT_TIMEOUT)
    try:
        page.select_option(SEL_KATASTIMA, label=KATASTHMA_LABEL)
    except Exception:
        page.select_option(SEL_KATASTIMA, value=KATASTHMA_VALUE)
    # επιβεβαίωση
    selected = page.locator(f"{SEL_KATASTIMA} option:checked").first.inner_text().strip()
    if KATASTHMA_LABEL not in selected:
        page.select_option(SEL_KATASTIMA, value=KATASTHMA_VALUE)

def click_search(page):
    btn = page.locator(SEL_SEARCH_BTN)
    if btn.count() and btn.first.is_visible():
        btn.first.click()
        return
    raise RuntimeError("Δεν βρέθηκε/κλικάρεται το «Αναζήτηση».")

def wait_for_table_data(page, timeout_ms=RESULT_TIMEOUT):
    # Περιμένουμε το grid να είναι ορατό
    page.wait_for_selector(SEL_GRID, state="visible", timeout=DEFAULT_TIMEOUT)
    # Περιμένουμε να εμφανιστεί τουλάχιστον ένα td στο data-body ΚΑΙ να μην γράφει 'Δεν υπάρχουν δεδομένα'
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

def read_decision_text(page) -> Optional[str]:
    """
    Διαβάζει το κείμενο από την 1η γραμμή της στήλης c10.
    1) Περιμένει το άμεσο κελί #pc1:ldoTable:0:c10
    2) Fallback: βρίσκει οποιοδήποτε td με id που τελειώνει σε :c10 και παίρνει το πρώτο
    """
    # 1) Άμεσος selector με escape
    try:
        page.wait_for_selector(SEL_DECISION_DIRECT, state="visible", timeout=5_000)
        txt = page.locator(SEL_DECISION_DIRECT).inner_text().strip()
        if txt:
            return txt
    except Exception:
        pass

    # 2) Fallback: "οποιαδήποτε" 1η γραμμή της στήλης c10
    try:
        cell = page.locator(SEL_DECISION_ANYROW).first
        if cell.count():
            txt = cell.inner_text().strip()
            if txt:
                return txt
    except Exception:
        pass

    # 3) Τελικό fallback: πάρε το τελευταίο κελί της 1ης data row
    try:
        first_row_last_td = page.locator(f"{SEL_GRID_DB} tr >> td").last
        if first_row_last_td.count():
            return first_row_last_td.inner_text().strip()
    except Exception:
        pass

    return None

def main(gak_number: Optional[str] = None, gak_year: Optional[str] = None):
    if gak_number is None: gak_number = "70927"
    if gak_year   is None: gak_year   = "2025"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        ctx = browser.new_context(locale="el-GR", viewport={"width":1500,"height":950})
        page = ctx.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)

        try:
            print("→ Φόρτωση σελίδας…")
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            accept_cookies_if_present(page)

            print("→ Θέτω «Κατάστημα» = ΠΡΩΤΟΔΙΚΕΙΟ ΑΘΗΝΩΝ …")
            select_katastima(page)

            print(f"→ Γ.Α.Κ. Αριθμός = {gak_number}")
            page.fill(SEL_GAK_NUMBER, gak_number)

            print(f"→ Γ.Α.Κ. Έτος = {gak_year}")
            page.fill(SEL_GAK_YEAR, gak_year)

            print("→ Κλικ στο «Αναζήτηση» …")
            click_search(page)

            print("→ Αναμονή για φόρτωση πίνακα …")
            wait_for_table_data(page, timeout_ms=RESULT_TIMEOUT)

            print("→ Ανάγνωση στήλης «Αριθμός Απόφασης/Έτος - Είδος Διατακτικού» …")
            result = read_decision_text(page)

            ensure_artifacts()
            page.screenshot(path="artifacts/solon_after_search.png", full_page=True)

            print("\n=== ΑΠΟΤΕΛΕΣΜΑ ===")
            if result:
                print(result)
                with open("artifacts/solon_result.txt","w",encoding="utf-8") as f:
                    f.write(result)
                print("✔ Αποθηκεύτηκε: artifacts/solon_result.txt")
            else:
                print("⚠ Δεν εντοπίστηκε κείμενο. Δες artifacts/solon_after_search.png")
                with open("artifacts/debug_after.html","w",encoding="utf-8") as f:
                    f.write(page.content())
                print("⚙ DOM dump: artifacts/debug_after.html")

        except PWTimeout as e:
            print(f"⛔ Timeout: {e}")
        except Exception as e:
            print(f"⛔ Σφάλμα: {e}")
            ensure_artifacts()
            with open("artifacts/debug_error.html","w",encoding="utf-8") as f:
                f.write(page.content())
        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        main()

"""Daily official-source monitor. Python 3.12+, no API key required.

Discovery is deliberately conservative: these are candidate program pages,
not a certified, exhaustive tariff database. Prices remain source excerpts.
"""
import argparse
import csv
import hashlib
import html
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

AGENT = "DealPointEnergyMonitor/1.0"
TERMS = ("ρευμα", "αεριο", "τιμολογ", "προγραμμα", "χρεωσ", "electric", "gas", "power", "home", "business", "value", "blue", "yellow", "green", "product", "price", "tariff", "timolog")
SKIP = ("privacy", "cookie", "career", "blog", "news", "login", "contact", "epikoin", "istorik", "archive", "inactive", "anenerga", "ανενεργ", "faq", "search")


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFD", value.lower()) if unicodedata.category(c) != "Mn")


def clean(value):
    return " ".join(value.split())


def safe_url(base, href, host):
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, href))
    if p.scheme != "https" or p.hostname != host or p.username or p.password:
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))


def relevant(url, label=""):
    text = norm(urllib.parse.unquote(url) + " " + label)
    return any(t in text for t in TERMS) and not any(t in text for t in SKIP)


def excerpts(text, kind):
    patterns = {
        "charges": r"€|ευρω|kwh|mwh|κιλοβατω|παγι",
        "duration": r"μην|διαρκ|δεσμευσ|αποχωρησ",
        "benefits": r"δωρ|εκπτω|συνεπει|προυποθ|παγια εντολ",
    }
    lines = [clean(x) for x in text.splitlines() if clean(x)]
    hits = []
    for i, line in enumerate(lines):
        if re.search(patterns[kind], norm(line)):
            # Preserve context: never pick an isolated number as the final price.
            fragment = " | ".join(lines[max(0, i - 1):i + 2])
            if fragment not in hits:
                hits.append(fragment)
    return "\n".join(hits[:20])


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def csv_safe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows({k: csv_safe(row.get(k, "")) for k in fields} for row in rows)


def change_kind(old, content_hash):
    if old is None:
        return "Πρώτη καταγραφή"
    return "Αλλαγή περιεχομένου — έλεγχος" if old["hash"] != content_hash else "Χωρίς αλλαγή"


class Collector:
    def __init__(self):
        self.robots = {}

    def allowed(self, url):
        parsed = urllib.parse.urlsplit(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            request = urllib.request.Request(root + "/robots.txt", headers={"User-Agent": AGENT})
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    rp.parse(response.read(1_000_000).decode("utf-8", "replace").splitlines())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    rp.parse([])
                else:
                    raise RuntimeError(f"robots.txt HTTP {e.code}") from e
            self.robots[root] = rp
        if not self.robots[root].can_fetch(AGENT, url):
            raise RuntimeError("Η πρόσβαση δεν επιτρέπεται από robots.txt")
        return max(2, self.robots[root].crawl_delay(AGENT) or 0)

    def fetch(self, page, url):
        delay = self.allowed(url)
        time.sleep(delay)
        response = page.goto(url, wait_until="networkidle", timeout=45000)
        if response is None or response.status >= 400:
            raise RuntimeError(f"HTTP {response.status if response else 'no response'}")
        if urllib.parse.urlsplit(page.url).hostname != urllib.parse.urlsplit(url).hostname:
            raise RuntimeError("Ανακατεύθυνση εκτός καθορισμένου ιστοτόπου")
        title = page.locator("h1").first.inner_text() if page.locator("h1").count() else page.title()
        # Remove shared navigation so it cannot attach other products' prices.
        body = page.evaluate("""() => {
            const n = (document.querySelector('main') || document.body).cloneNode(true);
            n.querySelectorAll('nav,header,footer,script,style,[role=navigation], [id*=cookie], [class*=cookie]').forEach(e=>e.remove());
            document.body.appendChild(n);
            const text=n.innerText; n.remove(); return text;
        }""")
        if len(clean(body)) < 160 or re.search(r"verify you are human|just a moment|access denied|captcha", body, re.I):
            raise RuntimeError("Κενή σελίδα ή φραγή πρόσβασης")
        links = page.locator("a[href]").evaluate_all("nodes => nodes.map(n=>({href:n.href,label:n.innerText}))")
        return clean(title), body, links


def run(config, out):
    from playwright.sync_api import sync_playwright
    stamp = datetime.now(ZoneInfo("Europe/Athens")).isoformat(timespec="seconds")
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    old = json.loads(state_path.read_text()) if state_path.exists() else {}
    state = dict(old)  # Unreachable sources keep their last successful snapshot.
    rows, health = [], []
    collector = Collector()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(user_agent=AGENT, locale="el-GR")
        page = context.new_page()
        for provider in config["providers"]:
            name, root = provider["name"], provider["root"]
            host = urllib.parse.urlsplit(root).hostname
            queue = list(provider.get("seeds", [root]))
            seen, succeeded, candidates, failures = set(), 0, 0, []
            while queue and len(seen) < config.get("max_pages_per_provider", 45):
                url = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                try:
                    title, body, links = collector.fetch(page, url)
                    succeeded += 1
                    for link in links:
                        target = safe_url(url, link["href"], host)
                        if target and relevant(target, link["label"]) and not target.lower().endswith((".pdf", ".jpg", ".png", ".zip")):
                            if target not in seen and target not in queue:
                                queue.append(target)
                    pdfs = sorted({link["href"] for link in links if safe_url(url, link["href"], host) and ".pdf" in link["href"].lower()})
                    path_parts = urllib.parse.urlsplit(url).path.strip("/").split("/")
                    evidence = excerpts(body, "charges")
                    # Category pages are still candidates, visibly labelled.
                    is_root = not path_parts[0] or (len(path_parts) == 1 and path_parts[0] in ("el", "en", "gr"))
                    if not evidence or is_root or not relevant(url, title):
                        continue
                    candidates += 1
                    key = name + "|" + url
                    fingerprint = digest(clean(body))
                    previous = old.get(key)
                    state[key] = {"hash": fingerprint, "checked_at": stamp, "title": title}
                    row = {"Πάροχος": name, "Σελίδα / υποψήφιο πρόγραμμα": title,
                           "Χρεώσεις όπως δημοσιεύονται": evidence,
                           "Διάρκεια / αποχώρηση": excerpts(body, "duration"),
                           "Προσφορές / προϋποθέσεις": excerpts(body, "benefits"),
                           "Πηγή": url, "Σχετικά PDF": "\n".join(pdfs),
                           "Έλεγχος": stamp, "Μεταβολή": change_kind(previous, fingerprint),
                           "Κατάσταση": "Χρειάζεται επιβεβαίωση προγράμματος και ισχύος τιμών"}
                    rows.append(row)
                    source_dir = out / "sources" / digest(key)[:20]
                    source_dir.mkdir(parents=True, exist_ok=True)
                    (source_dir / "latest.txt").write_text(f"{url}\n{stamp}\n\n{body}", encoding="utf-8")
                except Exception as e:
                    failures.append({"url": url, "error": str(e)[:250]})
            status = "Μερικός έλεγχος" if failures or queue else "Ολοκληρώθηκε η καθορισμένη σάρωση"
            if not candidates:
                status = "Δεν εξήχθησαν προγράμματα — απαιτείται έλεγχος"
            health.append({"provider": name, "status": status, "pages_ok": succeeded,
                           "candidates": candidates, "limit_reached": bool(queue), "errors": failures})
        browser.close()
    fields = ["Πάροχος", "Σελίδα / υποψήφιο πρόγραμμα", "Χρεώσεις όπως δημοσιεύονται", "Διάρκεια / αποχώρηση", "Προσφορές / προϋποθέσεις", "Πηγή", "Σχετικά PDF", "Έλεγχος", "Μεταβολή", "Κατάσταση"]
    write_csv(out / "programs.csv", rows, fields)
    write_csv(out / "changes.csv", [r for r in rows if r["Μεταβολή"] != "Χωρίς αλλαγή"], fields)
    (out / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "programs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(state_path)
    summary = "\n".join(f"{h['provider']}: {h['status']} ({h['candidates']} υποψήφιες σελίδες)" for h in health)
    report = f"<!doctype html><html lang='el'><meta charset='utf-8'><title>Deal Point — καθημερινός έλεγχος</title><style>body{{font:16px system-ui;margin:30px;max-width:1200px}}article{{border:1px solid #ccc;padding:16px;margin:12px 0}}pre{{white-space:pre-wrap}}a{{overflow-wrap:anywhere}}</style><h1>Deal Point — έλεγχος παρόχων</h1><p>{html.escape(stamp)}</p><p>Αυτόματη συλλογή υποψήφιων σελίδων. Δεν αποτελεί επιβεβαιωμένο τιμοκατάλογο. Τα PDF παρατίθενται ως σύνδεσμοι· δεν αναλύονται. Αλλαγή περιεχομένου δεν σημαίνει αλλαγή τιμής. Δεν τεκμηριώνεται διακοπή προγράμματος από απουσία αποτελέσματος.</p><pre>{html.escape(summary)}</pre>"
    for r in rows:
        report += f"<article><h2>{html.escape(r['Πάροχος'])} — {html.escape(r['Σελίδα / υποψήφιο πρόγραμμα'])}</h2><p>{html.escape(r['Μεταβολή'])}</p><a href='{html.escape(r['Πηγή'], quote=True)}'>Επίσημη πηγή</a><pre>{html.escape(r['Χρεώσεις όπως δημοσιεύονται'])}</pre></article>"
    (out / "report.html").write_text(report + "</html>", encoding="utf-8")
    print(summary)
    return 0 if any(h["candidates"] for h in health) else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="providers.json")
    parser.add_argument("--out", default="output")
    args = parser.parse_args()
    raise SystemExit(run(json.loads(Path(args.config).read_text(encoding="utf-8")), Path(args.out)))

#!/usr/bin/env python3
"""
Ball Knowledge-Play – Einsätze, Tore und Titelanzahl ergänzen
=============================================================

Liest players.json, sucht zu jedem Spieler den Wikidata-Eintrag (über QLever) und seine
Wikipedia-Artikel (de, en, it) und liest daraus:

  * Ligaspiele und Ligatore der ganzen Karriere (Summe aller Vereinsstationen der Infobox,
    ohne Jugend; die Infoboxen zählen nur Ligaspiele),
  * wie oft er die fünf erfassten Titel gewonnen hat (Abschnitt „Honours“ der englischen
    Wikipedia). Gezählt werden nur Titelarten, die der Spieler in players.json schon hat –
    so bleibt alles zu Tic-Tac-Toe & Co. passend.

Ergebnis: players.json bekommt einen zusätzlichen Block "stats" (die Spielerliste selbst bleibt
unverändert). Inhalte aus Wikipedia stehen unter CC BY-SA 4.0 – auf der Website muss deshalb
die Quelle genannt werden (index.html macht das automatisch, sobald der Block da ist).

Aufruf:
    python stats_update.py --players players.json --out players.json --report stats_report.txt
    python stats_update.py --limit 300          # Probelauf: nur Bericht, players.json bleibt unberührt

Benötigt nur Python 3.9+ (keine Zusatzpakete).
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

QLEVER = "https://qlever.dev/api/wikidata"
LANGS = ["de", "en", "it"]                      # Reihenfolge = Vorrang bei Einsätzen/Toren
REPO = os.environ.get("GITHUB_REPOSITORY", "j4kobif/Ball-Knowledge-Play")
UA = f"BallKnowledgePlay-DataUpdate/1.0 (https://github.com/{REPO}; football quiz, non-bulk polite client)"

# ---------------------------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------------------------

def normalize(s):
    """Wie normalize() in index.html: Akzente weg, Kleinbuchstaben, nur a-z/0-9."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not ("̀" <= ch <= "ͯ"))
    for a, b in (("ß", "ss"), ("ø", "o"), ("Ø", "o"), ("ł", "l"), ("Ł", "l"), ("đ", "d"), ("Đ", "d"), ("æ", "ae"), ("Æ", "ae")):
        s = s.replace(a, b)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def log(*a):
    print(*a, flush=True)


def http(url, data=None, headers=None, tries=6, timeout=300):
    """GET/POST mit Wiederholung bei 429/5xx/Netzfehlern."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    h = {"User-Agent": UA}
    h.update(headers or {})
    redirects = 0
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc and redirects < 5:
                url = urllib.parse.urljoin(url, loc)              # Umzug des Servers: neue Adresse nehmen, Anfrage wiederholen
                redirects += 1
                log(f"  Weiterleitung nach {url}")
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < tries:
                wait = int(e.headers.get("Retry-After") or 0) or 5 * attempt
                log(f"  HTTP {e.code}, warte {wait}s …")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < tries:
                log(f"  Netzfehler ({e}), neuer Versuch …")
                time.sleep(5 * attempt)
                continue
            raise


# ---------------------------------------------------------------------------------------------
# 1) Wikidata (QLever): Fußballspieler mit Name, Geburtsjahr und Wikipedia-Artikeln
# ---------------------------------------------------------------------------------------------
PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX schema: <http://schema.org/>
"""
Q_PEOPLE = PREFIXES + """SELECT ?item ?born ?de ?en WHERE {
  ?item wdt:P106 wd:Q937857 .
  ?item wdt:P569 ?born .
  OPTIONAL { ?item rdfs:label ?de . FILTER(LANG(?de) = "de") }
  OPTIONAL { ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }
}"""
Q_SITES = PREFIXES + """SELECT ?item ?site ?title WHERE {
  ?item wdt:P106 wd:Q937857 .
  ?article schema:about ?item ; schema:isPartOf ?site ; schema:name ?title .
  VALUES ?site { <https://de.wikipedia.org/> <https://en.wikipedia.org/> <https://it.wikipedia.org/> }
}"""


def rdf_value(term):
    """TSV-Wert von QLever in einfachen Text umwandeln."""
    term = term.strip()
    if term.startswith("<") and term.endswith(">"):
        return term[1:-1]
    m = re.match(r'^"(.*)"(?:@[\w-]+|\^\^<[^>]*>)?$', term, re.S)
    if m:
        v = m.group(1)
        return v.replace('\\"', '"').replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")
    return term


def qlever_tsv(query):
    txt = http(QLEVER, data={"query": query, "action": "tsv_export"}, headers={"Accept": "text/tab-separated-values"}, timeout=900)
    lines = txt.split("\n")
    out = []
    for ln in lines[1:]:                              # erste Zeile = Spaltennamen
        if not ln.strip():
            continue
        out.append([rdf_value(x) for x in ln.split("\t")])
    return out


def qid(uri):
    return uri.rsplit("/", 1)[-1]


def load_wikidata(cache_dir):
    """Liefert {qid: {"years": set, "names": set(norm), "sites": {lang: title}}}."""
    cache = os.path.join(cache_dir, "wikidata_cache.json") if cache_dir else None
    if cache and os.path.exists(cache):
        log("Wikidata: lade Zwischenstand", cache)
        raw = json.load(open(cache, encoding="utf-8"))
        return {k: {"years": set(v["years"]), "names": set(v["names"]), "sites": v["sites"]} for k, v in raw.items()}
    log("Wikidata: Fußballspieler abfragen (QLever) …")
    people = qlever_tsv(Q_PEOPLE)
    log(f"  {len(people):,} Zeilen")
    wd = {}
    for row in people:
        row += [""] * (4 - len(row))
        item, born, de, en = row[:4]
        q = qid(item)
        e = wd.setdefault(q, {"years": set(), "names": set(), "sites": {}})
        m = re.match(r"^\+?(\d{4})-", born)
        if m:
            e["years"].add(int(m.group(1)))
        for nm in (de, en):
            if nm:
                e["names"].add(normalize(nm))
    log("Wikidata: Wikipedia-Artikel abfragen …")
    sites = qlever_tsv(Q_SITES)
    log(f"  {len(sites):,} Zeilen")
    for row in sites:
        if len(row) < 3:
            continue
        item, site, title = row[:3]
        q = qid(item)
        if q not in wd:
            continue
        lang = urllib.parse.urlparse(site).netloc.split(".")[0]
        wd[q]["sites"][lang] = title
        base = re.sub(r"\s*\(.*?\)\s*$", "", title)      # „Thomas Müller (Fußballspieler)“ → „Thomas Müller“
        wd[q]["names"].add(normalize(base))
    if cache:
        os.makedirs(cache_dir, exist_ok=True)
        json.dump({k: {"years": sorted(v["years"]), "names": sorted(v["names"]), "sites": v["sites"]} for k, v in wd.items()},
                  open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return wd


def match_players(players, wd):
    """Ordnet jedem Spieler (Index) höchstens einen Wikidata-Eintrag zu: gleicher Name + gleicher Jahrgang."""
    index = {}
    for q, e in wd.items():
        if not e["sites"]:
            continue
        for nm in e["names"]:
            for y in e["years"]:
                index.setdefault((nm, y), []).append(q)
    links, ambiguous, missing = {}, 0, 0
    for i, p in enumerate(players):
        cands = index.get((normalize(p[0]), p[1]), [])
        cands = list(dict.fromkeys(cands))
        if not cands:
            missing += 1
            continue
        if len(cands) > 1:
            ambiguous += 1
            cands.sort(key=lambda q: (-len(wd[q]["sites"]), int(q[1:]) if q[1:].isdigit() else 0))
        links[i] = cands[0]
    return links, ambiguous, missing


# ---------------------------------------------------------------------------------------------
# 2) Wikipedia: Quelltext der Artikel holen (50 pro Anfrage)
# ---------------------------------------------------------------------------------------------

def fetch_wikitext(lang, titles, delay=0.3):
    """Liefert Paket für Paket {angefragter Titel: Quelltext} (spart Speicher). Folgt Weiterleitungen."""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    titles = list(dict.fromkeys(titles))
    for k in range(0, len(titles), 50):
        chunk = titles[k:k + 50]
        params = {"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                  "format": "json", "formatversion": "2", "redirects": "1", "maxlag": "5", "titles": "|".join(chunk)}
        cont = {}
        result = {}
        mapping = {t: t for t in chunk}
        for _ in range(20):
            for attempt in range(8):
                txt = http(api, data={**params, **cont})
                data = json.loads(txt)
                if data.get("error", {}).get("code") == "maxlag":
                    time.sleep(5 + 5 * attempt)
                    continue
                break
            q = data.get("query", {})
            for n in q.get("normalized", []):
                for t, v in list(mapping.items()):
                    if v == n["from"]:
                        mapping[t] = n["to"]
            for r in q.get("redirects", []):
                for t, v in list(mapping.items()):
                    if v == r["from"]:
                        mapping[t] = r["to"]
            got = {}
            for pg in q.get("pages", []):
                revs = pg.get("revisions")
                if revs:
                    got[pg["title"]] = revs[0].get("slots", {}).get("main", {}).get("content", "")
            for t, v in mapping.items():
                if v in got:
                    result[t] = got[v]
            if "continue" in data:
                cont = data["continue"]
                continue
            break
        yield result
        time.sleep(delay)
        if (k // 50) % 20 == 0:
            log(f"  {lang}: {min(k + 50, len(titles)):,}/{len(titles):,} Artikel")


# ---------------------------------------------------------------------------------------------
# 3) Wikitext auswerten
# ---------------------------------------------------------------------------------------------

def find_template(text, names):
    """Erste Vorlage mit einem der Namen finden; gibt den Inhalt zwischen {{ und }} zurück."""
    pat = r"\{\{\s*(?:" + "|".join(re.escape(n).replace(r"\ ", r"[ _]+") for n in names) + r")\s*(?=[|}\n<])"
    m = re.search(pat, text, re.I)
    if not m:
        return None
    i, depth = m.start(), 0
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
        elif text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return text[m.start() + 2:i - 2]
        else:
            i += 1
    return None


def split_top(body):
    """An | auf oberster Ebene trennen (nicht in {{…}} oder [[…]])."""
    parts, depth_t, depth_l, cur, i = [], 0, 0, [], 0
    while i < len(body):
        two = body[i:i + 2]
        if two == "{{":
            depth_t += 1; cur.append(two); i += 2; continue
        if two == "}}" and depth_t:
            depth_t -= 1; cur.append(two); i += 2; continue
        if two == "[[":
            depth_l += 1; cur.append(two); i += 2; continue
        if two == "]]" and depth_l:
            depth_l -= 1; cur.append(two); i += 2; continue
        ch = body[i]
        if ch == "|" and depth_t == 0 and depth_l == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _eq_top(p):
    """Position des ersten = außerhalb von {{…}}, [[…]] und <…>, sonst -1."""
    depth, i = 0, 0
    while i < len(p):
        two = p[i:i + 2]
        if two in ("{{", "[["):
            depth += 1; i += 2; continue
        if two in ("}}", "]]") and depth:
            depth -= 1; i += 2; continue
        if p[i] == "<":
            j = p.find(">", i)
            if j > 0:
                i = j + 1; continue
        if p[i] == "=" and depth == 0:
            return i
        i += 1
    return -1


def template_params(body):
    parts = split_top(body)
    named, positional = {}, []
    for p in parts[1:]:
        e = _eq_top(p)
        if e > 0 and re.fullmatch(r"[\w \-()äöüÄÖÜß]+", p[:e].strip() or "-"):
            named[p[:e].strip().lower()] = p[e + 1:].strip()
        else:
            positional.append(p.strip())
    return named, positional


def clean(v):
    v = re.sub(r"<ref[^>/]*/>", "", v, flags=re.I)
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.I | re.S)
    v = re.sub(r"<!--.*?-->", "", v, flags=re.S)
    v = re.sub(r"\{\{\s*0+\s*\}\}", "", v)                            # {{0}} = Füllzeichen
    v = re.sub(r"\{\{\s*(?:0|nowrap|nobr|small)\s*\|([^{}]*)\}\}", r"\1", v, flags=re.I)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)
    v = v.replace("&nbsp;", " ").replace("&#160;", " ").replace("'''", "").replace("''", "")
    return v


BR = re.compile(r"<br\s*/?>|\n", re.I)
PAIR = re.compile(r"(\d+)\s*\(\s*([−–-]?\s*\d+)\s*\)")


def _num(s):
    s = s.replace("−", "-").replace("–", "-").replace(" ", "")
    return int(s)


def plausible(g, t):
    return 1 <= g <= 1500 and 0 <= t <= 1000 and t <= g * 1.6 + 5


def parse_de(text):
    body = find_template(text, ["Infobox Fußballspieler"])
    if not body:
        return None
    named, _ = template_params(body)
    val = None
    for k, v in named.items():
        if re.fullmatch(r"spiele\s*\(\s*tore\s*\)", k):
            val = v
            break
    if not val:
        return None
    games = goals = rows = 0
    for line in BR.split(clean(val)):
        line = line.strip()
        if not line:
            continue
        if "?" in line or re.fullmatch(r"[-–—−()\s]+", line):
            return None                                                    # unvollständig → lieber nichts
        m = PAIR.search(line)
        if m:
            games += int(m.group(1)); goals += max(0, _num(m.group(2))); rows += 1
        elif re.fullmatch(r"\d+", line):
            games += int(line); rows += 1
    return (games, goals) if rows and plausible(games, goals) else None


def parse_en(text):
    body = find_template(text, ["Infobox football biography", "Infobox footballer", "Infobox football biography 2"])
    if not body:
        return None
    named, _ = template_params(body)
    games = goals = rows = 0
    for n in range(1, 61):
        caps = named.get(f"caps{n}")
        if caps is None:
            continue
        c = clean(caps).strip()
        g = clean(named.get(f"goals{n}", "")).strip()
        if not c:
            continue
        if "?" in c or "?" in g:
            return None
        mc = re.search(r"\d+", c)
        if not mc:
            if re.fullmatch(r"[-–—−\s]+", c):
                return None                                                # Strich = unbekannt
            continue
        games += int(mc.group())
        mg = re.search(r"[−–-]?\d+", g)
        if mg:
            goals += max(0, _num(mg.group()))
        rows += 1
    if not rows:
        return None
    return (games, goals) if plausible(games, goals) else None


def parse_it(text):
    body = find_template(text, ["Sportivo"])
    if not body:
        return None
    named, _ = template_params(body)
    sq = named.get("squadre")
    if not sq:
        return None
    car = find_template(sq, ["Carriera sportivo"])
    if not car:
        return None
    _, pos = template_params(car)
    games = goals = rows = 0
    for k in range(2, len(pos), 3):                                          # Jahre | Verein | Spiele (Tore)
        cell = clean(pos[k]).strip()
        if not cell:
            continue
        if "?" in cell or re.fullmatch(r"[-–—−()\s]+", cell):
            return None
        m = PAIR.search(cell)
        if m:
            games += int(m.group(1)); goals += max(0, _num(m.group(2))); rows += 1   # negative Zahl = Gegentore (Torwart)
        elif re.fullmatch(r"\d+", cell):
            games += int(cell); rows += 1
    return (games, goals) if rows and plausible(games, goals) else None


PARSERS = {"de": parse_de, "en": parse_en, "it": parse_it}

# ----- Titel zählen (englische Wikipedia, Abschnitt Honours) -----
TITLE_KEYS = {
    "WM-Sieger": "wc", "EM-Sieger": "euro", "CL-Sieger": "cl",
    "Europa-League-Sieger": "el", "Conference-League-Sieger": "ecl",
}
TITLE_RX = {
    "cl": re.compile(r"\b(european cup|champions league)\b", re.I),
    "el": re.compile(r"\b(uefa cup|europa league)\b", re.I),
    "ecl": re.compile(r"\bconference league\b", re.I),
    "wc": re.compile(r"\bfifa world cup\b", re.I),
    "euro": re.compile(r"\b(uefa european championship|uefa euro|european championship|european nations' cup)\b", re.I),
}
BAD_ANY = re.compile(r"runner|second place|third|fourth|finalist|semi|best|player of|team of|goal of|golden|silver|bronze|top ?scorer|"
                     r"all-star|dream team|squad|xi\b|award|ballon|qualif|women|youth|u-?\d\d|under-?\d\d|olympic|super ?cup|"
                     r"cup winners|intertoto|club world cup|confederations|nations league|asian|african|afc|caf|concacaf|"
                     r"conmebol|ofc|libertadores|sudamericana", re.I)
YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})(?:\s*[–\-/]\s*(?:\d{2}|\d{4}))?\b")


def honours_section(text):
    m = re.search(r"^(=+)\s*(honours|honors)\s*\1\s*$", text, re.I | re.M)
    if not m:
        return ""
    level = len(m.group(1))
    rest = text[m.end():]
    nxt = re.search(r"^={1,%d}[^=].*?={1,%d}\s*$" % (level, level), rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def count_titles_en(text, section=False):
    sec = clean(text if section else honours_section(text))
    counts = {k: 0 for k in TITLE_RX}
    for line in sec.split("\n"):
        line = line.strip()
        if not line.startswith("*"):
            continue
        for seg in re.split(r";", line):
            if BAD_ANY.search(seg):
                continue
            for key, rx in TITLE_RX.items():
                if key == "cl" and re.search(r"\bconference league\b", seg, re.I):
                    continue
                if key == "euro" and TITLE_RX["cl"].search(seg):
                    continue
                m = rx.search(seg)
                if m:
                    counts[key] += len(YEAR.findall(seg[m.end():]))
                    break
    return counts


# ---------------------------------------------------------------------------------------------
# 4) Ablauf
# ---------------------------------------------------------------------------------------------

def run(args, fetch=fetch_wikitext, wikidata=None):
    db = json.load(open(args.players, encoding="utf-8"))
    players = db["players"]
    title_names = [t[0] for t in db.get("titles", [])]
    n = len(players)
    log(f"players.json: {n:,} Spieler")

    wd = wikidata if wikidata is not None else load_wikidata(args.cache)
    links, ambiguous, missing = match_players(players, wd)
    log(f"Zuordnung: {len(links):,} gefunden, {missing:,} ohne Treffer, {ambiguous:,} mehrdeutig (bekanntester Eintrag genommen)")

    todo = sorted(links)
    if args.limit:
        rnd = random.Random(7)
        with_titles = [i for i in todo if len(players[i]) > 5 and players[i][5]]
        pick = set(rnd.sample(todo, min(args.limit, len(todo))))
        pick |= set(with_titles[: max(1, args.limit // 5)])
        todo = sorted(pick)
        log(f"Probelauf mit {len(todo):,} Spielern")

    games = [-1] * n
    goals = [-1] * n
    src = [""] * n
    texts_en = {}
    need = set(todo)
    for lang in LANGS:
        want = {}
        for i in todo:
            t = wd[links[i]]["sites"].get(lang)
            if not t:
                continue
            has_titles = len(players[i]) > 5 and bool(players[i][5])
            if i in need or (lang == "en" and has_titles):
                want[i] = t
        if not want:
            continue
        log(f"Wikipedia {lang}: {len(want):,} Artikel laden …")
        by_title = {}
        for i, t in want.items():
            by_title.setdefault(t, []).append(i)
        parsed = 0
        for texts in fetch(lang, list(by_title)):
            for t, tx in texts.items():
                for i in by_title.get(t, []):
                    if lang == "en" and len(players[i]) > 5 and players[i][5]:
                        texts_en[i] = honours_section(tx)          # nur den Titel-Abschnitt behalten
                    if i in need:
                        r = PARSERS[lang](tx)
                        if r:
                            games[i], goals[i] = r
                            src[i] = lang
                            need.discard(i)
                            parsed += 1
        log(f"  {lang}: Einsätze/Tore für {parsed:,} Spieler")

    titles_total = [-1] * n
    title_found = 0
    for i, p in enumerate(players):
        tl = p[5] if len(p) > 5 and p[5] else []
        if not tl:
            continue
        c = count_titles_en(texts_en[i], section=True) if i in texts_en else {}
        total = 0
        for t in set(tl):
            key = TITLE_KEYS.get(title_names[t]) if t < len(title_names) else None
            k = c.get(key, 0) if key else 0
            if k > 1:
                title_found += 1
            total += max(1, min(k, 15))
        titles_total[i] = total

    have = sum(1 for g in games if g >= 0)
    today = dt.date.today().isoformat()
    lines = [
        f"Ball Knowledge-Play – Einsätze/Tore/Titel, Stand {today}",
        f"Spieler gesamt: {n:,}",
        f"Wikidata-Zuordnung: {len(links):,} ({len(links) / n:.1%}), ohne Treffer {missing:,}, mehrdeutig {ambiguous:,}",
        f"Bearbeitet: {len(todo):,}" + (" (Probelauf)" if args.limit else ""),
        f"Ligaspiele/Tore gefunden: {have:,} ({have / max(1, len(todo)):.1%} der bearbeiteten)",
        "  nach Sprache: " + ", ".join(f"{l}: {src.count(l):,}" for l in LANGS),
        f"Spieler mit Titeln: {sum(1 for x in titles_total if x >= 0):,}, davon mit Mehrfachtitel aus Honours: {title_found:,}",
        "",
        "Stichprobe (Spiele / Tore / Titel gesamt / Quelle):",
    ]
    sample = [i for i in todo if games[i] >= 0]
    sample.sort(key=lambda i: -games[i])
    for i in sample[:25] + random.Random(1).sample(sample, min(25, len(sample))):
        lines.append(f"  {players[i][0]} ({players[i][1]}): {games[i]} / {goals[i]} / {titles_total[i]} / {src[i]}")
    lines.append("")
    lines.append("Ohne Einsatzdaten (Auswahl):")
    for i in [i for i in todo if games[i] < 0][:40]:
        q = links.get(i)
        lines.append(f"  {players[i][0]} ({players[i][1]}) {q or ''} {sorted(wd[q]['sites']) if q else ''}")
    report = "\n".join(lines)
    log("\n" + report)
    if args.report:
        open(args.report, "w", encoding="utf-8").write(report + "\n")

    if args.limit:
        log("\nProbelauf: players.json wurde nicht verändert.")
        return db
    db["stats"] = {
        "source": f"Wikipedia (de, en, it), CC BY-SA 4.0, Stand {today}",
        "license": "CC BY-SA 4.0 – https://creativecommons.org/licenses/by-sa/4.0/",
        "note": "games/goals = Ligaspiele/-tore der Karriere laut Wikipedia-Infobox (-1 = unbekannt); titles = Anzahl gewonnener Titel der erfassten Titelarten (-1 = keine).",
        "games": games, "goals": goals, "titles": titles_total,
    }
    out = args.out or args.players
    with open(out, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    log(f"\nGeschrieben: {out} ({os.path.getsize(out) / 1e6:.2f} MB)")
    return db


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--players", default="players.json")
    ap.add_argument("--out", default=None, help="Ausgabedatei (Standard: --players überschreiben)")
    ap.add_argument("--report", default="stats_report.txt")
    ap.add_argument("--limit", type=int, default=0, help="Probelauf mit N Spielern (schreibt nur den Bericht)")
    ap.add_argument("--cache", default=".cache", help="Ordner für Zwischenstände (leer = aus)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    sys.exit(main())

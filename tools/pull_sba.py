#!/usr/bin/env python3
"""Pull official state results feeds and match them to the SBA pick list.

civicAPI could not be relied on, so results come straight from each state's
own election authority. Several states publish on the same platform (the one
behind electionresults.utah.gov and results.sos.ga.gov), which exposes:

    /results/public/api/jurisdictions/<Jurisdiction>          -> election ids
    /results/public/api/elections/<Jurisdiction>/<id>/data    -> every contest

Adding a state on that platform is one entry in STATES below. States that
publish differently need their own fetch function and are simply absent until
one is written — the page shows those picks as "state feed not wired up yet"
rather than pretending there is no result.

Runs in GitHub Actions, where there is no CORS and no rate limiting.
"""
import json, os, re, sys, datetime, urllib.request, urllib.error
import http.cookiejar
import time
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# --- states on the shared platform ------------------------------------------
# host:     where that state publishes
# juris:    jurisdiction segment in the API path
# election: the publicElectionId for Nov 3 2026 (discovered from /jurisdictions)
# house / senate: regex capturing the district number from the contest name
STATES = {
    "GA": {
        "host": "results.sos.ga.gov",
        "juris": "Georgia",
        "election": "11032026GeneralAndSpecialElection",
        "house": r"^State House of Representatives\s*-\s*District\s*(\d+)",
        "senate": r"^State Senate\s*-\s*District\s*(\d+)",
    },
    "AL": {"fetch": "alabama"},
    "NY": {"fetch": "newyork"},
}

TIMEOUT = 45

# --- states that publish their own way -------------------------------------
# Alabama's election-night site is ASP.NET: 20 pages of contests reachable only
# by replaying the pager's postback, with no plain URL per page. So walk it the
# way a browser would, carrying the viewstate forward.
AL_URL = ("https://www2.alabamavotes.gov/electionNight/"
          "statewideResultsByContest.aspx?ecode=1001305")
AL_MAX_PAGES = 30
AL_REQ_TIMEOUT = 20      # per request; the site is slow under election-night load
AL_BUDGET = 100          # seconds for the whole walk, so one slow state can't
                         # stall the cycle and hold up the states that do answer


def get_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "sba-election-results/1.0 (github actions)",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def en(items):
    if isinstance(items, str):
        return items.strip()
    for it in (items or []):
        if it.get("languageId") == "en":
            return (it.get("text") or "").strip()
    return ""


PARTY_IN_NAME = re.compile(r"\((I)\)|\((Rep|Dem|Lib|Ind|Grn|Const|Ref|Wfp|Con)\)", re.I)
PARTY_CODE = {"rep": "R", "dem": "D", "lib": "L", "ind": "I", "grn": "G",
              "const": "C", "ref": "RF", "wfp": "WF", "con": "C",
              "republican": "R", "democratic": "D", "democrat": "D",
              "libertarian": "L", "independent": "I", "green": "G"}


def split_candidate(raw, party_obj):
    """Return (clean name, party code, is_incumbent).

    Some states put party and incumbency in the name — "Mike Cameron (I)(Rep)" —
    and others supply a party object. Handle both.
    """
    incumbent = False
    party = ""
    if party_obj:
        party = PARTY_CODE.get(en(party_obj.get("name")).lower(), "")
    for m in PARTY_IN_NAME.finditer(raw or ""):
        if m.group(1):
            incumbent = True
        elif m.group(2):
            party = party or PARTY_CODE.get(m.group(2).lower(), m.group(2)[:1].upper())
    name = re.sub(r"\((?:I|Rep|Dem|Lib|Ind|Grn|Const|Ref|Wfp|Con)\)", "", raw or "", flags=re.I)
    return re.sub(r"\s+", " ", name).strip(), party, incumbent


# --- name matching -----------------------------------------------------------
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv)\b")


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[\"'’.]", "", s)
    s = re.sub(r"[^a-z ]", " ", s)
    s = SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def surname(s):
    parts = norm(s).split()
    return parts[-1] if parts else ""


def forename(s):
    parts = norm(s).split()
    return parts[0] if parts else ""


def same_person(a, b):
    """Surname must match; forenames must be compatible (nicknames, initials)."""
    if not a or not b:
        return False
    if surname(a) != surname(b):
        return False
    fa, fb = forename(a), forename(b)
    if not fa or not fb:
        return True
    if fa == fb or fa.startswith(fb) or fb.startswith(fa):
        return True
    return fa[0] == fb[0] and (len(fa) <= 2 or len(fb) <= 2)


class _ALParser(HTMLParser):
    """Pull contests and candidate rows out of one Alabama results page.

    Each contest is a <td class="enrContestHeader"> followed by candidate rows
    whose cells carry enrCandNameCol / enrCandVotePerCol / enrCandVoteNumCol.
    """

    def __init__(self):
        HTMLParser.__init__(self)
        self.contests = []        # [[name, [[cand, pct, votes], ...]], ...]
        self._cell = None
        self._buf = []
        self._row = {}
        self.hidden = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value", "")
            return
        if tag != "td":
            return
        cls = a.get("class", "")
        if "enrContestHeader" in cls:
            self._cell = "header"
        elif "enrCandNameCol" in cls and "enrCandidatesHeader" not in cls:
            self._cell = "name"
        elif "enrCandVotePerCol" in cls and "enrCandidatesHeader" not in cls:
            self._cell = "pct"
        elif "enrCandVoteNumCol" in cls and "enrCandidatesHeader" not in cls:
            self._cell = "votes"
        else:
            self._cell = None
        self._buf = []

    def handle_data(self, data):
        if self._cell:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag != "td" or not self._cell:
            return
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if self._cell == "header":
            if text:
                self.contests.append([text, []])
        elif self._cell == "name":
            self._row = {"name": text}
        elif self._cell == "pct":
            self._row["pct"] = text
        elif self._cell == "votes":
            self._row["votes"] = text
            if self.contests and self._row.get("name"):
                self.contests[-1][1].append(
                    [self._row["name"], self._row.get("pct", ""), text])
            self._row = {}
        self._cell = None
        self._buf = []


AL_PARTY = {"REP": "R", "DEM": "D", "LIB": "L", "IND": "I", "GRN": "G", "CON": "C"}


def _al_num(s):
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else 0


def _al_export(opener, hdrs, html):
    """Alabama's own Export Data control: every contest in one xlsx.

    The page's pager is postback-only and walking its 20 pages took longer than
    a polling cycle. This control returns the whole election in one request, so
    Alabama costs two requests instead of twenty.
    """
    import urllib.parse
    p = _ALParser()
    p.feed(html)
    form = dict(p.hidden)
    form["__EVENTTARGET"] = "hlnkExportData"
    form["__EVENTARGUMENT"] = ""
    req = urllib.request.Request(AL_URL, data=urllib.parse.urlencode(form).encode(),
                                 headers=dict(hdrs))
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=AL_REQ_TIMEOUT) as r:
        blob = r.read()
        ctype = r.headers.get("Content-Type", "")
    if "spreadsheet" not in ctype and not blob.startswith(b"PK"):
        raise ValueError("export did not return a workbook (%s)" % ctype[:40])

    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    rows = []
    for row in ws.iter_rows(values_only=True):
        if row and any(c not in (None, "") for c in row):
            rows.append(["" if c is None else str(c).strip() for c in row])
    wb.close()
    if not rows:
        raise ValueError("export workbook was empty")

    header = [h.lower() for h in rows[0]]
    print("AL export: %d rows, columns = %s" % (len(rows), rows[0]), file=sys.stderr)
    for sample in rows[1:4]:
        print("AL sample : %s" % (sample,), file=sys.stderr)

    def col(*names):
        """First header equal to a preferred name, else first containing one.

        Order matters: Alabama ships both 'Contest Code' and 'Contest Title',
        and both 'Candidate Number' and 'Candidate Name', so a bare substring
        match picks the numeric column and every contest collapses to a code.
        """
        for n in names:
            for i, h in enumerate(header):
                if h == n:
                    return i
        for n in names:
            for i, h in enumerate(header):
                if n in h:
                    return i
        return None

    i_contest = col("contest title", "contest name", "office", "contest")
    i_cand = col("candidate name", "candidate")
    i_party = col("party code", "party")
    i_votes = col("votes", "vote")
    if i_contest is None or i_cand is None or i_votes is None:
        raise ValueError("unexpected export columns: %s" % rows[0])

    # The export may be county-level; fold it to one row per candidate.
    tally = {}
    order = []
    for r in rows[1:]:
        if max(i_contest, i_cand, i_votes) >= len(r):
            continue
        contest = r[i_contest].strip()
        cand = r[i_cand].strip()
        if not contest or not cand:
            continue
        party = r[i_party].strip() if i_party is not None and i_party < len(r) else ""
        key = (contest, cand)
        if key not in tally:
            tally[key] = {"party": party, "votes": 0}
            order.append(key)
        tally[key]["votes"] += _al_num(r[i_votes])

    contests = {}
    for contest, cand in order:
        contests.setdefault(contest, []).append(
            (cand, tally[(contest, cand)]["party"], tally[(contest, cand)]["votes"]))
    return contests


def fetch_alabama():
    """Alabama's results, by its own export; falls back to walking the pager."""
    import urllib.parse
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    hdrs = {"User-Agent": "sba-election-results/1.0 (github actions)"}

    def get(url, body=None):
        req = urllib.request.Request(url, data=body, headers=dict(hdrs))
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with opener.open(req, timeout=AL_REQ_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")

    started = time.time()
    html = get(AL_URL)
    index, every, seen = {}, [], set()

    try:
        contests = _al_export(opener, hdrs, html)
        for name, rows in contests.items():
            cands = []
            for cname, pty_raw, votes in rows:
                m = re.match(r"^(.*?)\s*\((\w+)\)\s*$", cname)
                nm = (m.group(1) if m else cname).strip()
                code = (m.group(2) if m else pty_raw or "").upper()
                cands.append({"n": nm, "p": AL_PARTY.get(code, ""), "inc": False,
                              "v": votes, "w": False,
                              "wi": bool(re.match(r"^write[- ]?in$", nm, re.I))})
            if not cands:
                continue
            total = sum(c["v"] for c in cands)
            every.append({"st": "AL", "n": name, "d": district_of(name),
                          "ru": 0, "tu": 0, "v": total,
                          "c": [[c["n"], c["p"], c["v"], 0, 0, 1 if c["wi"] else 0]
                                for c in cands]})
            race = {"race": name, "v": total, "ru": 0, "tu": 0, "c": cands}
            m = re.match(r"^STATE REPRESENTATIVE,?\s*DISTRICT\s*(\d+)", name, re.I)
            if m:
                index[("State House", m.group(1))] = race
            else:
                m = re.match(r"^STATE SENATOR,?\s*DISTRICT\s*(\d+)", name, re.I)
                if m:
                    index[("State Senate", m.group(1))] = race
        print("AL: export gave %d contests, %d legislative districts (%.0fs)"
              % (len(every), len(index), time.time() - started), file=sys.stderr)
        if index:
            return index, every, None
        print("AL: export had no legislative districts — walking the pager",
              file=sys.stderr)
        index, every, seen = {}, [], set()
    except Exception as e:
        print("AL: export failed (%s) — walking the pager" % str(e)[:90],
              file=sys.stderr)

    for page_no in range(AL_MAX_PAGES):
        if time.time() - started > AL_BUDGET:
            print("AL: budget reached after %d pages, publishing what was read"
                  % page_no, file=sys.stderr)
            break
        p = _ALParser()
        p.feed(html)

        for name, rows in p.contests:
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cands = []
            for cname, pct, votes in rows:
                m = re.match(r"^(.*?)\s*\((\w+)\)\s*$", cname)
                nm = (m.group(1) if m else cname).strip()
                pty = AL_PARTY.get((m.group(2).upper() if m else ""), "")
                if not nm:
                    continue
                cands.append({"n": nm, "p": pty, "inc": False,
                              "v": _al_num(votes), "w": False,
                              "wi": bool(re.match(r"^write[- ]?in$", nm, re.I))})
            if not cands:
                continue
            total = sum(c["v"] for c in cands)
            race = {"race": name, "v": total, "ru": 0, "tu": 0, "c": cands}

            every.append({
                "st": "AL", "n": name, "d": district_of(name),
                "ru": 0, "tu": 0, "v": total,
                "c": [[c["n"], c["p"], c["v"], 0, 0, 1 if c["wi"] else 0]
                      for c in cands],
            })

            m = re.match(r"^STATE REPRESENTATIVE,?\s*DISTRICT\s*(\d+)", name, re.I)
            if m:
                index[("State House", m.group(1))] = race
            else:
                m = re.match(r"^STATE SENATOR,?\s*DISTRICT\s*(\d+)", name, re.I)
                if m:
                    index[("State Senate", m.group(1))] = race

        pg = re.search(r"Page:\s*(\d+)\s*of\s*(\d+)", html, re.I)
        if pg:
            print("AL: page %s of %s, %d contests so far (%.0fs)"
                  % (pg.group(1), pg.group(2), len(every), time.time() - started),
                  file=sys.stderr)
        if not pg or int(pg.group(1)) >= int(pg.group(2)):
            break
        form = dict(p.hidden)
        form["__EVENTTARGET"] = "cmdNext"
        form["__EVENTARGUMENT"] = ""
        html = get(AL_URL, urllib.parse.urlencode(form).encode())

    return index, every, None


NY_URL = "https://nyenr.elections.ny.gov/"
NY_CONTEST = {"State Senate": "5", "State House": "6"}   # NY's lower house is the Assembly


class _NYParser(HTMLParser):
    """Rows of New York's results tables, plus the form's hidden fields."""

    def __init__(self):
        HTMLParser.__init__(self)
        self.rows = []          # [[cell, ...], ...] in document order
        self.hidden = {}
        self._cells = None
        self._buf = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value", "")
        elif tag == "tr":
            self._cells = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._buf = []

    def handle_data(self, data):
        if self._in_cell:
            self._buf.append(data)

    def handle_entityref(self, name):
        if self._in_cell and name == "nbsp":
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            if self._cells is not None:
                self._cells.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            self._in_cell = False
            self._buf = []
        elif tag == "tr" and self._cells is not None:
            if any(self._cells):
                self.rows.append(self._cells)
            self._cells = None


NY_HEAD = re.compile(r"(\d+)\w\w\s+(Senate|Assembly)\s+District", re.I)
NY_REPORTING = re.compile(r"Districts Reporting:\s*([\d,]+)\s*of\s*([\d,]+)", re.I)
NY_PARTY = {"DEM": "D", "REP": "R", "CON": "C", "WOR": "WF", "LBT": "L", "GRE": "G"}


def fetch_newyork():
    """New York, one request per chamber.

    New York uses fusion voting: a candidate appears on several party lines and
    the table repeats them. The per-candidate column is the real total, so read
    that and ignore the party-line rows, plus the Blank and Void rows.
    """
    import urllib.parse
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    hdrs = {"User-Agent": "sba-election-results/1.0 (github actions)"}

    def post(form):
        req = urllib.request.Request(
            NY_URL, data=urllib.parse.urlencode(form).encode(), headers=dict(hdrs))
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")

    with opener.open(urllib.request.Request(NY_URL, headers=dict(hdrs)),
                     timeout=TIMEOUT) as r:
        html = r.read().decode("utf-8", errors="replace")

    index, every = {}, []
    for chamber, code in NY_CONTEST.items():
        p = _NYParser(); p.feed(html)
        base = dict(p.hidden)

        # Choosing a contest repopulates the district list, so take that step
        # on its own rather than posting a district the page hasn't offered yet.
        form = dict(base)
        form.update({"__EVENTTARGET": "ctl00$HomePage$ddContest", "__EVENTARGUMENT": "",
                     "ctl00$HomePage$ddContest": code,
                     "ctl00$HomePage$ddDistrict": "0",
                     "ctl00$HomePage$ddCountyBreakDown": "N"})
        step = post(form)

        p2 = _NYParser(); p2.feed(step)
        form = dict(p2.hidden)
        form.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "",
                     "ctl00$HomePage$ddContest": code,
                     "ctl00$HomePage$ddDistrict": "-1",     # every district at once
                     "ctl00$HomePage$ddCountyBreakDown": "N",
                     "ctl00$HomePage$btnGo": "Search"})
        page = post(form)

        p3 = _NYParser(); p3.feed(page)
        cur = None
        for row in p3.rows:
            if len(row) == 1:
                m = NY_HEAD.search(row[0])
                if m:
                    rep = NY_REPORTING.search(row[0])
                    cur = {"dist": m.group(1), "name": m.group(0),
                           "ru": _al_num(rep.group(1)) if rep else 0,
                           "tu": _al_num(rep.group(2)) if rep else 0,
                           "c": []}
                    label = "%s %s District %s" % ("New York", m.group(2), m.group(1))
                    cur["label"] = label
                    index_key = (chamber, m.group(1))
                    cur["key"] = index_key
                continue
            if cur is None or len(row) < 8:
                continue
            nm = row[0].strip()
            if not nm or nm.lower() in ("candidate", "blank", "void"):
                continue
            cur["c"].append({"n": nm, "p": NY_PARTY.get(row[1].strip().upper(), ""),
                             "inc": False, "v": _al_num(row[7]), "w": False,
                             "wi": bool(re.match(r"^write[- ]?in", nm, re.I))})
            # keep the newest version of this district
            total = sum(c["v"] for c in cur["c"])
            race = {"race": cur["label"], "v": total,
                    "ru": cur["ru"], "tu": cur["tu"], "c": list(cur["c"])}
            index[cur["key"]] = race
            cur["race"] = race

    for (chamber, dist), race in index.items():
        every.append({"st": "NY", "n": race["race"], "d": dist,
                      "ru": race["ru"], "tu": race["tu"], "v": race["v"],
                      "c": [[c["n"], c["p"], c["v"], 0, 0, 1 if c["wi"] else 0]
                            for c in race["c"]]})
    print("NY: %d districts indexed" % len(index), file=sys.stderr)
    return index, every, None


SOURCE_PAGES = {
    "AL": "https://www2.alabamavotes.gov/electionNight/"
          "statewideResultsByContest.aspx?ecode=1001305",
    "NY": NY_URL,
}


def source_url(ab):
    """Where a state's numbers come from, for the page's own attribution."""
    if ab in SOURCE_PAGES:
        return SOURCE_PAGES[ab]
    cfg = STATES.get(ab) or {}
    if cfg.get("host") and cfg.get("juris"):
        return "https://%s/results/public/%s" % (cfg["host"], cfg["juris"])
    return ""


def district_of(name):
    m = re.search(r"District\s*(\d+[A-Z]?)\s*$", name, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(\d+[A-Z]?)\s*$", name)
    return m.group(1) if m else ""


def fetch_state(ab, cfg):
    """Return (pick index, every contest, asOf).

    The pick index is keyed by (chamber, district) for scoring the SBA list.
    The contest list is every race the state publishes, which the tracker needs
    to draw its map and seat counts — not just the ones SBA backs.
    """
    base = "https://%s/results/public/api" % cfg["host"]
    data = get_json("%s/elections/%s/%s/data" % (base, cfg["juris"], cfg["election"]))
    items = data.get("ballotItems") or []
    house_re = re.compile(cfg["house"], re.I)
    senate_re = re.compile(cfg["senate"], re.I)

    index, every = {}, []
    as_of = None
    for x in items:
        name = en(x.get("name"))
        rs = x.get("reportingStatus") or {}
        if rs.get("asOf") and (as_of is None or rs["asOf"] > as_of):
            as_of = rs["asOf"]

        opts = ((x.get("summaryResults") or {}).get("ballotOptions")) or []
        cands = []
        for o in opts:
            nm, pty, inc = split_candidate(en(o.get("name")), o.get("party"))
            cands.append({
                "n": nm, "p": pty, "inc": inc,
                "v": o.get("voteCount") or 0,
                "w": bool(o.get("isWinner")),
                "wi": bool(o.get("isWriteIn")),
            })

        race = {
            "race": name,
            "v": x.get("voteTotal") or 0,
            "ru": rs.get("reportingUnits") or 0,
            "tu": rs.get("totalUnits") or 0,
            "c": cands,
        }

        every.append({
            "st": ab,
            "n": name,
            "d": district_of(name),
            "ru": race["ru"],
            "tu": race["tu"],
            "v": race["v"],
            "c": [[c["n"], c["p"], c["v"], 1 if c["w"] else 0,
                   1 if c["inc"] else 0, 1 if c["wi"] else 0] for c in cands],
        })

        m = house_re.match(name)
        if m:
            index[("State House", m.group(1))] = race
        else:
            m = senate_re.match(name)
            if m:
                index[("State Senate", m.group(1))] = race

    return index, every, as_of


def assess(pick, race):
    """Where the SBA candidate stands in their race."""
    if race is None:
        return {"st": "nofeed"}
    cands = race["c"]
    mine = next((c for c in cands if same_person(c["n"], pick["name"])), None)
    if mine is None:
        return {"st": "nomatch", "ballot": [c["n"] for c in cands]}

    total = sum(c["v"] for c in cands)
    out = {
        "race": race["race"], "feedName": mine["n"], "party": mine["p"],
        "inc": mine["inc"], "v": mine["v"], "total": total,
        "ru": race["ru"], "tu": race["tu"],
        "opp": None, "oppV": None, "margin": None, "pct": None, "oppPct": None,
    }
    if total <= 0:
        out["st"] = "unopposed" if len([c for c in cands if not c["wi"]]) <= 1 else "notyet"
        return out

    ranked = sorted(cands, key=lambda c: -c["v"])
    rank = ranked.index(mine) + 1
    out["pct"] = round(mine["v"] / total * 100, 1)
    # The nearest rival: runner-up when we lead, leader when we don't. A
    # single-candidate ballot has neither, so leave the comparison empty.
    if rank == 1:
        rival = ranked[1] if len(ranked) > 1 else None
    else:
        rival = ranked[0]
    if rival is not None:
        out["opp"] = rival["n"]
        out["oppV"] = rival["v"]
        out["oppPct"] = round(rival["v"] / total * 100, 1)
        out["margin"] = mine["v"] - rival["v"]

    if any(c["w"] for c in cands):
        out["st"] = "won" if mine["w"] else "lost"
    elif len([c for c in cands if not c["wi"]]) <= 1:
        out["st"] = "unopposed"
    else:
        out["st"] = "leading" if rank == 1 else "trailing"
    return out


def main():
    with open(os.path.join(ROOT, "picks.json")) as f:
        picks = json.load(f)

    state_data, every_race, errors, as_of = {}, [], {}, None
    for ab, cfg in STATES.items():
        try:
            if cfg.get("fetch") == "alabama":
                index, every, a = fetch_alabama()
            elif cfg.get("fetch") == "newyork":
                index, every, a = fetch_newyork()
            else:
                index, every, a = fetch_state(ab, cfg)
            state_data[ab] = index
            every_race.extend(every)
            if a and (as_of is None or a > as_of):
                as_of = a
            print("%s: %d contests, %d legislative districts indexed"
                  % (ab, len(every), len(index)))
        except Exception as e:
            errors[ab] = str(e)[:120]
            print("%s: FAILED — %s" % (ab, e), file=sys.stderr)

    rows = []
    for p in picks:
        row = {"st": p["st"], "type": p["type"], "dist": p.get("dist"),
               "name": p.get("name") or ("%s %s" % (p.get("measure", ""), p.get("side", ""))).strip(),
               "polled": bool(p.get("polled"))}
        if p["type"] == "Ballot Measure":
            row.update({"st_": "nofeed", "measure": p.get("measure"), "side": p.get("side")})
            row["r"] = {"st": "nofeed"}
        elif p["st"] not in state_data:
            row["r"] = {"st": "nowire"} if p["st"] not in errors else {"st": "error"}
        else:
            row["r"] = assess(p, state_data[p["st"]].get((p["type"], str(p["dist"]))))
        rows.append(row)

    wired = sorted(state_data.keys())
    out = {
        "pulled": datetime.datetime.now(datetime.timezone.utc)
                      .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "asOf": as_of,
        "wired": wired,
        "errors": errors,
        "sources": {ab: source_url(ab) for ab in wired},
        "counts": {
            "total": len(rows),
            "withResult": sum(1 for r in rows if r["r"].get("st") in
                              ("leading", "trailing", "won", "lost", "unopposed")),
            "races": len(every_race),
        },
        "rows": rows,
        # Every contest the wired states publish, for the tracker's own map and
        # seat counts. The board only needs `rows`; the tracker needs these.
        "races": every_race,
    }

    path = os.path.join(ROOT, "results.json")
    # Don't let a failed or partial pull replace a fuller published file.
    if os.path.exists(path) and not state_data:
        print("no state returned data — keeping the published file", file=sys.stderr)
        return 0
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote results.json — %d picks, %d with results, states: %s"
          % (len(rows), out["counts"]["withResult"], ", ".join(wired) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

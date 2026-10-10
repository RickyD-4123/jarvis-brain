"""Listings: finding internship postings that match config.yaml, from public, permitted sources only.

Sources (each a Source with fetch(web) -> list of Listing):
  GreenhouseSource  the public Job Board API (boards-api.greenhouse.io/v1/boards/<board>/jobs),
                    one request per board in greenhouse_boards.
  LeverSource       the public Postings API (api.lever.co/v0/postings/<company>), per lever_companies.
  ManualSource      links you paste into data/manual_urls.txt. Greenhouse/Lever links go through
                    their APIs; other pages are read (schema.org JobPosting data, else the page
                    title) only when robots.txt allows and no login is needed. Otherwise, and always
                    for manual_only_domains, the link is saved unread for you to apply by hand.

Every request goes through Web: robots.txt is checked first (and its Crawl-delay honoured), each
site gets at least min_seconds_between_requests between requests, the whole run stops at
max_requests_per_run, and the User-Agent says who is asking. Nothing logs in, ever.

Board results are filtered to internship / co-op roles matching target_roles, locations and
remote_preference (your pasted links are kept whatever they say: you chose them). Listings are
deduplicated by company + title + URL and stored in data/listings.db (SQLite).

    python -m internship_apply.listings      # one real run with config.yaml; prints the report
"""
import dataclasses
import datetime
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser

from internship_apply import DATA_DIR, load_config
from internship_apply.log import get_logger

log = get_logger("listings")

DB_PATH = os.path.join(DATA_DIR, "listings.db")
MANUAL_URLS_PATH = os.path.join(DATA_DIR, "manual_urls.txt")
AGENT = "JarvisInternshipFinder"
VERSION = "0.1"
MAX_BODY = 20 * 1024 * 1024
DESCRIPTION_CHARS = 20000


@dataclass
class Listing:
    id: str
    company: str
    title: str
    location: str | None
    remote: str                       # "remote" | "hybrid" | "onsite" | "unknown"
    url: str
    source: str                       # "greenhouse" | "lever" | "manual"
    posted_date: str | None           # YYYY-MM-DD
    deadline: str | None              # YYYY-MM-DD
    description: str                  # plain text
    requirements: list[str]
    application_type: str             # "Greenhouse" | "Lever" | "Workday" | "Ashby" | ... | "company site" | "email"
    manual_only: bool = False         # a person applies by hand: never automated
    note: str | None = None           # why it wasn't read, etc.
    apply_url: str | None = None      # the application form itself, when the board gives it (else url)


def dedup_key(company, title, url):
    norm = lambda s: " ".join((s or "").lower().split())
    return "|".join((norm(company), norm(title), canonical_url(url)))


def canonical_url(url):
    """Lower-case host, no fragment, no tracking parameters, no trailing slash."""
    parts = urllib.parse.urlsplit((url or "").strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "gh_src", "lever-source", "ref"))]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                                    urllib.parse.urlencode(query), ""))


def make_listing(**kw):
    kw["id"] = hashlib.sha1(dedup_key(kw["company"], kw["title"], kw["url"]).encode()).hexdigest()[:16]
    kw["description"] = (kw.get("description") or "")[:DESCRIPTION_CHARS]
    return Listing(**kw)


# ---------------------------------------------------------------- the polite web client

class BudgetExhausted(Exception):
    pass


class Blocked(Exception):
    """robots.txt forbids this URL, or it needs a login: not read."""


@dataclass
class Response:
    status: int
    url: str                          # after redirects
    body: bytes
    content_type: str = ""
    retry_after: float | None = None  # seconds, from a 429/503's Retry-After header

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def text(self):
        return self.body.decode("utf-8", errors="replace")


def urllib_transport(url, headers, timeout=20):
    """The real network. Tests pass their own transport with the same shape."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise OSError(f"response from {url} is larger than {MAX_BODY // 1024 // 1024} MB")
            return Response(r.status, r.geturl(), body, r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        after = (e.headers.get("Retry-After") or "").strip()
        return Response(e.code, e.geturl() or url, e.read(65536) if e.fp else b"", e.headers.get("Content-Type", ""),
                        float(after) if after.isdigit() else None)


RETRIES = 3                       # attempts per request (each one counts toward max_requests_per_run)
BACKOFF = 2.0                     # seconds before the first retry; doubles each time
MAX_WAIT = 60.0                   # never wait longer than this, whatever Retry-After says
RETRY_STATUS = {429, 500, 502, 503, 504}


class Web:
    def __init__(self, config, transport=None, clock=time.monotonic, sleep=time.sleep):
        contact = (config.get("user_agent_contact") or "").strip()
        self.user_agent = f"{AGENT}/{VERSION} (personal internship search; one person, low volume" + \
                          (f"; contact {contact}" if contact else "") + ")"
        self.min_gap = float(config.get("min_seconds_between_requests", 2))
        self.budget = int(config.get("max_requests_per_run", 100))
        self.transport = transport or (lambda url, headers: urllib_transport(url, headers))   # looked up at call time
        self.clock, self.sleep = clock, sleep
        self.requests = 0
        self.retries = 0
        self.robots = {}                  # origin -> RobotFileParser (or "all" / "none")
        self.last = {}                    # host -> clock() of the last request

    def _once(self, url, accept):
        if self.requests >= self.budget:
            raise BudgetExhausted(f"stopped at max_requests_per_run ({self.budget})")
        host = urllib.parse.urlsplit(url).netloc.lower()
        gap = max(self.min_gap, self._crawl_delay(url))
        wait = self.last.get(host, -1e9) + gap - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.requests += 1
        try:
            return self.transport(url, {"User-Agent": self.user_agent, "Accept": accept})
        finally:
            self.last[host] = self.clock()

    def _raw_get(self, url, accept):
        """One request with retries: network errors, timeouts, 429 and 5xx are retried with exponential
        backoff (2 s, 4 s, ...; a site's Retry-After is honoured up to MAX_WAIT). Never past the budget."""
        delay = BACKOFF
        for attempt in range(1, RETRIES + 1):
            try:
                r = self._once(url, accept)
            except BudgetExhausted:
                raise
            except OSError as e:                  # includes timeouts and connection errors
                if attempt == RETRIES:
                    log.warning("giving up on %s after %d attempts: %s", url, attempt, e)
                    raise
                log.info("retrying %s after %s (attempt %d)", url, type(e).__name__, attempt)
            else:
                if r.status not in RETRY_STATUS or attempt == RETRIES:
                    return r
                log.info("retrying %s after HTTP %s (attempt %d)", url, r.status, attempt)
                after = (getattr(r, "retry_after", None) or 0)
                delay = max(delay, min(float(after), MAX_WAIT))
            self.retries += 1
            if self.requests >= self.budget:
                raise BudgetExhausted(f"stopped at max_requests_per_run ({self.budget})")
            self.sleep(min(delay, MAX_WAIT))
            delay *= 2

    def _rules(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        if origin not in self.robots:
            self.robots[origin] = "none"          # while fetching robots.txt itself
            try:
                r = self._raw_get(origin + "/robots.txt", "text/plain")
            except BudgetExhausted:
                del self.robots[origin]
                raise
            except OSError:
                r = None
            if r is None or r.status >= 500 or r.status == 429:
                self.robots[origin] = "none"      # can't tell: don't touch the site this run
            elif r.status in (401, 403):
                self.robots[origin] = "none"      # robots.txt itself is locked: treat as "keep out"
            elif r.status >= 400:
                self.robots[origin] = "all"       # no robots.txt: everything allowed
            else:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text().splitlines())
                self.robots[origin] = rp
        return self.robots[origin]

    def _crawl_delay(self, url):
        parts = urllib.parse.urlsplit(url)
        rules = self.robots.get(f"{parts.scheme}://{parts.netloc.lower()}")
        if isinstance(rules, urllib.robotparser.RobotFileParser):
            return float(rules.crawl_delay(AGENT) or 0)
        return 0.0

    def allowed(self, url):
        rules = self._rules(url)
        if rules == "all":
            return True
        if rules == "none":
            return False
        return rules.can_fetch(AGENT, url)

    def get(self, url, accept="application/json"):
        if not self.allowed(url):
            raise Blocked("robots.txt doesn't allow it (or couldn't be read)")
        return self._raw_get(url, accept)


# ---------------------------------------------------------------- HTML to text, and requirements

class _Lines(HTMLParser):
    """HTML -> [(kind, text)], kind "h" (heading, or a short all-bold line), "li" or "p"."""
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "section", "article", "header", "footer", "table",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.parts, self.kind, self.skip, self.bold, self.bold_chars = [], [], "p", 0, 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.flush()
            if tag == "li":
                self.kind = "li"
            elif tag[0] == "h" and tag[1:].isdigit():
                self.kind = "h"
        elif tag in ("b", "strong"):
            self.bold += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.flush()
        elif tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self.bold:
                self.bold_chars += len(data.strip())

    def flush(self):
        text = " ".join("".join(self.parts).split())
        if text:
            kind = self.kind
            if kind == "p" and len(text) <= 80 and self.bold_chars >= len(text.replace(" ", "")) * 0.9:
                kind = "h"
            self.lines.append((kind, text))
        self.parts, self.kind, self.bold_chars = [], "p", 0

    def close(self):
        super().close()
        self.flush()
        return self.lines


def html_lines(markup):
    p = _Lines()
    p.feed(markup or "")
    return p.close()


def lines_to_text(lines):
    out = []
    for kind, text in lines:
        if kind == "h" and out:
            out.append("")
        out.append(("• " if kind == "li" else "") + text)
    return "\n".join(out)


REQ_HEAD = re.compile(r"require|qualifications?|what you(?:'|’)?ll need|what you need|looking for|who you are|"
                      r"about you|you (?:have|bring|should|will have)|must.have|nice.to.have|preferred|"
                      r"skills|we value|ideal candidate|experience", re.I)


def requirements_from(lines, limit=30):
    """Bullet points under requirement-like headings ("Minimum requirements", "What We Require"...)."""
    out, inside = [], False
    for kind, text in lines:
        if kind == "h" or (kind == "p" and len(text) <= 80 and text.endswith(":")):
            inside = bool(REQ_HEAD.search(text))
        elif kind == "li" and inside and text not in out:
            out.append(text)
    return out[:limit]


# ---------------------------------------------------------------- filtering

INTERN_RE = re.compile(r"\b(?:interns?|internships?|co-?ops?|co op|industrial placement|placement year|"
                       r"year in industry|sandwich placement|summer analyst|working student)\b", re.I)
ROLE_STOP = {"intern", "interns", "internship", "internships", "co", "op", "coop", "co-op", "and", "of", "the",
             "for", "in", "a", "an", "&", "summer", "placement"}
ALIASES = {"swe": "software engineer", "sde": "software development engineer", "ml": "machine learning",
           "ai": "artificial intelligence"}


def _tokens(text):
    words = re.findall(r"[a-z0-9+#]+", (text or "").lower())
    out = []
    for w in words:
        out += ALIASES.get(w, w).split()
    return out


def _stem(w):
    return w[:5]                      # engineer/engineering, scientist/science, analyst/analytics


def is_internship(listing, extra=""):
    return bool(INTERN_RE.search(listing.title or "") or INTERN_RE.search(extra or ""))


def matches_role(title, target_roles):
    """Every key word of some target role is in the title (loosely: first five letters)."""
    if not target_roles:
        return True
    have = {_stem(t) for t in _tokens(title)}
    for role in target_roles:
        need = {_stem(t) for t in _tokens(role) if t not in ROLE_STOP}
        if need and need <= have:
            return True
    return False


def workplace_from_text(location):
    low = (location or "").lower()
    if "remote" in low or "anywhere" in low:
        return "remote"
    if "hybrid" in low:
        return "hybrid"
    return "unknown"


def matches_location(listing, locations, preference):
    """remote_only: remote listings. remote_or_hybrid: remote anywhere, or hybrid / unknown in one of
    your locations. any: remote, or anything in one of your locations. No locations set: anywhere."""
    places = [p.lower() for p in locations if p.lower() not in ("remote", "anywhere")]
    here = not places or any(p in (listing.location or "").lower() for p in places)
    if listing.remote == "remote":
        return True
    if preference == "remote_only":
        return False
    if preference == "remote_or_hybrid" and listing.remote == "onsite":
        return False
    return here


def keep(listing, config, extra=""):
    """None if the listing should be kept, else the reason it was filtered out."""
    if not is_internship(listing, extra):
        return "not an internship or co-op"
    if not matches_role(listing.title, config["target_roles"]):
        return "not one of your target roles"
    if not matches_location(listing, config["locations"], config["remote_preference"]):
        return "location / remote preference"
    return None


# ---------------------------------------------------------------- sources

def _date(value):
    """ISO date (YYYY-MM-DD) from an ISO timestamp, a date, or epoch milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc).date().isoformat()
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(value))
    return m.group(0) if m else None


def domain_matches(host, domains):
    host = host.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


class Source:
    """A place listings come from. fetch() returns (listings, extra) where extra maps a listing id to
    any text the filter should also look at (e.g. Lever's "Internship" commitment)."""
    name = "source"
    filtered = True                       # board results are filtered; your own pasted links are not

    def fetch(self, web):
        raise NotImplementedError


def greenhouse_job(job, board):
    """board: the board name in the URL (e.g. "stripe"), which also gives the hosted application form."""
    content = html.unescape(job.get("content") or "")
    lines = html_lines(content)
    location = ((job.get("location") or {}).get("name") or "").strip() or None
    apply_url = f"https://job-boards.greenhouse.io/{urllib.parse.quote(board, safe='')}/jobs/{job['id']}" \
        if job.get("id") else None
    return make_listing(company=job.get("company_name") or _company_name(board), title=(job.get("title") or "").strip(),
                        location=location, remote=workplace_from_text(location), url=job.get("absolute_url") or "",
                        source="greenhouse", posted_date=_date(job.get("first_published") or job.get("updated_at")),
                        deadline=_date(job.get("application_deadline")), description=lines_to_text(lines),
                        requirements=requirements_from(lines), application_type="Greenhouse", apply_url=apply_url)


def lever_posting(p, company):
    cats = p.get("categories") or {}
    parts = [p.get("descriptionBody") or p.get("description") or ""]
    for lst in p.get("lists") or []:
        parts.append(f"<h3>{html.escape(lst.get('text') or '')}</h3><ul>{lst.get('content') or ''}</ul>")
    parts.append(p.get("additional") or "")
    lines = html_lines("\n".join(parts))
    location = cats.get("location") or ", ".join(cats.get("allLocations") or []) or None
    workplace = (p.get("workplaceType") or "").lower()
    remote = workplace if workplace in ("remote", "hybrid", "onsite") else workplace_from_text(location)
    return make_listing(company=company, title=(p.get("text") or "").strip(), location=location, remote=remote,
                        url=p.get("hostedUrl") or "", source="lever", posted_date=_date(p.get("createdAt")),
                        deadline=None, description=lines_to_text(lines), requirements=requirements_from(lines),
                        application_type="Lever", apply_url=p.get("applyUrl") or None)


def _company_name(name):
    return name.replace("-", " ").title() if name.islower() else name


class GreenhouseSource(Source):
    name = "greenhouse"

    def __init__(self, board):
        self.board = board

    def fetch(self, web):
        board = urllib.parse.quote(self.board, safe="")
        r = web.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
        if r.status == 404:
            raise LookupError(f"no Greenhouse board called {self.board!r}")
        if r.status != 200:
            raise OSError(f"Greenhouse answered HTTP {r.status} for {self.board!r}")
        return [greenhouse_job(j, self.board) for j in r.json().get("jobs") or []], {}


class LeverSource(Source):
    name = "lever"

    def __init__(self, company):
        self.company = company

    def fetch(self, web):
        company = urllib.parse.quote(self.company, safe="")
        r = web.get(f"https://api.lever.co/v0/postings/{company}?mode=json")
        if r.status == 404:
            raise LookupError(f"no Lever board called {self.company!r}")
        if r.status != 200:
            raise OSError(f"Lever answered HTTP {r.status} for {self.company!r}")
        out, extra = [], {}
        for p in r.json() or []:
            item = lever_posting(p, _company_name(self.company))
            out.append(item)
            extra[item.id] = (p.get("categories") or {}).get("commitment") or ""
        return out, extra


APPLICATION_TYPES = [("myworkdayjobs.com", "Workday"), ("greenhouse.io", "Greenhouse"), ("lever.co", "Lever"),
                     ("ashbyhq.com", "Ashby"), ("smartrecruiters.com", "SmartRecruiters"),
                     ("icims.com", "iCIMS"), ("taleo.net", "Taleo"), ("successfactors.com", "SuccessFactors"),
                     ("workable.com", "Workable"), ("bamboohr.com", "BambooHR"), ("oraclecloud.com", "Oracle")]
LOGIN_RE = re.compile(r"/(?:log-?in|sign-?in|signin|auth|sso|account/login|users/sign_in)\b", re.I)
GH_URL = re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)/jobs/(\d+)", re.I)
LEVER_URL = re.compile(r"^https?://jobs\.(?:eu\.)?lever\.co/([^/?#]+)/([0-9a-f-]{36})", re.I)


def application_type(url):
    if url.lower().startswith("mailto:"):
        return "email"
    host = urllib.parse.urlsplit(url).netloc.lower()
    return next((name for d, name in APPLICATION_TYPES if domain_matches(host, [d])), "company site")


def company_from_host(host):
    labels = [x for x in host.lower().split(":")[0].split(".") if x not in ("www", "careers", "jobs", "apply", "boards")]
    if len(labels) >= 3 and labels[-2] == "myworkdayjobs":           # acme.wd5.myworkdayjobs.com
        return _company_name(labels[0])
    return _company_name(labels[-2] if len(labels) >= 2 else (labels[0] if labels else host))


class _JsonLd(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.inside, self.buf, self.title, self.in_title, self.meta = [], False, [], "", False, {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.inside, self.buf = True, []
        elif tag == "title":
            self.in_title = True
        elif tag == "meta" and a.get("property", a.get("name")):
            self.meta[(a.get("property") or a.get("name")).lower()] = a.get("content") or ""

    def handle_endtag(self, tag):
        if tag == "script" and self.inside:
            self.inside = False
            self.blocks.append("".join(self.buf))
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.inside:
            self.buf.append(data)
        elif self.in_title:
            self.title += data


def _job_postings(node):
    if isinstance(node, list):
        for x in node:
            yield from _job_postings(x)
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield node
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in node:
                yield from _job_postings(node[k])


def page_listing(url, page_html, final_url):
    """A listing from a job page: its schema.org JobPosting if it has one, else just the title."""
    p = _JsonLd()
    p.feed(page_html)
    host = urllib.parse.urlsplit(final_url or url).netloc
    for block in p.blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for job in _job_postings(data):
            org = job.get("hiringOrganization")
            company = (org.get("name") if isinstance(org, dict) else org) or company_from_host(host)
            locs = job.get("jobLocation") or []
            places = []
            for loc in locs if isinstance(locs, list) else [locs]:
                addr = (loc or {}).get("address") or {}
                if isinstance(addr, dict):
                    country = addr.get("addressCountry")
                    country = country.get("name") if isinstance(country, dict) else country
                    places.append(", ".join(str(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"),
                                                               country) if x))
            location = "; ".join(x for x in places if x) or None
            remote = "remote" if str(job.get("jobLocationType", "")).upper() == "TELECOMMUTE" \
                else workplace_from_text(location)
            lines = html_lines(html.unescape(str(job.get("description") or "")))
            return make_listing(company=str(company), title=str(job.get("title") or p.title).strip(),
                                location=location, remote=remote, url=url, source="manual",
                                posted_date=_date(job.get("datePosted")), deadline=_date(job.get("validThrough")),
                                description=lines_to_text(lines), requirements=requirements_from(lines),
                                application_type=application_type(final_url or url))
    title = (p.meta.get("og:title") or p.title or "").strip()
    company = p.meta.get("og:site_name") or company_from_host(host)
    desc = p.meta.get("og:description") or p.meta.get("description") or ""
    return make_listing(company=company, title=" ".join(title.split()), location=None, remote="unknown", url=url,
                        source="manual", posted_date=None, deadline=None, description=desc, requirements=[],
                        application_type=application_type(final_url or url),
                        note="no structured job data on the page: only the title was read")


def _as_manual(item, url):
    """A listing read through a board API for a link you pasted: keeps your link (and an id to match)."""
    d = dataclasses.asdict(item)
    d.pop("id")
    return make_listing(**{**d, "source": "manual", "url": url})


def read_manual_urls(path):
    """[(url, note)] from the file; blank lines and # comments skipped. Bad lines -> (None, reason)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            scheme = urllib.parse.urlsplit(line).scheme.lower()
            if scheme in ("http", "https", "mailto"):
                out.append((line, None))
            else:
                out.append((None, f"line {n}: {line[:80]!r} isn't an http(s) or mailto: link"))
    return out


class ManualSource(Source):
    name = "manual"
    filtered = False

    def __init__(self, path, manual_only_domains):
        self.path, self.manual_only = path, manual_only_domains
        self.problems, self.duplicates = [], 0

    def saved(self, url, note, manual_only=False, title=""):
        host = urllib.parse.urlsplit(url).netloc
        company = company_from_host(host) if host else url.split("@")[-1].split("?")[0]
        return make_listing(company=company, title=title, location=None, remote="unknown", url=url, source="manual",
                            posted_date=None, deadline=None, description="", requirements=[],
                            application_type=application_type(url), manual_only=manual_only, note=note)

    def fetch(self, web):
        out, self.problems, self.duplicates, seen = [], [], 0, set()
        for url, problem in read_manual_urls(self.path):
            if problem:
                self.problems.append(problem)
                continue
            if canonical_url(url) in seen:                # pasted twice: fetched once
                self.duplicates += 1
                continue
            seen.add(canonical_url(url))
            out.append(self.one(web, url))
        return out, {}

    def one(self, web, url):
        if url.lower().startswith("mailto:"):
            return self.saved(url, "apply by email", manual_only=True)
        host = urllib.parse.urlsplit(url).netloc
        if domain_matches(host, self.manual_only):
            return self.saved(url, "manual-only site (config.yaml): not read, apply by hand", manual_only=True)
        try:
            if m := GH_URL.match(url):
                r = web.get(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}?content=true")
                if r.status == 200:
                    return _as_manual(greenhouse_job(r.json(), m[1]), url)
            elif m := LEVER_URL.match(url):
                r = web.get(f"https://api.lever.co/v0/postings/{m[1]}/{m[2]}?mode=json")
                if r.status == 200:
                    return _as_manual(lever_posting(r.json(), _company_name(m[1])), url)
            else:
                r = web.get(url, accept="text/html,application/xhtml+xml")
                if r.status in (401, 403) or LOGIN_RE.search(urllib.parse.urlsplit(r.url).path):
                    return self.saved(url, "needs a login: not read, apply by hand", manual_only=True)
                if r.status == 200 and "html" in (r.content_type or "html").lower():
                    item = page_listing(url, r.text(), r.url)
                    return item if item.title else self.saved(url, "page had no title")
            return self.saved(url, f"couldn't read it (HTTP {r.status})")
        except Blocked as e:
            return self.saved(url, f"not read: {e}", manual_only=True)
        except (OSError, ValueError) as e:
            return self.saved(url, f"couldn't read it: {e}")


def sources_from(config, manual_path=MANUAL_URLS_PATH):
    """(sources, skipped boards). max_companies_per_run counts Greenhouse and Lever boards together."""
    boards = [GreenhouseSource(b) for b in config["greenhouse_boards"]] + [LeverSource(c) for c in config["lever_companies"]]
    limit = int(config["max_companies_per_run"])
    skipped = [f"{s.name}:{getattr(s, 'board', None) or s.company}" for s in boards[limit:]]
    return boards[:limit] + [ManualSource(manual_path, config["manual_only_domains"])], skipped


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, remote TEXT NOT NULL, url TEXT NOT NULL,
    source TEXT NOT NULL, posted_date TEXT, deadline TEXT, description TEXT NOT NULL,
    requirements TEXT NOT NULL,                    -- JSON list
    application_type TEXT NOT NULL, manual_only INTEGER NOT NULL DEFAULT 0, note TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, apply_url TEXT
);
"""
FIELDS = [f.name for f in dataclasses.fields(Listing)]


class Store:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(listings)")}
        if "apply_url" not in have:                       # a database from before apply_url existed
            with self.db:
                self.db.execute("ALTER TABLE listings ADD COLUMN apply_url TEXT")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def save(self, listings):
        """Insert new listings, refresh ones already stored. Returns (new, updated)."""
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        new = updated = 0
        with self.db:
            for x in listings:
                key = dedup_key(x.company, x.title, x.url)
                row = dataclasses.asdict(x)
                row["requirements"] = json.dumps(x.requirements)
                row["manual_only"] = int(x.manual_only)
                exists = self.db.execute("SELECT 1 FROM listings WHERE dedup_key = ?", (key,)).fetchone()
                cols = ", ".join(FIELDS)
                self.db.execute(
                    f"INSERT INTO listings ({cols}, dedup_key, first_seen, last_seen) "
                    f"VALUES ({', '.join('?' * len(FIELDS))}, ?, ?, ?) "
                    f"ON CONFLICT(dedup_key) DO UPDATE SET "
                    + ", ".join(f"{f} = excluded.{f}" for f in FIELDS if f != "id") + ", last_seen = excluded.last_seen",
                    [row[f] for f in FIELDS] + [key, now, now])
                if exists:
                    updated += 1
                else:
                    new += 1
        return new, updated

    def all(self):
        rows = self.db.execute(f"SELECT {', '.join(FIELDS)} FROM listings ORDER BY first_seen, company, title").fetchall()
        out = []
        for r in rows:
            d = dict(zip(FIELDS, r))
            d["requirements"] = json.loads(d["requirements"])
            d["manual_only"] = bool(d["manual_only"])
            out.append(Listing(**d))
        return out


# ---------------------------------------------------------------- one run

@dataclass
class Report:
    fetched: dict = field(default_factory=dict)        # source label -> listings returned
    kept: int = 0
    filtered: dict = field(default_factory=dict)       # reason -> count
    duplicates: int = 0
    new: int = 0
    updated: int = 0
    requests: int = 0
    errors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def summary(self):
        lines = [f"{self.kept} listings kept ({self.new} new, {self.updated} already known), "
                 f"{self.duplicates} duplicates merged, {self.requests} web requests"]
        lines += [f"  {k}: {v} fetched" for k, v in self.fetched.items()]
        lines += [f"  filtered out ({k}): {v}" for k, v in self.filtered.items()]
        lines += [f"  skipped: {s}" for s in self.skipped]
        lines += [f"  problem: {e}" for e in self.errors]
        return "\n".join(lines)


def collect(config, db_path=DB_PATH, manual_path=MANUAL_URLS_PATH, web=None):
    """Fetch every source, filter, deduplicate, store. Never raises for one bad source."""
    web = web or Web(config)
    sources, skipped = sources_from(config, manual_path)
    report = Report(skipped=[f"{s} (over max_companies_per_run)" for s in skipped])
    kept = {}
    for src in sources:
        label = src.name + (f":{getattr(src, 'board', '') or getattr(src, 'company', '')}" if src.filtered else "")
        try:
            items, extra = src.fetch(web)
        except BudgetExhausted as e:
            report.errors.append(f"{label}: {e}")
            break
        except Blocked as e:
            report.skipped.append(f"{label}: {e}")
            continue
        except (OSError, ValueError, LookupError) as e:
            report.errors.append(f"{label}: {e}")
            continue
        report.fetched[label] = len(items)
        report.errors += getattr(src, "problems", [])
        report.duplicates += getattr(src, "duplicates", 0)
        for item in items:
            reason = keep(item, config, extra.get(item.id, "")) if src.filtered else None
            if reason:
                report.filtered[reason] = report.filtered.get(reason, 0) + 1
            elif item.id in kept:
                report.duplicates += 1
            else:
                kept[item.id] = item
    report.requests = web.requests
    log.info("run: %d fetched sources, %d kept, %d requests (%d retries), %d errors",
             len(report.fetched), len(kept), web.requests, web.retries, len(report.errors))
    for e in report.errors:
        log.warning("problem: %s", e)
    report.kept = len(kept)
    store = Store(db_path)
    try:
        report.new, report.updated = store.save(list(kept.values()))
    finally:
        store.close()
    return report


if __name__ == "__main__":
    print(collect(load_config()).summary())

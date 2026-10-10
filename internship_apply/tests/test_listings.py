"""Finding listings, against saved fixtures only. Run from the project root:
    python -m unittest discover -s internship_apply/tests -t .
No test touches the network: every request goes to a fake transport, and urllib's urlopen is
replaced with one that fails the test if anything tries to go live."""
import contextlib
import os
import sqlite3
import tempfile
import unittest
import urllib.request
from unittest import mock

from internship_apply import DEFAULTS, listings
from internship_apply.listings import Listing, Response, Web

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


GH_LIST = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
GH_ONE = "https://boards-api.greenhouse.io/v1/boards/globex/jobs/555?content=true"
LEVER = "https://api.lever.co/v0/postings/initech?mode=json"
WORKDAY = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123"
HOOLI = "https://careers.hooli.example/jobs/42?utm_source=newsletter"
PLAIN = "https://vandelay.example/jobs/junior"
SECRET = "https://secret.example/jobs/9"
PRIVATE = "https://private.example/jobs/1"

ROUTES = {
    "https://boards-api.greenhouse.io/robots.txt": (200, b"User-agent: *\nDisallow: /embed/\n"),
    "https://api.lever.co/robots.txt": (200, b"User-agent: *\nAllow: /\nCrawl-delay: 1\n"),
    "https://careers.hooli.example/robots.txt": (404, b""),
    "https://vandelay.example/robots.txt": (200, b"User-agent: JarvisInternshipFinder\nCrawl-delay: 5\n"),
    "https://secret.example/robots.txt": (200, b"User-agent: *\nAllow: /\n"),
    "https://private.example/robots.txt": (200, b"User-agent: *\nDisallow: /jobs\n"),
    GH_LIST: (200, fixture("greenhouse_jobs.json")),
    GH_ONE: (200, fixture("greenhouse_job.json")),
    LEVER: (200, fixture("lever_postings.json")),
    HOOLI: (200, fixture("company_job.html"), "text/html; charset=utf-8"),
    PLAIN: (200, fixture("plain_job.html"), "text/html"),
    SECRET: (200, b"<html><title>Sign in</title></html>", "text/html", "https://secret.example/login?next=/jobs/9"),
}

MANUAL = f"""# my links
https://job-boards.greenhouse.io/globex/jobs/555
{WORKDAY}

{HOOLI}
{PLAIN}
{SECRET}   # needs an account
{PRIVATE}
mailto:jobs@initech.example
not a link
{PLAIN}  # pasted twice
"""


class FakeNet:
    """The transport tests use: fixtures by URL, a clock that sleeping moves forward, and a log."""
    def __init__(self, routes=ROUTES):
        self.routes, self.calls, self.now, self.slept = dict(routes), [], 1000.0, []

    def transport(self, url, headers):
        self.calls.append((url, headers, self.now))
        if url not in self.routes:
            raise AssertionError(f"unexpected request: {url}")
        status, body, *rest = self.routes[url]
        ctype = rest[0] if rest else "application/json"
        final = rest[1] if len(rest) > 1 else url
        return Response(status, final, body, ctype)

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.now += s

    def web(self, config):
        return Web(config, transport=self.transport, clock=self.clock, sleep=self.sleep)

    def urls(self):
        return [u for u, _, _ in self.calls]


def config(**kw):
    c = {**DEFAULTS, "target_roles": ["Software Engineering Intern", "Data Science Intern"],
         "locations": ["London", "Remote"], "remote_preference": "remote_or_hybrid",
         "greenhouse_boards": ["acme"], "lever_companies": ["initech"], "manual_only_domains": ["myworkdayjobs.com"]}
    c.update(kw)
    return c


def listing(title="Software Engineering Intern", location="London, UK", remote="unknown"):
    return listings.make_listing(company="X", title=title, location=location, remote=remote, url="https://x.example/1",
                                 source="greenhouse", posted_date=None, deadline=None, description="",
                                 requirements=[], application_type="Greenhouse")


class NoNetwork(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("live network call in a test"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "listings.db")
        self.manual = os.path.join(self.tmp.name, "manual_urls.txt")
        with open(self.manual, "w", encoding="utf-8") as f:
            f.write(MANUAL)

    def stored(self):
        with listings.Store(self.db) as store:
            return store.all()

    def run_collect(self, cfg=None, net=None):
        net = net or FakeNet()
        cfg = cfg or config()
        return listings.collect(cfg, db_path=self.db, manual_path=self.manual, web=net.web(cfg)), net


class ParsingTests(NoNetwork):
    def test_greenhouse_job(self):
        import json
        job = json.loads(fixture("greenhouse_jobs.json"))["jobs"][0]
        x = listings.greenhouse_job(job, "Acme")
        self.assertEqual((x.company, x.title, x.location), ("Acme Robotics", "Software Engineering Intern", "London, UK"))
        self.assertEqual((x.posted_date, x.deadline), ("2026-09-20", "2026-11-30"))
        self.assertEqual((x.source, x.application_type, x.remote), ("greenhouse", "Greenhouse", "unknown"))
        self.assertEqual(x.requirements, ["Pursuing a degree in Computer Science", "Experience with Python or C++",
                                          "Robotics coursework"])       # not "Ship features" (that's duties)
        self.assertIn("What you'll do", x.description)                  # HTML unescaped, tags gone
        self.assertNotIn("<", x.description)

    def test_lever_posting(self):
        import json
        p = json.loads(fixture("lever_postings.json"))[0]
        x = listings.lever_posting(p, "Initech")
        self.assertEqual((x.title, x.remote, x.location), ("Software Engineer", "hybrid", "London, United Kingdom"))
        self.assertEqual(x.requirements, ["Java or Kotlin", "Curiosity"])
        self.assertEqual(x.posted_date, "2026-08-29")                   # createdAt is epoch milliseconds
        self.assertEqual(x.url, "https://jobs.lever.co/initech/11111111-1111-4111-8111-111111111111")

    def test_json_ld_page(self):
        x = listings.page_listing(HOOLI, fixture("company_job.html").decode(), HOOLI)
        self.assertEqual((x.company, x.title), ("Hooli", "Software Engineering Intern (Summer)"))
        self.assertEqual((x.remote, x.location, x.deadline), ("remote", "Leeds, UK", "2026-12-01"))
        self.assertEqual(x.requirements, ["Python", "Git"])

    def test_page_without_structured_data(self):
        x = listings.page_listing(PLAIN, fixture("plain_job.html").decode(), PLAIN)
        self.assertEqual((x.company, x.title), ("Vandelay Industries", "Junior Developer Internship - Vandelay Industries"))
        self.assertIn("only the title", x.note)
        self.assertNotIn("not text", x.description)

    def test_application_type(self):
        self.assertEqual(listings.application_type(WORKDAY), "Workday")
        self.assertEqual(listings.application_type("https://jobs.ashbyhq.com/x/1"), "Ashby")
        self.assertEqual(listings.application_type("https://hooli.example/jobs"), "company site")
        self.assertEqual(listings.application_type("mailto:a@b.example"), "email")


class FilterTests(unittest.TestCase):
    def test_internship_words(self):
        for t in ("Software Engineering Intern", "Data Internship 2027", "Engineering Co-op", "Software Engineer (Coop)",
                  "Industrial Placement - Software", "Summer Analyst"):
            self.assertTrue(listings.is_internship(listing(t)), t)
        for t in ("Internal Tools Engineer", "Senior Software Engineer", "International Sales"):
            self.assertFalse(listings.is_internship(listing(t)), t)
        self.assertTrue(listings.is_internship(listing("Software Engineer"), extra="Internship"))   # Lever commitment

    def test_roles(self):
        roles = ["Software Engineering Intern", "Data Science Intern"]
        for t in ("Software Engineer Intern", "Backend Software Engineering Internship", "SWE Intern",
                  "Data Scientist, Internship"):
            self.assertTrue(listings.matches_role(t, roles), t)
        for t in ("Marketing Intern", "Hardware Engineering Intern", "Data Analyst Intern"):
            self.assertFalse(listings.matches_role(t, roles), t)
        self.assertTrue(listings.matches_role("Anything Intern", []))         # no roles set: all internships

    def test_locations(self):
        london, tokyo = listing(location="London, UK"), listing(location="Tokyo, Japan")
        remote = listing(location="US-Remote", remote="remote")
        hybrid = listing(location="London", remote="hybrid")
        onsite = listing(location="London", remote="onsite")
        places = ["London", "Remote"]
        self.assertEqual([listings.matches_location(x, places, "remote_only") for x in (london, remote, hybrid)],
                         [False, True, False])
        self.assertEqual([listings.matches_location(x, places, "remote_or_hybrid")
                          for x in (london, tokyo, remote, hybrid, onsite)], [True, False, True, True, False])
        self.assertEqual([listings.matches_location(x, places, "any") for x in (london, tokyo, onsite)],
                         [True, False, True])
        self.assertTrue(listings.matches_location(tokyo, [], "any"))          # no locations set: anywhere
        self.assertEqual(listings.workplace_from_text("Chicago, US-Remote"), "remote")
        self.assertEqual(listings.workplace_from_text("Manchester, UK - Hybrid"), "hybrid")

    def test_dedup_key_ignores_tracking_and_case(self):
        a = listings.dedup_key("Acme", "SWE  Intern", "https://Jobs.Acme.example/1/?utm_source=x#apply")
        b = listings.dedup_key("acme", "swe intern", "https://jobs.acme.example/1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, listings.dedup_key("acme", "swe intern", "https://jobs.acme.example/2"))
        self.assertNotEqual(listings.dedup_key("a", "t", "https://x.example/?gh_jid=1"),
                            listings.dedup_key("a", "t", "https://x.example/?gh_jid=2"))   # real ids kept


class CollectTests(NoNetwork):
    def test_full_run(self):
        report, net = self.run_collect()
        stored = {(x.source, x.company, x.title): x for x in self.stored()}
        titles = sorted(t for _, _, t in stored)
        self.assertEqual(report.fetched, {"greenhouse:acme": 8, "lever:initech": 4, "manual": 7})
        self.assertEqual(report.filtered, {"not an internship or co-op": 3, "location / remote preference": 3,
                                           "not one of your target roles": 1})
        self.assertEqual(report.duplicates, 2)          # Greenhouse listed one job twice; one link pasted twice
        self.assertEqual((report.kept, report.new, report.updated), (11, 11, 0))
        self.assertIn("Software Engineering Intern", titles)
        self.assertIn("Data Scientist, Internship", titles)
        self.assertNotIn("Marketing Intern", titles)
        self.assertNotIn("Senior Software Engineer", titles)
        self.assertEqual(report.errors, ["line 10: 'not a link' isn't an http(s) or mailto: link"])

        # Manual links: each handled the safe way.
        manual = {x.url: x for x in self.stored() if x.source == "manual"}
        globex = manual["https://job-boards.greenhouse.io/globex/jobs/555"]
        self.assertEqual((globex.company, globex.title, globex.manual_only), ("Globex", "Data Science Intern", False))
        self.assertTrue(manual[WORKDAY].manual_only)
        self.assertEqual((manual[WORKDAY].application_type, manual[WORKDAY].company), ("Workday", "Acme"))
        self.assertIn("manual-only", manual[WORKDAY].note)
        self.assertEqual(manual[HOOLI].title, "Software Engineering Intern (Summer)")
        self.assertIn("login", manual[SECRET].note)
        self.assertTrue(manual[SECRET].manual_only)
        self.assertIn("robots.txt", manual[PRIVATE].note)
        self.assertEqual(manual["mailto:jobs@initech.example"].application_type, "email")

        # What was (and wasn't) requested.
        urls = net.urls()
        self.assertFalse(any("myworkdayjobs" in u for u in urls))           # never touched, not even robots.txt
        self.assertNotIn(PRIVATE, urls)                                     # robots.txt said no
        self.assertEqual(urls.count(PLAIN), 1)                              # the pasted-twice link: fetched once
        self.assertEqual(report.requests, len(urls))

    def test_rerun_updates_instead_of_duplicating(self):
        self.run_collect()
        first = {x.id for x in self.stored()}
        report, _ = self.run_collect()
        self.assertEqual((report.new, report.updated), (0, 11))
        self.assertEqual({x.id for x in self.stored()}, first)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            rows = db.execute("SELECT COUNT(*), COUNT(DISTINCT dedup_key) FROM listings").fetchone()
        self.assertEqual(rows, (11, 11))

    def test_user_agent_and_politeness(self):
        cfg = config(user_agent_contact="me@example.com")
        _, net = self.run_collect(cfg)
        agents = {h["User-Agent"] for _, h, _ in net.calls}
        self.assertEqual(len(agents), 1)
        ua = agents.pop()
        self.assertTrue(ua.startswith("JarvisInternshipFinder/0.1 ("), ua)
        self.assertIn("me@example.com", ua)
        # Per host: at least min_seconds_between_requests (2), or the site's Crawl-delay if longer.
        by_host = {}
        for url, _, t in net.calls:
            by_host.setdefault(url.split("/")[2], []).append(t)
        for host, times in by_host.items():
            gap = 5 if host == "vandelay.example" else 2
            for a, b in zip(times, times[1:]):
                self.assertGreaterEqual(round(b - a, 6), gap, host)
        self.assertGreaterEqual(len(by_host["vandelay.example"]), 2)

    def test_robots_disallow_and_unreadable(self):
        routes = dict(ROUTES)
        routes["https://api.lever.co/robots.txt"] = (200, b"User-agent: *\nDisallow: /v0/\n")
        routes["https://boards-api.greenhouse.io/robots.txt"] = (503, b"")
        net = FakeNet(routes)
        report, _ = self.run_collect(net=net)
        self.assertNotIn(LEVER, net.urls())
        self.assertNotIn(GH_LIST, net.urls())
        self.assertTrue(any(s.startswith("lever:initech") and "robots.txt" in s for s in report.skipped), report.skipped)
        self.assertTrue(any(s.startswith("greenhouse:acme") for s in report.skipped), report.skipped)

    def test_request_budget_stops_the_run(self):
        cfg = config(max_requests_per_run=3)
        report, net = self.run_collect(cfg)
        self.assertEqual(len(net.calls), 3)                 # GH robots + GH list + Lever robots, then stop
        self.assertTrue(any("max_requests_per_run" in e for e in report.errors), report.errors)
        self.assertEqual(report.kept, 2)                    # what was fetched is still saved

    def test_max_companies_per_run(self):
        cfg = config(max_companies_per_run=1)
        report, net = self.run_collect(cfg)
        self.assertNotIn(LEVER, net.urls())
        self.assertTrue(any("lever:initech" in s and "max_companies_per_run" in s for s in report.skipped))

    def test_missing_board(self):
        routes = dict(ROUTES)
        routes[GH_LIST] = (404, b'{"status":404}')
        report, _ = self.run_collect(net=FakeNet(routes))
        self.assertTrue(any("no Greenhouse board called 'acme'" in e for e in report.errors), report.errors)
        self.assertEqual(report.fetched["lever:initech"], 4)    # one bad board doesn't stop the rest


if __name__ == "__main__":
    unittest.main()

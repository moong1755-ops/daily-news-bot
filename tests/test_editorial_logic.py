import sys
import types
import unittest
from datetime import datetime, timezone


def _install_optional_dependency_stubs():
    """Allow policy tests to run without loading network or ML dependencies."""
    try:
        import requests  # noqa: F401
    except ImportError:
        requests = types.ModuleType("requests")
        requests.HTTPError = RuntimeError
        sys.modules["requests"] = requests

    try:
        import feedparser  # noqa: F401
    except ImportError:
        feedparser = types.ModuleType("feedparser")
        feedparser.USER_AGENT = ""
        sys.modules["feedparser"] = feedparser

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        sentence_transformers = types.ModuleType("sentence_transformers")
        sentence_transformers.SentenceTransformer = object
        sys.modules["sentence_transformers"] = sentence_transformers

    try:
        from sklearn.metrics.pairwise import cosine_similarity  # noqa: F401
    except ImportError:
        sklearn = types.ModuleType("sklearn")
        metrics = types.ModuleType("sklearn.metrics")
        pairwise = types.ModuleType("sklearn.metrics.pairwise")
        pairwise.cosine_similarity = lambda values: values
        sys.modules["sklearn"] = sklearn
        sys.modules["sklearn.metrics"] = metrics
        sys.modules["sklearn.metrics.pairwise"] = pairwise


_install_optional_dependency_stubs()

from src.bot import is_relevant
from src.config import CATEGORIES
from src.fetchers.rss_feeds import _article_date, _uses_long_form_window
from src.processor.deduplicator import _events_compatible, _merge_group
from src.processor.reranker import _fallback_rule_based
from src.processor.summarizer import summarize


def _category(prefix):
    return next(category for category in CATEGORIES if category.startswith(prefix))


IMPACT = _category("🌱")
AI = _category("🤖")
ALTERNATIVE = _category("💼")
INSIGHTS = _category("👔")
CATEGORY_ORDER = list(CATEGORIES)


class ArticleQualificationTests(unittest.TestCase):
    def test_job_post_is_excluded(self):
        article = {
            "title": "We're hiring: AI software engineer",
            "description": "Apply now to join our team.",
            "source": "Example",
            "link": "https://example.com/jobs/engineer",
        }

        self.assertFalse(is_relevant(article))
        self.assertTrue(article["filter_reason"].startswith("hard_exclusion:"))

    def test_opinion_is_excluded_even_from_primary_source(self):
        article = {
            "title": "Opinion: Climate tech needs patient capital",
            "description": "A columnist argues for long-term investment.",
            "source": "Impact Alpha",
            "feed": "Impact Alpha",
            "link": "https://impactalpha.com/opinion/patient-capital",
        }

        self.assertFalse(is_relevant(article))
        self.assertTrue(article["filter_reason"].startswith("opinion:"))

    def test_material_press_release_is_rescued(self):
        article = {
            "title": "Press release: Acme closes Series B",
            "description": "The company raised growth capital for expansion.",
            "source": "Example",
            "link": "https://example.com/acme-series-b",
        }

        self.assertTrue(is_relevant(article))
        self.assertIn("series b", article["rescue_signal"])


class CategoryRoutingTests(unittest.TestCase):
    def test_impact_content_overrides_venture_feed(self):
        article = {
            "title": "Climate healthcare startup raises Series A",
            "description": "The technology reduces emissions and improves health access.",
            "source": "TechCrunch Venture",
            "feed": "TechCrunch Venture",
        }

        result, errors = summarize(article)

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], IMPACT)
        self.assertEqual(result["category_reason"], "impact_content")
        self.assertTrue(result["impact_themes"])
        self.assertTrue(result["impact_must_read"])

    def test_official_mbb_source_stays_in_insights(self):
        article = {
            "title": "Climate investment outlook for industrial companies",
            "description": "A global report on decarbonization and AI.",
            "source": "BCG",
            "feed": "BCG Official Insights",
        }

        result, errors = summarize(article)

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], INSIGHTS)
        self.assertEqual(result["category_reason"], "official_insights_source")

    def test_series_b_is_a_major_alternative_deal(self):
        article = {
            "title": "Whisper raises $200 million Series B",
            "description": "The financing values the company at $2 billion.",
            "source": "TechCrunch Venture",
            "feed": "TechCrunch Venture",
        }

        result, errors = summarize(article)

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], ALTERNATIVE)
        self.assertTrue(result["major_deal"])


class DuplicateProtectionTests(unittest.TestCase):
    def test_different_event_types_are_not_merged(self):
        acquisition = {
            "title": "Acme acquires Beta",
            "description": "The acquisition was announced today.",
        }
        funding = {
            "title": "Acme raises Series B",
            "description": "The funding round was announced today.",
        }

        self.assertFalse(_events_compatible(acquisition, funding))

    def test_different_funding_rounds_are_not_merged(self):
        series_a = {"title": "Acme raises Series A", "description": "Funding round"}
        series_b = {"title": "Acme raises Series B", "description": "Funding round"}

        self.assertFalse(_events_compatible(series_a, series_b))

    def test_verified_direct_source_becomes_representative(self):
        relay = {
            "title": "Climate company raises Series B",
            "description": "Short report.",
            "source": "Other Outlet",
            "feed": "Google News",
            "link": "https://other.example/story",
            "gnews_link": "https://news.google.com/story",
            "date": "2026-08-20",
        }
        original = {
            "title": "Climate company raises $200 million Series B",
            "description": "Detailed original reporting on the financing.",
            "source": "Impact Alpha",
            "feed": "Impact Alpha",
            "link": "https://impactalpha.com/story",
            "gnews_link": "",
            "date": "2026-08-19",
        }

        result = _merge_group([relay, original])

        self.assertEqual(result["source"][0], "Impact Alpha")
        self.assertEqual(result["link"][0], "https://impactalpha.com/story")
        self.assertEqual(result["duplicate_count"], 2)
        self.assertIsInstance(relay["source"], str)


class SelectionAndDateTests(unittest.TestCase):
    def test_fallback_keeps_only_tagged_overflow(self):
        buckets = {category: [] for category in CATEGORY_ORDER}
        buckets[IMPACT] = [
            {
                "title": f"Impact event {index}",
                "category": IMPACT,
                "impact_must_read": True,
                "relevance": 10 - index,
            }
            for index in range(4)
        ]
        buckets[ALTERNATIVE] = [
            {
                "title": f"Company {index} raises Series B",
                "category": ALTERNATIVE,
                "major_deal": True,
                "relevance": 10 - index,
            }
            for index in range(5)
        ]
        buckets[AI] = [
            {"title": f"Routine AI news {index}", "category": AI, "relevance": index}
            for index in range(5)
        ]

        selected = _fallback_rule_based(buckets, CATEGORY_ORDER)

        self.assertEqual(sum(a["category"] == IMPACT for a in selected), 4)
        self.assertEqual(sum(a["category"] == ALTERNATIVE for a in selected), 5)
        self.assertEqual(sum(a["category"] == AI for a in selected), 3)

    def test_rss_date_windows_and_korea_date(self):
        now = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
        recent = {"published_parsed": (2026, 8, 18, 20, 0, 0, 0, 0, 0)}
        too_old = {"published_parsed": (2026, 8, 16, 23, 0, 0, 0, 0, 0)}
        weekly = {"published_parsed": (2026, 8, 12, 12, 0, 0, 0, 0, 0)}

        self.assertEqual(
            _article_date(recent, "TechCrunch Venture", now),
            ("2026-08-19", True),
        )
        self.assertFalse(_article_date(too_old, "TechCrunch Venture", now)[1])
        self.assertTrue(_uses_long_form_window("BCG Official Insights"))
        self.assertTrue(_article_date(weekly, "BCG Official Insights", now)[1])


if __name__ == "__main__":
    unittest.main()

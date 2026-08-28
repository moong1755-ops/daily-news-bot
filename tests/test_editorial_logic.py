import sys
import types
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch


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

from src.bot import (
    _build_slack_blocks,
    _decision_record,
    _select_category_articles,
    is_relevant,
    send_aggregated_slack_news,
)
from src.config import CATEGORIES, DIRECT_WEB_SOURCE_METADATA, GOOGLE_NEWS_FEEDS
from src.fetchers.rss_feeds import (
    _ConfiguredArticleListParser,
    _article_date,
    _direct_web_date,
    _uses_long_form_window,
)
from src.processor import deduplicator as deduplicator_module
from src.processor.deduplicator import (
    _events_compatible,
    _merge_group,
    _same_headline_event,
    _should_merge,
    deduplicate_and_merge,
)
from src.processor.reranker import (
    _fallback_rule_based,
    _finalize_llm_selection,
    _macro_geography_scope,
    select_top_news_with_llm,
)
from src.processor.summarizer import summarize


def _category(prefix):
    return next(category for category in CATEGORIES if category.startswith(prefix))


IMPACT = _category("🌱")
AI = _category("🤖")
ALTERNATIVE = _category("📈")
MACRO = _category("🌐")
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
    def test_weekly_calendar_roundups_are_excluded(self):
        titles = (
            "[다음주 경제] 기준금리 결정과 출생통계 발표",
            "[이번주 증시] 주요 기업 실적 발표 일정",
            "주간 정책 일정: 국회와 정부 주요 회의",
            "Week Ahead: Central bank meetings and economic releases",
        )

        for title in titles:
            with self.subTest(title=title):
                result, errors = summarize({
                    "title": title,
                    "description": "여러 예정 일정을 한 기사에 모아 정리한다.",
                    "source": "Example",
                    "feed": "Example",
                    "region": "korea",
                })
                self.assertEqual(errors, [])
                self.assertTrue(result["editorial_excluded"])
                self.assertEqual(
                    result["editorial_exclusion_reason"],
                    "compound_roundup",
                )

    def test_substantive_rate_outlook_is_not_treated_as_calendar(self):
        result, errors = summarize({
            "title": "기준금리 전망 혼조…인상과 숨 고르기 의견 엇갈려",
            "description": "시장 전문가들이 한국은행의 다음 결정을 분석했다.",
            "source": "연합뉴스",
            "feed": "국내 거시/정책 (연합·한경)",
            "region": "korea",
        })

        self.assertEqual(errors, [])
        self.assertFalse(result["editorial_excluded"])
        self.assertEqual(result["category"], MACRO)

    def test_local_administration_and_company_rules_do_not_become_macro(self):
        titles = (
            "불법 운임인상 역대급 제재…통합 후에도 최대 난제로",
            "해외직구 주문 정보 관세청에 바로 전달…통관부호 일회용 인증",
            "금산법 10% 규제, 배당과 자사주 삼성전자 주주환원 변수",
        )

        for title in titles:
            with self.subTest(title=title):
                result, errors = summarize({
                    "title": title,
                    "description": "",
                    "source": "Example",
                    "feed": "Example",
                    "region": "korea",
                })
                self.assertEqual(errors, [])
                self.assertNotEqual(result["category"], MACRO)

    def test_economy_wide_macro_events_stay_macro(self):
        titles = (
            "한국은행 기준금리 동결",
            "한국 소비자물가 상승률 둔화",
            "정부, 내년도 국가예산과 재정정책 발표",
            "EU sanctions Russia over invasion",
            "US imposes tariffs on China",
        )

        for title in titles:
            with self.subTest(title=title):
                result, errors = summarize({
                    "title": title,
                    "description": "",
                    "source": "Example",
                    "feed": "Example",
                    "region": "korea",
                })
                self.assertEqual(errors, [])
                self.assertEqual(result["category"], MACRO)

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

    def test_official_mbb_domain_overrides_a_generic_feed(self):
        result, errors = summarize({
            "title": "2026 AI Jobs Barometer Global report findings",
            "description": "A global labor-market report.",
            "source": "PwC",
            "feed": "TechCrunch AI",
            "link": "https://www.pwc.com/gx/en/issues/artificial-intelligence/job-barometer.html",
        })

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], INSIGHTS)
        self.assertEqual(result["category_reason"], "official_insights_source")

    def test_roundups_and_official_person_views_are_excluded(self):
        cases = (
            (
                "[뉴스모음] 삼전닉스 3복층 반도체팹 추진 外",
                "compound_roundup",
            ),
            (
                "McKinsey’s Fangning Zhang on China’s growing role in life sciences R&D",
                "official_person_view",
            ),
        )

        for title, reason in cases:
            with self.subTest(title=title):
                result, errors = summarize({
                    "title": title,
                    "description": "여러 소식 또는 임직원 견해를 소개한다.",
                    "source": "McKinsey Insights",
                    "feed": "McKinsey Insights",
                })
                self.assertEqual(errors, [])
                self.assertTrue(result["editorial_excluded"])
                self.assertEqual(result["editorial_exclusion_reason"], reason)

    def test_impact_bond_is_kept_but_categoryless_contract_is_excluded(self):
        impact_bond, bond_errors = summarize({
            "title": "World Bank Issues $4 Billion Sustainable Development Bond",
            "description": "The bond finances sustainable development projects.",
            "source": "ESG Today",
            "feed": "Example",
        })
        defense_contract, contract_errors = summarize({
            "title": "Boeing awarded $131.2 billion F-15 contract",
            "description": "The defense department awarded the company a contract.",
            "source": "Reuters",
            "feed": "Example",
        })

        self.assertEqual(bond_errors + contract_errors, [])
        self.assertEqual(impact_bond["category"], IMPACT)
        self.assertFalse(impact_bond["editorial_excluded"])
        self.assertTrue(defense_contract["editorial_excluded"])
        self.assertEqual(
            defense_contract["editorial_exclusion_reason"],
            "contract_without_category_fit",
        )

    def test_hanja_country_signal_routes_korean_report_to_global_macro(self):
        result, errors = summarize({
            "title": "美국채 금리 상승, 글로벌 금융시장 압박",
            "description": "미국 정부부채 전망을 분석한다.",
            "source": "연합뉴스",
            "feed": "국내 거시/정책 (연합·한경)",
            "region": "korea",
        })

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], MACRO)
        self.assertEqual(result["region"], "global")

    def test_routine_accounting_notice_is_excluded(self):
        result, errors = summarize({
            "title": "Weekly accounting news: IFRS effective dates",
            "description": "A routine technical notice.",
            "source": "PwC",
            "feed": "Example",
            "link": "https://www.pwc.com/example",
        })

        self.assertEqual(errors, [])
        self.assertTrue(result["editorial_excluded"])
        self.assertEqual(result["editorial_exclusion_reason"], "title_noise")

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

    def test_non_deal_legal_and_generic_business_are_not_alt_fallbacks(self):
        legal, legal_errors = summarize({
            "title": "KKR to Pay $250 Million to Resolve DOJ Merger Filing Lawsuit",
            "description": "A legal settlement involving merger filing rules.",
            "source": "Example",
            "feed": "Example",
        })
        generic, generic_errors = summarize({
            "title": "Company opens a new regional office",
            "description": "The company expanded its office.",
            "source": "Example",
            "feed": "Example",
        })
        trend, trend_errors = summarize({
            "title": "Private equity fundraising and exit market trends improve",
            "description": "VC and PE capital flows, valuations and exits recover.",
            "source": "Example",
            "feed": "Example",
        })

        self.assertEqual(legal_errors + generic_errors + trend_errors, [])
        self.assertTrue(legal["editorial_excluded"])
        self.assertEqual(legal["editorial_exclusion_reason"], "non_deal_legal_event")
        self.assertFalse(legal["major_deal"])
        self.assertTrue(generic["editorial_excluded"])
        self.assertEqual(
            generic["editorial_exclusion_reason"],
            "general_business_without_category_fit",
        )
        self.assertEqual(trend["category"], ALTERNATIVE)
        self.assertFalse(trend["editorial_excluded"])

    def test_korean_outlet_foreign_alt_deal_is_global(self):
        result, errors = summarize({
            "title": "United States startup raises $200 million Series B",
            "description": "US financing round.",
            "source": "국내 매체",
            "feed": "Example",
            "region": "korea",
        })

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], ALTERNATIVE)
        self.assertEqual(result["region"], "global")
        self.assertEqual(result["region_reason"], "foreign_event_in_korean_source")

    def test_foreign_outlet_korean_macro_event_is_domestic(self):
        result, errors = summarize({
            "title": "South Korea central bank delivers back-to-back rate hike",
            "description": "The Bank of Korea raised its policy rate.",
            "source": "Reuters",
            "feed": "Reuters 거시/정책",
            "region": "global",
        })

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], MACRO)
        self.assertEqual(result["region"], "korea")
        self.assertEqual(result["region_reason"], "korea_event_content")

    def test_only_final_selected_articles_are_translated(self):
        topics = (
            "chip export rule",
            "foundation model launch",
            "data center financing",
            "robotics acquisition",
            "cloud regulation",
            "agent benchmark",
        )
        articles = [
            {
                "category": AI,
                "title": title,
                "title_orig": title,
                "link": [f"https://example.com/{index}"],
                "source": ["Test Source"],
                "date": "2026-08-27",
                "relevance": 100 - index,
            }
            for index, title in enumerate(topics)
        ]

        with patch("src.bot.is_dry_run", return_value=True):
            with patch(
                "src.bot.translate_titles",
                side_effect=lambda selected: selected,
            ) as translate:
                with patch("builtins.print"):
                    success, sent = send_aggregated_slack_news(articles)

        self.assertTrue(success)
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(translate.call_args.args[0]), 3)

    def test_hanja_country_signals_route_korean_article_to_global_region(self):
        result, errors = summarize({
            "title": "자산시장 숨통 조이는 美 국가부채",
            "description": "미국 재무부 부채 부담이 자산시장에 영향을 주고 있다.",
            "source": "한국경제",
            "feed": "국내 거시/정책 (연합·한경)",
            "region": "korea",
        })

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], MACRO)
        self.assertEqual(result["region"], "global")
        self.assertEqual(result["region_reason"], "foreign_event_in_korean_source")

    def test_accounting_standard_notices_are_excluded(self):
        titles = (
            "Cash equivalents and digital assets, FASB effective dates",
            "IFRS 20: new accounting for regulatory assets and liabilities",
            "세법 개정 안내 및 회계 처리 지침",
        )

        for title in titles:
            with self.subTest(title=title):
                result, errors = summarize({
                    "title": title,
                    "description": "",
                    "source": "EY",
                    "feed": "EY Official Insights",
                    "link": "https://www.ey.com/insights/accounting-update",
                })
                self.assertEqual(errors, [])
                self.assertTrue(result["editorial_excluded"])
                self.assertEqual(
                    result["editorial_exclusion_reason"],
                    "title_noise",
                )

    def test_official_big4_domain_link_routes_to_insights(self):
        result, errors = summarize({
            "title": "EY Global IPO Trends Q2 2026",
            "description": "Global IPO activity and market forecast.",
            "source": "EY",
            "feed": "글로벌 VC/PE",
            "link": "https://www.ey.com/en_th/insights/ipo/trends",
        })

        self.assertEqual(errors, [])
        self.assertEqual(result["category"], INSIGHTS)
        self.assertEqual(result["category_reason"], "official_insights_source")


class DuplicateProtectionTests(unittest.TestCase):
    def test_same_mortgage_statistic_headlines_are_one_event(self):
        yonhap = {
            "title": "2분기 신규 주담대 평균 2억829만원…대출 규제에 역대 최대폭↓",
            "date": "2026-08-25",
        }
        hankyung = {
            "title": "대출 규제 영향…2분기 신규 주담대 역대 최대폭↓",
            "date": "2026-08-25",
        }

        self.assertTrue(_same_headline_event(yonhap, hankyung))

    def test_policy_decision_and_immediate_market_reaction_are_merged(self):
        decision = {
            "title": "Turkey's Central Bank Shifts Funding Back to 37% Policy Rate",
            "description": "The central bank restored funding at its main policy rate.",
            "date": "2026-08-24",
        }
        reaction = {
            "title": "Turkish Banks Rally as Central Bank Restores Cheaper Funding",
            "description": "Bank shares rose after the central bank changed funding policy.",
            "date": "2026-08-24",
        }

        self.assertTrue(_should_merge(decision, reaction, similarity=0.65))

    def test_different_policy_rate_values_are_not_merged(self):
        first_rate = {
            "title": "Turkey's Central Bank Moves to 37% Policy Rate",
            "description": "Local markets rallied after the policy decision.",
            "date": "2026-08-24",
        }
        second_rate = {
            "title": "Turkish Banks Rally as Central Bank Moves to 40% Policy Rate",
            "description": "Banks gained after the policy rate announcement.",
            "date": "2026-08-24",
        }

        self.assertFalse(_should_merge(first_rate, second_rate, similarity=0.65))

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

    def test_topic_series_articles_are_merged(self):
        common = {
            "source": "머니투데이",
            "feed": "국내 포용·상생금융",
            "gnews_link": "https://news.google.com/example",
        }
        articles = [
            {
                **common,
                "title": "개미 울 때 10조 번 증권가, 사회 기여 나선다",
                "description": "금투협 중심 사회기여 공동방안 논의 착수",
                "link": "https://www.mt.co.kr/story/1",
                "date": "2026-08-18",
            },
            {
                **common,
                "title": "불장에 10조 대박 증권사, 사회 기여 나선다",
                "description": "증권업계 이익나누기 종합",
                "link": "https://www.mt.co.kr/story/2",
                "date": "2026-08-20",
            },
            {
                **common,
                "title": "청년자산형성·모험자본, 증권업계 상생금융",
                "description": "금융투자교육과 상생펀드 조성",
                "link": "https://www.mt.co.kr/story/3",
                "date": "2026-08-18",
            },
        ]
        similarity_matrix = [
            [1.00, 0.65, 0.45],
            [0.65, 1.00, 0.69],
            [0.45, 0.69, 1.00],
        ]
        fake_model = types.SimpleNamespace(
            encode=lambda titles, convert_to_numpy=True: titles
        )

        with (
            patch.object(deduplicator_module, "_get_model", return_value=fake_model),
            patch.object(
                deduplicator_module,
                "cosine_similarity",
                return_value=similarity_matrix,
            ),
        ):
            result, errors = deduplicate_and_merge(articles)

        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["duplicate_count"], 3)

    def test_different_industry_articles_stay_separate(self):
        securities = {
            "title": "증권업계가 사회 기여와 상생금융을 확대한다",
            "source": "머니투데이",
            "feed": "국내 포용·상생금융",
            "date": "2026-08-20",
        }
        banking = {
            "title": "은행권이 취약계층 포용금융을 확대한다",
            "source": "머니투데이",
            "feed": "국내 포용·상생금융",
            "date": "2026-08-20",
        }

        self.assertFalse(_should_merge(securities, banking, similarity=0.95))

    def test_original_publisher_link_beats_aggregator(self):
        relay = {
            "title": "증권업계 사회 기여 논의 (종합)",
            "description": "최신 종합 기사",
            "source": "머니투데이",
            "feed": "국내 포용·상생금융",
            "link": "https://v.daum.net/v/123",
            "gnews_link": "https://news.google.com/123",
            "date": "2026-08-21",
        }
        original = {
            "title": "증권업계 사회 기여 논의",
            "description": "언론사 원문",
            "source": "머니투데이",
            "feed": "국내 포용·상생금융",
            "link": "https://www.mt.co.kr/story/original",
            "gnews_link": "",
            "date": "2026-08-20",
        }

        result = _merge_group([relay, original])

        self.assertEqual(result["link"][0], original["link"])

    def test_comprehensive_article_becomes_representative(self):
        base = {
            "description": "증권업계 사회 기여 공동방안",
            "source": "머니투데이",
            "feed": "국내 포용·상생금융",
            "gnews_link": "",
        }
        plain = {
            **base,
            "title": "증권업계 사회 기여 논의",
            "link": "https://www.mt.co.kr/story/plain",
            "date": "2026-08-20",
        }
        comprehensive = {
            **base,
            "title": "증권업계 사회 기여 논의 (종합)",
            "link": "https://www.mt.co.kr/story/comprehensive",
            "date": "2026-08-19",
        }

        result = _merge_group([plain, comprehensive])

        self.assertEqual(result["title"], comprehensive["title"])


class SelectionAndDateTests(unittest.TestCase):
    def test_decision_record_keeps_editor_event_audit_fields(self):
        record = _decision_record({
            "title": "한국은행 기준금리 인상",
            "region": "korea",
            "region_reason": "korea_event_content",
            "editor_reason": "policy_change",
            "editor_score": 9,
            "editor_event_key": "bank_of_korea_rate_hike_2026_08_27",
        }, "sent")

        self.assertEqual(record["region"], "korea")
        self.assertEqual(record["region_reason"], "korea_event_content")
        self.assertEqual(record["editor_score"], 9)
        self.assertEqual(
            record["editor_event_key"],
            "bank_of_korea_rate_hike_2026_08_27",
        )

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

    def test_final_impact_selection_uses_an_alternative_source(self):
        ranked = [
            {
                "title": f"ImpactOn article {index}",
                "source": "ImpactOn",
                "category": IMPACT,
            }
            for index in range(3)
        ] + [{
            "title": "ImpactAlpha article",
            "source": "ImpactAlpha",
            "category": IMPACT,
        }]

        with patch(
            "src.bot.filter_near_duplicates",
            side_effect=lambda articles, _threshold: list(articles),
        ):
            selected = _select_category_articles(ranked, IMPACT)

        self.assertEqual(len(selected), 3)
        self.assertEqual(
            [article["source"] for article in selected],
            ["ImpactOn", "ImpactOn", "ImpactAlpha"],
        )

    def test_final_impact_selection_does_not_stop_at_two_with_one_source(self):
        ranked = [
            {
                "title": f"Qualified impact article {index}",
                "source": "Only Impact Source",
                "category": IMPACT,
            }
            for index in range(3)
        ]

        with patch(
            "src.bot.filter_near_duplicates",
            side_effect=lambda articles, _threshold: list(articles),
        ):
            selected = _select_category_articles(ranked, IMPACT)

        self.assertEqual(len(selected), 3)
        self.assertTrue(
            all(article["source"] == "Only Impact Source" for article in selected)
        )

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


class RegionalBriefingTests(unittest.TestCase):
    def test_core_macro_markets_are_kept(self):
        articles = (
            {"title": "Federal Reserve signals a rate cut", "region": "global"},
            {"title": "ECB reviews the eurozone policy rate", "region": "global"},
            {"title": "China cuts its benchmark lending rate", "region": "global"},
            {"title": "Bank of Japan changes bond policy", "region": "global"},
            {"title": "한국은행이 기준금리를 동결했다", "region": "korea"},
        )

        for article in articles:
            with self.subTest(title=article["title"]):
                self.assertEqual(_macro_geography_scope(article), "core_market")

    def test_non_core_local_macro_is_excluded_but_global_shock_is_kept(self):
        local_only = {
            "title": "Turkey's Central Bank Shifts Funding Back to 37% Policy Rate",
            "description": "Turkish banks reacted to cheaper local funding.",
            "region": "global",
        }
        global_shock = {
            "title": "Iran attack threatens oil supply through Strait of Hormuz",
            "description": "World oil prices rise as global markets assess the disruption.",
            "region": "global",
        }

        self.assertEqual(_macro_geography_scope(local_only), "non_core_local")
        self.assertEqual(_macro_geography_scope(global_shock), "global_exception")

    @patch.dict("os.environ", {"GEMINI_API_KEY": ""})
    def test_macro_pipeline_removes_non_core_local_news(self):
        articles = [
            {
                "title": "Turkey's Central Bank Shifts Funding Back to 37% Policy Rate",
                "description": "Turkish banks reacted to local funding.",
                "region": "global",
                "category": MACRO,
            },
            {
                "title": "Federal Reserve signals a rate cut",
                "description": "U.S. inflation continues to cool.",
                "region": "global",
                "category": MACRO,
            },
            {
                "title": "Iran attack threatens oil supply through Strait of Hormuz",
                "description": "World oil prices rise after the disruption.",
                "region": "global",
                "category": MACRO,
            },
        ]

        # Windows의 기본 콘솔 인코딩이 안내용 이모지를 표시하지 못할 수 있다.
        # 이 테스트는 화면 출력이 아니라 선별 결과만 검증한다.
        with patch("builtins.print"):
            selected = select_top_news_with_llm(articles, [MACRO])
        selected_titles = {article["title"] for article in selected}

        self.assertNotIn(articles[0]["title"], selected_titles)
        self.assertIn(articles[1]["title"], selected_titles)
        self.assertIn(articles[2]["title"], selected_titles)

    def test_macro_source_queries_use_core_markets_and_global_shocks(self):
        for feed_name in ("Reuters 거시/정책", "Bloomberg 거시/정책"):
            query = parse_qs(urlparse(GOOGLE_NEWS_FEEDS[feed_name]).query)["q"][0]
            with self.subTest(feed=feed_name):
                self.assertIn('"Federal Reserve"', query)
                self.assertIn("ECB", query)
                self.assertIn("BOJ", query)
                self.assertIn("PBOC", query)
                self.assertIn('"global markets"', query)
                self.assertIn('"Strait of Hormuz"', query)

    def test_marketinsight_configured_html_is_parsed(self):
        metadata = DIRECT_WEB_SOURCE_METADATA["국내 한경 마켓인사이트"]
        article_url = "https://marketinsight.hankyung.com/article/202608214829r"
        parser = _ConfiguredArticleListParser(metadata)
        parser.feed(
            '<h3 class="news-tit">'
            f'<a href="{article_url}">'
            "IPO 한파인데 실적은 ‘잭팟’…상반기 VC 성과보수 터졌다"
            "</a></h3>"
            '<p class="lead">국내 상장 VC의 회수 성과와 성과보수가 증가했다.</p>'
        )

        self.assertEqual(len(parser.items), 1)
        self.assertEqual(parser.items[0]["link"], article_url)
        self.assertIn("VC 성과보수", parser.items[0]["title"])
        self.assertIn("회수 성과", parser.items[0]["description"])
        self.assertEqual(
            _direct_web_date(
                article_url,
                metadata,
                datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc),
            ),
            ("2026-08-21", True),
        )

    def test_blocked_direct_sources_have_google_news_replacements(self):
        for feed_name in ("국내 한경 마켓인사이트", "McKinsey Korea Insights"):
            with self.subTest(feed=feed_name):
                self.assertFalse(DIRECT_WEB_SOURCE_METADATA[feed_name]["enabled"])
                self.assertIn(feed_name, GOOGLE_NEWS_FEEDS)

        market_query = parse_qs(
            urlparse(GOOGLE_NEWS_FEEDS["국내 한경 마켓인사이트"]).query
        )["q"][0]
        mckinsey_query = parse_qs(
            urlparse(GOOGLE_NEWS_FEEDS["McKinsey Korea Insights"]).query
        )["q"][0]
        self.assertIn("site:marketinsight.hankyung.com/article", market_query)
        self.assertIn("site:mckinsey.com/kr/our-insights", mckinsey_query)

    def test_llm_selection_keeps_three_per_region(self):
        candidates = {
            str(index + 1): {
                "title": f"Candidate {index}",
                "category": ALTERNATIVE if index < 10 else MACRO,
                "region": "korea" if index % 10 < 5 else "global",
            }
            for index in range(20)
        }
        selected = _finalize_llm_selection(
            {
                "selected": list(candidates),
                "impact_must_read": [],
                "major_deals": [],
            },
            candidates,
            [ALTERNATIVE, MACRO],
        )

        for category in (ALTERNATIVE, MACRO):
            self.assertEqual(
                sum(
                    article["category"] == category
                    and article["region"] == "global"
                    for article in selected
                ),
                3,
            )
            self.assertEqual(
                sum(
                    article["category"] == category
                    and article["region"] == "korea"
                    for article in selected
                ),
                3,
            )

    def test_macro_does_not_fill_missing_korea_slots_with_global_news(self):
        ranked = [
            {"title": f"Global macro {index}", "region": "global"}
            for index in range(5)
        ] + [{"title": "Korea macro", "region": "korea"}]

        # 이 테스트는 지역별 한도만 검증한다. 비슷한 가짜 제목을 당일 중복으로
        # 판단하는 별도 기능은 잠시 제외해 두 규칙이 서로 간섭하지 않게 한다.
        with patch(
            "src.bot.filter_near_duplicates",
            side_effect=lambda articles, _threshold: list(articles),
        ):
            selected = _select_category_articles(ranked, MACRO)

        self.assertEqual(len(selected), 4)
        self.assertEqual(
            [article["region"] for article in selected],
            ["global", "global", "global", "korea"],
        )

    def test_slack_region_sections_show_global_first(self):
        global_article = {
            "title": "Global deal",
            "link": "https://example.com/global",
            "source": "Reuters",
            "date": "2026-08-24",
            "region": "global",
        }
        korea_article = {
            "title": "Korea deal",
            "link": "https://example.com/korea",
            "source": "한경 마켓인사이트",
            "date": "2026-08-24",
            "region": "korea",
        }
        blocks = _build_slack_blocks({
            ALTERNATIVE: [
                {"article": global_article, "region": "global"},
                {"article": korea_article, "region": "korea"},
            ]
        })
        alternative_block = next(
            block
            for block in blocks
            if block.get("type") == "rich_text"
            and block["elements"][0]["elements"][0]["text"] == ALTERNATIVE
        )

        elements = alternative_block["elements"]
        self.assertEqual(elements[1]["elements"][0]["text"], "해외")
        self.assertEqual(elements[2]["type"], "rich_text_list")
        self.assertEqual(elements[3]["elements"][0]["text"], "국내")
        self.assertEqual(elements[4]["type"], "rich_text_list")


if __name__ == "__main__":
    unittest.main()

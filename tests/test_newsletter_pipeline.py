"""Offline regressions for newsletter extraction, review, and failure isolation.

Synthetic mail only: no Gmail credentials, Slack delivery, or paid model calls.
"""

from email.message import EmailMessage
import json
import unittest
from unittest import mock

from test_editorial_logic import _install_optional_dependency_stubs

_install_optional_dependency_stubs()

from src import bot
from src.config import NEWSLETTER_SOURCES
from src.fetchers import gmail_newsletters as gmail
from src.fetchers.newsletter_body import public_story_url
from src.processor import editor
from src.processor.deduplicator import _merge_group
from src.processor.summarizer import summarize
from src.utils.newsletters import attach_body_notes, identify_newsletter
from src.utils.publishers import publisher_id


def mail(html="", plain="RSVP for our next event", sender="ImpactAlpha Open <news@impactalpha.com>",
         subject="Today's newsletter"):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = "Wed, 28 Oct 2026 06:00:00 +0000"
    message.set_content(plain)
    if html:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


def card(number, title=None, context=None):
    title = title or f"Climate investors announce new investment strategy number {number}"
    context = context or f"Article-specific evidence for portfolio {number}."
    return (f'<div><h2><a href="https://impactalpha.com/news-{number}/">{title}</a></h2>'
            f'<p>{context}</p></div>')


class NewsletterIdentityTests(unittest.TestCase):
    def test_six_required_sources_and_aliases(self):
        cases = [
            ("Bloomberg Green", "news@news.bloomberg.com", "Daily", "bloomberg_green"),
            ("Bloomberg", "news@bloomberg.com", "Green: climate update", "bloomberg_green"),
            ("DealBook", "news@e.newyorktimes.com", "Daily", ""),
            ("DealBook", "news@e.nytimes.com", "Daily", "sorkin_dealbook"),
            ("Andrew Ross Sorkin", "sorkin@substack.com", "Daily", "sorkin_dealbook"),
            ("ImpactAlpha Open", "news@impactalpha.com", "Daily", "impactalpha"),
            ("The Morning", "news@e.nytimes.com", "Daily", "nyt"),
            ("Andreessen Horowitz", "news@mail.a16z.com", "Daily", "a16z"),
            ("a16z", "a16z@substack.com", "Databricks & the Future of the Firm", "a16z"),
            ("한국개발연구원", "news@eiec.kdi.re.kr", "Daily", "kdi"),
        ]
        for name, address, subject, expected in cases:
            with self.subTest(name=name, address=address):
                self.assertEqual(identify_newsletter(name, address, subject), expected)

    def test_display_name_and_domain_lookalikes_do_not_grant_review_status(self):
        for address in ("bad@evil.test", "bad@impactalpha.com.evil.test", "other@substack.com"):
            self.assertEqual(identify_newsletter("ImpactAlpha Open", address, "Daily"), "")
        self.assertEqual(identify_newsletter("Bloomberg Markets", "news@bloomberg.com", "Markets"), "")

    def test_newsletter_aliases_do_not_inflate_independent_publisher_count(self):
        self.assertEqual(publisher_id("DealBook / Andrew Ross Sorkin"), publisher_id("The New York Times"))
        self.assertEqual(publisher_id("ImpactAlpha Open"), publisher_id("Impact Alpha"))
        self.assertEqual(publisher_id("a16z", "https://www.a16z.news/p/story"),
                         publisher_id("Andreessen Horowitz", "https://a16z.com/story"))

    def test_new_source_requires_only_configuration(self):
        extra = {"new_source": {"name": "New Research", "domains": ("research.test",),
                               "gmail_query": "from:research.test", "topics": (), "region": "korea"}}
        with mock.patch.dict(NEWSLETTER_SOURCES, extra):
            item = gmail._parse_email(mail(card(1), sender="Research <news@research.test>"))[0]
        self.assertEqual(item["source"], "New Research")
        self.assertEqual(item["newsletter_sources"], ["new_source"])
        self.assertEqual(item["region"], "korea")


class NewsletterExtractionTests(unittest.TestCase):
    def test_single_story_body_uses_public_permalink_without_tracking_query(self):
        title = "Databricks & the Future of the Firm"
        html = (
            '<h1><a href="https://open.substack.com/pub/a16z/p/databricks-and-the-future-of-the?token=private">'
            + title + '</a></h1><div class="body markup"><p>'
            + "Agents and databases are changing how firms are organized. " * 12
            + '</p></div><p>Subscribe to get future posts.</p>'
        )
        article = gmail._parse_email(mail(html, sender="a16z <a16z@substack.com>", subject=title))[0]
        self.assertEqual(article["link"],
                         "https://www.a16z.news/p/databricks-and-the-future-of-the")
        self.assertTrue(article["newsletter_primary_story"])
        self.assertIn("Agents and databases", article["description"])
        self.assertNotIn("Subscribe", article["description"])
        self.assertLessEqual(len(article["description"]), 600)
        self.assertEqual(article["title"], title)

    def test_public_post_rewrite_rejects_other_hosts_publications_and_paths(self):
        policy = NEWSLETTER_SOURCES["a16z"]
        for url in (
            "https://evil.test/pub/a16z/p/databricks",
            "http://open.substack.com/pub/a16z/p/databricks",
            "https://open.substack.com/pub/other/p/databricks",
            "https://open.substack.com/pub/a16z/subscribe",
            "https://open.substack.com/pub/a16z/p/databricks/extra",
        ):
            with self.subTest(url=url):
                self.assertEqual(public_story_url(url, policy), "")

    def test_app_link_title_can_use_separately_verified_public_permalink(self):
        title = "Databricks & the Future of the Firm"
        html = (
            '<h1><a href="https://substack.com/app-link/post?token=private">' + title + '</a></h1>'
            + '<div class="body markup"><p>'
            + "Agents and databases are changing how firms are organized. " * 12
            + '</p></div>'
            + '<a href="https://open.substack.com/pub/a16z/p/databricks-and-the-future-of-the?token=private">'
            + 'Read full story</a>'
        )
        item = gmail._parse_email(mail(html, sender="a16z <a16z@substack.com>", subject=title))[0]
        self.assertEqual(item["link"], "https://www.a16z.news/p/databricks-and-the-future-of-the")
        self.assertTrue(item["newsletter_primary_story"])
        self.assertIn("Agents and databases", item["description"])

    def test_unlinked_dealbook_essay_is_evidence_not_sendable_article(self):
        html = ('<p>Good morning. Andrew here. We have a breakdown of Anthropic’s I.P.O. filing. '
                + "Spark Capital and its investors may see big gains. " * 8
                + '</p><p>Here is how the banks may earn fees.</p>'
                + '<a href="https://nl.nytimes.com/f/a/tracking">Subscribe to The New York Times</a>')
        records = gmail._parse_email(mail(html, sender="Andrew Ross Sorkin <nytdirect@nytimes.com>",
                                         subject="DealBook: Anthropic’s winners and losers"))
        notes = [item for item in records if item.get("newsletter_body_only")]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "Anthropic’s winners and losers")
        self.assertEqual(notes[0]["link"], "")
        self.assertIn("I.P.O. filing", notes[0]["description"])
        self.assertFalse(any(item.get("link") for item in records))

    def test_impactalpha_bullets_yield_bounded_notes_not_rsvp(self):
        useful = ('<li><span><strong>Revolving loan fund keeps heirs’ properties out of speculators’ hands.</strong>'
                  + " A New York program protects affordable family housing with patient capital." * 5
                  + '</span></li>')
        promo = ('<li><strong>RSVP for the investor webinar next week.</strong>'
                 + " Sign up now to join our speaker panel." * 8 + '</li>')
        notes = [item for item in gmail._parse_email(mail(useful + promo))
                 if item.get("newsletter_body_only")]
        self.assertEqual(len(notes), 1)
        self.assertIn("Revolving loan fund", notes[0]["title"])
        self.assertEqual(notes[0]["link"], "")

    def test_all_twelve_links_survive_old_first_eight_cut(self):
        articles = gmail._parse_email(mail("".join(card(i) for i in range(12))))
        self.assertEqual(len(articles), 12)
        self.assertIn("number 11", articles[-1]["title"])

    def test_article_context_does_not_copy_rsvp_intro_or_other_article(self):
        articles = gmail._parse_email(mail('<p>RSVP for The Call</p>' + card(1) + card(2),
                                          subject="RSVP: newsletter and invitation"))
        self.assertEqual(articles[0]["description"], "Article-specific evidence for portfolio 1.")
        self.assertEqual(articles[1]["description"], "Article-specific evidence for portfolio 2.")
        self.assertNotIn("RSVP", str(articles))
        self.assertEqual(articles[0]["source"], "Impact Alpha")
        self.assertEqual(articles[0]["date_basis"], "newsletter_date")

    def test_linked_title_and_body_blocks_keep_full_title_and_local_body(self):
        title = "A long climate investment headline " * 6
        html = f'<a href="https://impactalpha.com/article/"><div>{title}</div><div>Local evidence only.</div></a>'
        item = gmail._parse_email(mail(html))[0]
        self.assertEqual(item["title"], title.strip())
        self.assertEqual(item["description"], "Local evidence only.")

    def test_other_story_links_prevent_borrowing_sibling_paragraph(self):
        html = ('<h2><a href="https://impactalpha.com/a/">Climate investment changes across the industry</a></h2>'
                '<p><a href="https://impactalpha.com/b/">Other investors announce a different transaction</a></p>')
        items = gmail._parse_email(mail(html))
        self.assertEqual(items[0]["description"], "")

    def test_single_link_table_cell_does_not_copy_mail_introduction(self):
        html = ('<table><tr><td>RSVP for The Call. '
                '<a href="https://impactalpha.com/story/">Climate investors change their allocation strategy</a>'
                '<p>Article-local allocation evidence.</p></td></tr></table>')
        item = gmail._parse_email(mail(html))[0]
        self.assertEqual(item["description"], "Article-local allocation evidence.")

    def test_plain_alternative_does_not_resurrect_browser_link(self):
        html = '<a href="https://impactalpha.com/browser/">Read in browser</a>' + card(1)
        items = gmail._parse_email(mail(html, plain="Read in browser\nhttps://impactalpha.com/browser/"))
        self.assertEqual(len(items), 1)
        self.assertIn("news-1", items[0]["link"])

    def test_plain_only_uses_link_local_title_not_mail_subject(self):
        items = gmail._parse_email(mail(plain=(
            "Climate investors announce new investment strategy\nhttps://impactalpha.com/story/\n"
            "Read in browser\nhttps://impactalpha.com/browser/\n"
        ), subject="RSVP for next week"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Climate investors announce new investment strategy")
        self.assertEqual(items[0]["description"], "")

    def test_privacy_policy_keyword_does_not_reject_a_privacy_news_headline(self):
        self.assertFalse(gmail._is_boilerplate_anchor("New privacy regulations reshape the AI industry"))

    def test_safety_limit_is_audited(self):
        stats = {}
        with mock.patch.object(gmail, "GMAIL_LINKS_PER_EMAIL", 2), mock.patch("builtins.print") as output:
            items = gmail._parse_email(mail(card(1) + card(2) + card(3)), stats)
        self.assertEqual(len(items), 2)
        self.assertEqual(stats["impactalpha"], {"emails": 1, "links": 2, "overflow": 1})
        self.assertTrue(output.called)


class NewsletterCollectionTests(unittest.TestCase):
    def test_required_sources_search_beyond_recent_cap_and_do_not_mark_read(self):
        client = mock.Mock()
        client.select.return_value = ("OK", [])
        message = mail(card(1))

        def uid(command, *args):
            if command == "search":
                # General search contains UID 9, source searches add older UID 1.
                return "OK", [b"1 9" if "from:" not in args[-1] else b"1"]
            return "OK", [(b"BODY", message)]

        client.uid.side_effect = uid
        with mock.patch.object(gmail, "is_configured", return_value=True), \
                mock.patch.object(gmail, "GMAIL_MAX_EMAILS", 1), \
                mock.patch.object(gmail.imaplib, "IMAP4_SSL", return_value=client), \
                mock.patch("builtins.print"):
            items, errors = gmail.fetch()
        self.assertEqual(errors, [])
        self.assertEqual(len(items), 2)
        client.select.assert_called_once_with(gmail.GMAIL_IMAP_FOLDER, readonly=True)
        fetches = [call for call in client.uid.call_args_list if call.args[0] == "fetch"]
        self.assertEqual([call.args[1] for call in fetches], [b"9", b"1"])
        self.assertTrue(all(call.args[2] == "(BODY.PEEK[])" for call in fetches))
        searches = [call for call in client.uid.call_args_list if call.args[0] == "search"]
        self.assertEqual(len(searches), 1 + len(NEWSLETTER_SOURCES))
        self.assertTrue(all("newer_than:" in call.args[-1] for call in searches))
        self.assertIn(gmail.GMAIL_NEWSLETTER_QUERY, searches[0].args[-1])
        self.assertTrue(all(gmail.GMAIL_NEWSLETTER_QUERY not in call.args[-1] for call in searches[1:]))
        client.logout.assert_called_once()

    def test_required_query_bypasses_only_the_label_not_the_date_window(self):
        for source in NEWSLETTER_SOURCES.values():
            query = gmail._gmail_raw_query(source["gmail_query"])
            self.assertIn(source["gmail_query"], query)
            self.assertIn(f"newer_than:{gmail.GMAIL_LOOKBACK_DAYS}d", query)
            self.assertNotIn("label:newsletters", query)
        self.assertIn("from:a16z@substack.com", NEWSLETTER_SOURCES["a16z"]["gmail_query"])

    def test_non_source_search_keeps_user_query_unchanged(self):
        with mock.patch.object(gmail, "GMAIL_NEWSLETTER_QUERY", "label:newsletters -from:noise.test"):
            self.assertIn("label:newsletters -from:noise.test", gmail._gmail_raw_query())
            self.assertNotIn("-from:noise.test", gmail._gmail_raw_query("from:kdi.re.kr"))

    def test_source_search_error_does_not_discard_general_collection(self):
        client = mock.Mock()
        client.select.return_value = ("OK", [])

        def uid(command, *args):
            if command == "fetch":
                return "OK", [(b"BODY", mail(card(1)))]
            if "from:" in args[-1]:
                raise gmail.imaplib.IMAP4.error("private server detail must not leak")
            return "OK", [b"1"]

        client.uid.side_effect = uid
        with mock.patch.object(gmail, "is_configured", return_value=True), \
                mock.patch.object(gmail.imaplib, "IMAP4_SSL", return_value=client), \
                mock.patch("builtins.print"):
            items, errors = gmail.fetch()
        self.assertEqual(len(items), 1)
        self.assertEqual(len(errors), len(NEWSLETTER_SOURCES))
        self.assertNotIn("private server detail", str(errors))


class NewsletterReviewTests(unittest.TestCase):
    def test_bloomberg_green_stays_impact_after_editor_reclassification(self):
        article = gmail._parse_email(mail(
            card(1, "Batteries Are Helping Electrify Trains Faster"),
            sender="Bloomberg Green <noreply@news.bloomberg.com>",
            subject="Green: This week's developments",
        ))[0]
        article, _ = summarize(article)
        self.assertEqual(article["category"], "🌱 임팩트")
        self.assertEqual(article["category_reason"], "configured_newsletter_category")
        editor._apply(article, {"keep": True, "category": "📈 대체투자", "score": 8,
                               "reason": "funding_round"}, set(editor.CATEGORIES))
        self.assertEqual(article["category"], "🌱 임팩트")

    def test_bloomberg_green_lock_does_not_force_promotions_to_be_sent(self):
        article = {"title": "RSVP for the climate investor webinar",
                   "feed": "Gmail Newsletters", "newsletter_source": "bloomberg_green"}
        article, _ = summarize(article)
        self.assertEqual(article["category"], "🌱 임팩트")
        self.assertTrue(article["editorial_excluded"])
        editor._apply(article, {"keep": False, "reason": "event_promotion"}, set(editor.CATEGORIES))
        self.assertTrue(article["editorial_excluded"])

    def test_bloomberg_display_name_alone_does_not_lock_category(self):
        article = gmail._parse_email(mail(
            card(1, "Software company raises Series B funding round"),
            sender="Bloomberg Green <news@untrusted.example>",
        ))[0]
        article, _ = summarize(article)
        self.assertNotEqual(article["category_reason"], "configured_newsletter_category")

    def test_unlinked_note_only_attaches_to_same_event_with_real_article_link(self):
        note = {"title": "Anthropic’s winners and losers",
                "description": "Anthropic’s I.P.O. filing shows which investors may gain.",
                "source": "DealBook", "newsletter_source": "sorkin_dealbook",
                "newsletter_body_only": True, "link": ""}
        related = {"title": "Anthropic IPO filing details gains for early investors",
                   "source": "Bloomberg", "link": "https://example.com/anthropic-ipo"}
        unrelated = {"title": "Anthropic announces a new model for developers",
                     "source": "Reuters", "link": "https://example.com/model"}
        total, attached = attach_body_notes([note, related, unrelated])
        self.assertEqual((total, attached), (1, 1))
        self.assertEqual(len(related["newsletter_body_notes"]), 1)
        self.assertNotIn("newsletter_body_notes", unrelated)
        self.assertEqual(note["filter_reason"], "newsletter_body_attached_to_linked_article")
        self.assertIn("뉴스레터 본문 참고", editor._candidate_block([related], 1))

    def test_unmatched_note_stays_unlinked_and_does_not_invent_source_url(self):
        note = {"title": "Climate finance policy changed in Europe",
                "description": "Evidence about investment and new regulation.",
                "source": "Impact Alpha", "newsletter_source": "impactalpha",
                "newsletter_body_only": True, "link": ""}
        other = {"title": "AI model launch from a major company", "link": "https://example.com/ai"}
        self.assertEqual(attach_body_notes([note, other]), (1, 0))
        self.assertEqual(note["filter_reason"], "newsletter_body_no_verified_public_link")
        self.assertNotIn("newsletter_body_notes", other)

    def test_body_note_does_not_attach_to_old_or_generic_same_sector_story(self):
        note = {"title": "Revolving loan fund keeps heirs’ properties out of speculators’ hands",
                "description": "Housing investment for families.",
                "date": "2026-10-28", "newsletter_body_only": True, "link": ""}
        old = {"title": "Revolving loan fund keeps heirs’ properties out of speculators’ hands",
               "date": "2026-10-20", "link": "https://example.com/old"}
        generic = {"title": "Climate finance policy changes across investment markets",
                   "date": "2026-10-28", "link": "https://example.com/generic"}
        self.assertEqual(attach_body_notes([note, old, generic]), (1, 0))

    def test_editor_impact_evidence_can_quote_visible_newsletter_note_only(self):
        article = {"title": "Investment in housing", "description": "A local fund raises capital.",
                   "newsletter_body_notes": [{"source": "Impact Alpha", "title": "Housing access",
                                              "excerpt": "This capital preserves affordable family homes."}]}
        self.assertEqual(editor._quoted_input_evidence(article, "preserves affordable family homes"),
                         "preserves affordable family homes")
        self.assertEqual(editor._quoted_input_evidence(article, "imaginary additional benefit"), "")

    def ambiguous(self):
        # Generic required-review streams remain content-classified. Green now
        # has a deliberate category lock, covered by dedicated tests above.
        item = gmail._parse_email(mail(card(1, "EPA Readies Bid to Ease Methane Controls for Oil, Gas"),
                                      sender="The Morning <news@nytimes.com>"))[0]
        item, _ = summarize(item)
        return item

    def test_article_is_not_rejected_by_rsvp_in_context_but_invitation_is(self):
        article = {"title": "Ceniarth revises its patient capital portfolio",
                   "description": "RSVP for the related call", "feed": "Gmail Newsletters"}
        self.assertTrue(bot.is_relevant(article, require_topic_match=False))
        article["title"] = "RSVP for The Call on patient capital"
        self.assertFalse(bot.is_relevant(article, require_topic_match=False))
        self.assertEqual(article["filter_reason"], "hard_exclusion:rsvp")

    def test_ambiguous_required_newsletter_reaches_editor_and_can_be_rescued(self):
        article = self.ambiguous()
        self.assertTrue(article["editorial_excluded"])

        def review(items):
            self.assertEqual(items, [article])
            editor._apply(article, {"keep": True, "category": "🌱 임팩트", "score": 8,
                                   "reason": "policy_change", "impact_basis": "sustainability_rules",
                                   "impact_evidence": "Methane Controls"}, set(editor.CATEGORIES))
            return items, []

        with mock.patch.object(bot, "editor_gate_enabled", return_value=True), \
                mock.patch.object(editor, "is_enabled", return_value=True), \
                mock.patch.object(editor, "review", side_effect=review), mock.patch("builtins.print"):
            selected, rejected, _ = bot.select_for_briefing([article])
        self.assertEqual(selected, [article])
        self.assertEqual(rejected, [])
        self.assertEqual(article["pre_editor_exclusion_reason"], "general_business_without_category_fit")
        self.assertEqual(article["editorial_exclusion_reason"], "")
        self.assertEqual(article["category"], "🌱 임팩트")
        self.assertEqual(article["editor_impact_evidence"], "Methane Controls")

    def test_failure_missing_verdict_and_rejection_never_auto_include(self):
        for result in ("failed", "missing", "reject"):
            with self.subTest(result=result):
                article = self.ambiguous()

                def review(items):
                    if result == "failed":
                        return None, ["test failure"]
                    if result == "reject":
                        editor._apply(article, {"keep": False, "reason": "low_relevance"}, set(editor.CATEGORIES))
                        return [], []
                    article["editor_verdict"] = "unreviewed"
                    return items, []

                with mock.patch.object(bot, "editor_gate_enabled", return_value=True), \
                        mock.patch.object(editor, "is_enabled", return_value=True), \
                        mock.patch.object(editor, "review", side_effect=review), \
                        mock.patch.object(bot, "rerank_by_category", side_effect=lambda items, _: items), \
                        mock.patch("builtins.print"):
                    selected, rejected, _ = bot.select_for_briefing([article])
                self.assertEqual(selected, [])
                self.assertEqual(rejected, [article])
                self.assertFalse(bot._is_sendable(article))

    def test_no_api_or_unknown_sender_keeps_previous_safe_filter(self):
        for registered, enabled in ((True, False), (False, True)):
            article = self.ambiguous()
            if not registered:
                article.pop("newsletter_source", None)
                article.pop("newsletter_sources", None)
            with mock.patch.object(bot, "editor_gate_enabled", return_value=True), \
                    mock.patch.object(editor, "is_enabled", return_value=enabled), \
                    mock.patch.object(editor, "review", return_value=([], [])) as review, \
                    mock.patch("builtins.print"):
                selected, rejected, _ = bot.select_for_briefing([article])
            review.assert_called_once_with([])
            self.assertEqual(selected, [])
            self.assertEqual(rejected, [article])

    def test_known_source_does_not_rescue_title_noise(self):
        article = gmail._parse_email(mail(card(1, "RSVP for The Call on impact-first investing")))[0]
        article, _ = summarize(article)
        with mock.patch.object(bot, "editor_gate_enabled", return_value=True), \
                mock.patch.object(editor, "is_enabled", return_value=True), \
                mock.patch.object(editor, "review", return_value=([], [])) as review, \
                mock.patch("builtins.print"):
            selected, _, _ = bot.select_for_briefing([article])
        review.assert_called_once_with([])
        self.assertEqual(selected, [])

    def test_rss_merge_preserves_newsletter_provenance_and_own_context(self):
        newsletter = gmail._parse_email(mail(card(1)))[0]
        rss = dict(newsletter, source="RSS", feed="RSS", description="RSS article context")
        rss.pop("newsletter_source")
        rss.pop("newsletter_sources")
        with mock.patch("src.processor.deduplicator._representative_key", side_effect=lambda item: item is rss):
            merged = _merge_group([newsletter, rss])
        self.assertEqual(merged["newsletter_sources"], ["impactalpha"])
        self.assertEqual(merged["description"], "RSS article context")
        self.assertEqual(bot._decision_record(merged, "not_selected")["newsletter_sources"], ["impactalpha"])

    def test_kdi_and_a16z_not_classified_as_mbb_and_hints_do_not_force_selection(self):
        for sender, source in (("KDI <news@kdi.re.kr>", "kdi"), ("a16z <news@a16z.com>", "a16z")):
            article = gmail._parse_email(mail(card(1), sender=sender))[0]
            article, _ = summarize(article)
            self.assertFalse(article["category"].startswith("👔"))
            block = editor._candidate_block([article], 1)
            self.assertIn("강제 분류/선정 금지", block)
            self.assertEqual(article["newsletter_sources"], [source])

    def test_decision_audit_includes_source_counts_and_pre_editor_reason(self):
        article = self.ambiguous()
        article["pre_editor_exclusion_reason"] = article["editorial_exclusion_reason"]
        article["newsletter_review_deferred"] = True
        article["filter_reason"] = "newsletter_review_unavailable"
        output = mock.mock_open()
        with mock.patch("builtins.open", output), mock.patch.object(bot.Path, "mkdir"), \
                mock.patch("builtins.print"):
            bot.save_run_decisions([article], [], [])
        saved = json.loads("".join(call.args[0] for call in output().write.call_args_list))
        counts = saved["newsletter_summary"]["nyt"]
        self.assertEqual(counts, {"decisions": 1, "reviewed": 0, "sent": 0, "deferred": 1})
        self.assertEqual(saved["decisions"][0]["editorial_exclusion_reason"],
                         "general_business_without_category_fit")
        self.assertNotIn("description", saved["decisions"][0])


if __name__ == "__main__":
    unittest.main()

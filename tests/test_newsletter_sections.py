"""Synthetic, offline regressions for secondary newsletter stories."""

from unittest import TestCase, mock
from email.message import EmailMessage

from test_editorial_logic import _install_optional_dependency_stubs
_install_optional_dependency_stubs()

from src.config import NEWSLETTER_SOURCES
from src.fetchers import gmail_newsletters as gmail
from src.fetchers.newsletter_body import section_stories
from src.fetchers.newsletter_redirects import resolve_attributed_url


TITLE = "Ben & Jerry’s founders pressure its owners to sell"
BODY = ("The founders are discussing an employee stock-ownership plan, or ESOP, "
        "to preserve employee ownership and the company's social mission. " * 3)
TRACKER = "https://nl.nytimes.com/f/a/opaque-reference"
PUBLIC = "https://www.wsj.com/business/employee-ownership-deal"


def section(title=TITLE, link=TRACKER, anchor="The Wall Street Journal reports"):
    return f'<h2>{title}</h2><p>{BODY}</p><p><a href="{link}">{anchor}</a></p>'


def parse(html):
    message = EmailMessage()
    message["From"] = "Andrew Ross Sorkin <nytdirect@nytimes.com>"
    message["Subject"] = "DealBook: An unrelated lead story"
    message.set_content("Unrelated lead story")
    message.add_alternative(html, subtype="html")
    return gmail._parse_email(message.as_bytes())


class NewsletterSectionTests(TestCase):
    def test_unlinked_title_is_paired_with_explicit_attribution(self):
        records = parse(section())
        item = next(item for item in records if item["title"] == TITLE)
        self.assertIn("ESOP", item["description"])
        self.assertEqual(item["link"], TRACKER)
        self.assertEqual(sum(item["link"] == TRACKER for item in records), 1)
        with mock.patch.object(gmail, "resolve_attributed_url", return_value=PUBLIC):
            gmail._resolve_section_links(records)
        self.assertEqual(item["link"], PUBLIC)
        self.assertEqual(item["newsletter_section_link_status"], "resolved")

    def test_missing_or_failed_link_is_evidence_only(self):
        for html in (section(), '<h2>' + TITLE + '</h2><p>' + BODY + '</p>'):
            with self.subTest(html=html[:30]):
                records = parse(html)
                with mock.patch.object(gmail, "resolve_attributed_url", return_value=""):
                    gmail._resolve_section_links(records)
                item = next(item for item in records if item["title"] == TITLE)
                self.assertEqual(item["link"], "")
                self.assertTrue(item["newsletter_body_only"])

    def test_adjacent_story_and_footer_are_not_borrowed(self):
        html = (section() + '<h2>Separate macroeconomic development today</h2>'
                '<p>UNRELATED EVIDENCE ' + BODY + '</p>')
        records = section_stories(html, NEWSLETTER_SOURCES["sorkin_dealbook"], 1200)
        self.assertNotIn("UNRELATED", records[0]["description"])
        self.assertEqual(records[1]["link"], "")

    def test_background_and_promotion_links_are_not_article_attribution(self):
        records = section_stories(section(anchor="acquired"), NEWSLETTER_SOURCES["sorkin_dealbook"], 1200)
        self.assertEqual(records[0]["link"], "")

    def test_conflicting_attributions_are_not_arbitrarily_chosen(self):
        html = section() + '<p><a href="https://reuters.com/business/another-story">Reuters reports</a></p>'
        records = section_stories(html, NEWSLETTER_SOURCES["sorkin_dealbook"], 1200)
        self.assertEqual(records[0]["link"], "")

    def test_unknown_sender_does_not_gain_section_privileges(self):
        self.assertEqual(section_stories(section(), {}, 1200), [])

    def test_section_limit_and_headline_preservation(self):
        policy = dict(NEWSLETTER_SOURCES["sorkin_dealbook"], section_max_articles=2)
        title = "Long original headline " * 20
        records = section_stories(''.join(section(title, f"{TRACKER}/{i}") for i in range(4)), policy, 50)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["title"], title.strip())
        self.assertLessEqual(len(records[0]["description"]), 50)


class AttributionRedirectTests(TestCase):
    def test_direct_article_is_accepted_without_network_or_email_tokens(self):
        with mock.patch("requests.get", side_effect=AssertionError("network forbidden")) as get:
            self.assertEqual(resolve_attributed_url(PUBLIC + "?email_token=private", ("wsj.com",)), PUBLIC)
        get.assert_not_called()

    def test_untrusted_destinations_and_private_networks_are_not_requested(self):
        with mock.patch("requests.get", side_effect=AssertionError("network forbidden")) as get:
            for url in ("http://wsj.com/business/story", "https://127.0.0.1/private",
                        "https://wsj.com.evil.example/story", "https://wsj.com@evil.example/story",
                        "https://www.wsj.com/login", "https://www.wsj.com/"):
                self.assertEqual(resolve_attributed_url(url, ("wsj.com",)), "")
        get.assert_not_called()

    def test_personalized_tracker_is_never_contacted(self):
        with mock.patch("requests.get", side_effect=AssertionError("network forbidden")) as get:
            self.assertEqual(resolve_attributed_url(TRACKER, ("wsj.com",)), "")
            records = gmail._resolve_section_links(parse(section()))
        get.assert_not_called()
        item = next(item for item in records if item["title"] == TITLE)
        self.assertTrue(item["newsletter_body_only"])
        self.assertEqual(item["link"], "")

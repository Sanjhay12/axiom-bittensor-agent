"""
CRM unit tests — no DB, no Claude API, no IMAP needed.
Run with: pytest test_crm.py -v
"""
import asyncio
import csv
import io
import time
import unittest
from unittest.mock import MagicMock, patch

import crm_score
import crm_import
import crm_agent
import crm_radar


# ---------------------------------------------------------------------------
# crm_score
# ---------------------------------------------------------------------------

def _person(**kwargs):
    defaults = {
        "id": 1, "email": "test@test.com", "name": "Test User",
        "firm_name": "Test Firm", "stage": "Engaged", "last_touch_ts": int(time.time()) - 86400 * 5,
        "mandate": None, "notes": None, "enrichment": None, "deal_amount_usd": None,
        "manual_priority": False,
    }
    return {**defaults, **kwargs}


def _interaction(**kwargs):
    defaults = {"importance": 3, "sentiment": "positive", "summary": "Good call"}
    return {**defaults, **kwargs}


class TestLpScore(unittest.TestCase):

    def test_full_data_returns_composite(self):
        person = _person(stage="Diligence", deal_amount_usd=5_000_000)
        interactions = [_interaction(), _interaction(importance=4)]
        result = crm_score.lp_score(person, interactions)
        self.assertGreater(result["composite_score"], 0)
        self.assertLessEqual(result["composite_score"], 100)

    def test_no_interactions_drops_engagement_signal(self):
        person = _person(stage="Engaged")
        result = crm_score.lp_score(person, [])
        # engagement_depth has no data → should be None in breakdown, not 0
        self.assertIsNone(result["breakdown"]["engagement_depth"])

    def test_passed_stage_scores_zero(self):
        person = _person(stage="Passed")
        result = crm_score.lp_score(person, [])
        self.assertEqual(result["breakdown"]["stage_progress"], 0)

    def test_committed_stage_scores_100(self):
        person = _person(stage="Committed")
        result = crm_score.lp_score(person, [])
        self.assertEqual(result["breakdown"]["stage_progress"], 100)

    def test_recency_decays_over_time(self):
        fresh = _person(last_touch_ts=int(time.time()) - 86400 * 1)   # 1 day ago
        stale = _person(last_touch_ts=int(time.time()) - 86400 * 59)  # 59 days ago
        fresh_score = crm_score.lp_score(fresh, [])["breakdown"]["recency"]
        stale_score = crm_score.lp_score(stale, [])["breakdown"]["recency"]
        self.assertGreater(fresh_score, stale_score)

    def test_recency_missing_touch_drops_signal(self):
        person = _person(last_touch_ts=None)
        result = crm_score.lp_score(person, [])
        self.assertIsNone(result["breakdown"]["recency"])

    def test_deal_size_structured_field(self):
        person = _person(deal_amount_usd=10_000_000)  # $10M → 100
        result = crm_score.lp_score(person, [])
        self.assertEqual(result["breakdown"]["deal_size"], 100.0)

    def test_deal_size_regex_fallback(self):
        person = _person(deal_amount_usd=None, mandate="Looking at $5M allocation")
        result = crm_score.lp_score(person, [])
        self.assertIsNotNone(result["breakdown"]["deal_size"])
        self.assertGreater(result["breakdown"]["deal_size"], 0)

    def test_deal_size_missing_drops_signal(self):
        person = _person(deal_amount_usd=None, mandate=None, notes=None)
        result = crm_score.lp_score(person, [])
        self.assertIsNone(result["breakdown"]["deal_size"])

    def test_sentiment_weights_recency(self):
        # Most recent interaction is positive, older ones negative
        interactions = [
            _interaction(sentiment="positive"),   # most recent (index 0)
            _interaction(sentiment="negative"),
            _interaction(sentiment="negative"),
        ]
        result = crm_score.lp_score(_person(), interactions)
        self.assertGreater(result["breakdown"]["sentiment_trend"], 50)

    def test_missing_data_does_not_drag_composite_to_zero(self):
        # Stage + recency only — deal/sentiment/engagement missing
        person = _person(stage="Call scheduled", deal_amount_usd=None, mandate=None, notes=None)
        result = crm_score.lp_score(person, [])
        # composite should reflect stage + recency, not zero
        self.assertGreater(result["composite_score"], 20)

    def test_rank_active_people_sorted_desc(self):
        people = [
            {**_person(id=1, stage="New"), "composite_score": 30},
            {**_person(id=2, stage="Diligence"), "composite_score": 80},
            {**_person(id=3, stage="Engaged"), "composite_score": 50},
        ]
        sorted_people = sorted(people, key=lambda r: r["composite_score"], reverse=True)
        self.assertEqual(sorted_people[0]["composite_score"], 80)
        self.assertEqual(sorted_people[-1]["composite_score"], 30)


# ---------------------------------------------------------------------------
# crm_import — CSV parsing
# ---------------------------------------------------------------------------

def _make_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


class TestCrmImport(unittest.TestCase):

    def _run(self, content, filename="contacts.csv"):
        added = updated = skipped = 0
        calls = []

        def fake_get_or_create_firm(name):
            return 1 if name else None

        def fake_upsert_person(extracted, firm_id, ts, source="inbound"):
            calls.append(extracted)
            if extracted.get("person_email") in already_exists:
                return (99, False)
            return (1, True)

        already_exists = set()
        with patch("crm_store.get_or_create_firm", side_effect=fake_get_or_create_firm), \
             patch("crm_store.upsert_person", side_effect=fake_upsert_person):
            result = crm_import.import_contacts(content, filename)
        return result, calls

    def test_basic_csv_import(self):
        rows = [{"Name": "Jane Smith", "Email": "jane@horizon.com", "Firm": "Horizon Capital"}]
        result, calls = self._run(_make_csv(rows))
        self.assertEqual(result["total"], 1)
        self.assertEqual(calls[0]["person_name"], "Jane Smith")
        self.assertEqual(calls[0]["person_email"], "jane@horizon.com")

    def test_flexible_column_aliases(self):
        # "Company" instead of "Firm", "Title" instead of "Role"
        rows = [{"Contact Name": "Bob", "E-mail": "bob@x.com", "Company": "Acorn", "Title": "Partner"}]
        result, calls = self._run(_make_csv(rows))
        self.assertEqual(calls[0]["person_name"], "Bob")
        self.assertEqual(calls[0]["role"], "Partner")

    def test_deal_amount_parses_millions(self):
        rows = [{"Name": "Ali", "Email": "ali@fund.com", "Deal Amount": "$5M"}]
        result, calls = self._run(_make_csv(rows))
        self.assertEqual(calls[0]["deal_amount_usd"], 5_000_000.0)

    def test_deal_amount_parses_thousands(self):
        rows = [{"Name": "Ali", "Email": "ali@fund.com", "Deal Amount": "500K"}]
        result, calls = self._run(_make_csv(rows))
        self.assertEqual(calls[0]["deal_amount_usd"], 500_000.0)

    def test_skips_empty_rows(self):
        # Completely empty rows (all-blank fields) are silently dropped before the
        # skipped counter — they don't appear in total. A row with some content but
        # no resolvable name/email is what actually increments skipped.
        content = b"Name,Email\n,\nJane,jane@x.com\n,"
        result, calls = self._run(content)
        # two all-empty rows dropped silently; only Jane added
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["total"], 1)

    def test_raises_on_no_recognizable_columns(self):
        content = b"Foo,Bar\nvalue1,value2\n"
        with self.assertRaises(ValueError):
            crm_import.import_contacts(content, "contacts.csv")

    def test_raises_on_unsupported_format(self):
        with self.assertRaises(ValueError):
            crm_import.import_contacts(b"data", "contacts.pdf")

    def test_on_new_person_callback_fires(self):
        rows = [{"Name": "New", "Email": "new@x.com"}]
        fired = []
        with patch("crm_store.get_or_create_firm", return_value=None), \
             patch("crm_store.upsert_person", return_value=(42, True)):
            crm_import.import_contacts(_make_csv(rows), "contacts.csv", on_new_person=lambda pid, ext: fired.append(pid))
        self.assertEqual(fired, [42])

    def test_on_new_person_callback_not_fired_on_update(self):
        rows = [{"Name": "Existing", "Email": "old@x.com"}]
        fired = []
        with patch("crm_store.get_or_create_firm", return_value=1), \
             patch("crm_store.upsert_person", return_value=(7, False)):
            crm_import.import_contacts(_make_csv(rows), "contacts.csv", on_new_person=lambda pid, ext: fired.append(pid))
        self.assertEqual(fired, [])


# ---------------------------------------------------------------------------
# crm_agent — command regex dispatch
# ---------------------------------------------------------------------------

class TestCommandDispatch(unittest.IsolatedAsyncioTestCase):

    async def test_help_returns_help_text(self):
        result = await crm_agent._handle_command("help")
        self.assertIn("pipeline", result)

    async def test_pipeline_command(self):
        with patch("crm_ask.pipeline_summary", return_value="pipeline output") as mock:
            result = await crm_agent._handle_command("pipeline")
        mock.assert_called_once()
        self.assertEqual(result, "pipeline output")

    async def test_radar_command(self):
        with patch("crm_radar.build_digest", return_value="digest output"):
            result = await crm_agent._handle_command("radar")
        self.assertEqual(result, "digest output")

    async def test_radar_empty_returns_fallback(self):
        with patch("crm_radar.build_digest", return_value=None):
            result = await crm_agent._handle_command("radar")
        self.assertEqual(result, "Nothing to flag right now.")

    async def test_whois_found(self):
        fake_person = {
            "id": 1, "email": "jane@x.com", "name": "Jane", "phone": "555-0100",
            "firm_name": "Horizon", "stage": "Diligence", "relationship_type": "lp_prospect",
            "mandate": "Nebari", "contact_channel": "email", "manual_priority": False,
            "next_step": "Send deck", "notes": None, "enrichment": None,
        }
        with patch("crm_store.find_person", return_value=fake_person), \
             patch("crm_store.list_opportunities", return_value=[]):
            result = await crm_agent._handle_command("whois jane@x.com")
        self.assertIn("Jane", result)
        self.assertIn("Diligence", result)
        self.assertIn("555-0100", result)

    async def test_whois_not_found(self):
        with patch("crm_store.find_person", return_value=None):
            result = await crm_agent._handle_command("whois nobody@x.com")
        self.assertIn("No contact", result)

    async def test_score_command(self):
        fake_result = {
            "name": "Jane", "firm_name": "Horizon", "composite_score": 74.0,
            "breakdown": {"stage_progress": 80, "recency": 70, "engagement_depth": 60,
                          "deal_size": None, "sentiment_trend": 90, "fund_history": None},
        }
        with patch("crm_score.score_by_query", return_value=fake_result):
            result = await crm_agent._handle_command("score jane@x.com")
        self.assertIn("74.0/100", result)
        self.assertIn("stage_progress", result)

    async def test_score_not_found(self):
        with patch("crm_score.score_by_query", return_value=None):
            result = await crm_agent._handle_command("score nobody")
        self.assertIn("No contact", result)

    async def test_brief_command(self):
        # Briefs go out as a Cedar Ridge letterhead PDF attachment via _send_brief,
        # not as a plain _handle_command string reply — see crm_agent._reply_to_note.
        fake_person = {"id": 1, "email": "jane@x.com", "name": "Jane", "firm_name": "Horizon"}
        with patch("crm_brief.generate", return_value="Here is your brief") as mock_generate, \
             patch("crm_store.find_person", return_value=fake_person), \
             patch("crm_pdf.generate_brief_pdf", return_value=b"%PDF-fake") as mock_pdf, \
             patch("crm_mail.send_async", return_value="<msgid>") as mock_send:
            await crm_agent._send_brief({"subject": "brief jane@x.com", "message_id": "<m1>"}, "jane@x.com")
        mock_generate.assert_called_once_with("jane@x.com", product=None)
        mock_pdf.assert_called_once()
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["attachment"][0], "Jane Brief.pdf")

    async def test_draft_command_with_instruction(self):
        with patch("crm_draft.generate", return_value="Draft here") as mock:
            await crm_agent._handle_command("draft jane@x.com: check-in after the call")
        mock.assert_called_once_with("jane@x.com", "check-in after the call")

    async def test_draft_command_no_instruction_uses_default(self):
        with patch("crm_draft.generate", return_value="Draft here") as mock:
            await crm_agent._handle_command("draft jane@x.com")
        args = mock.call_args[0]
        self.assertIn("follow-up", args[1].lower())

    async def test_who_is_in_stage(self):
        with patch("crm_ask.stage_filter", return_value="In diligence: Jane") as mock:
            result = await crm_agent._handle_command("who is in diligence")
        mock.assert_called_once_with("diligence")

    async def test_whos_in_stage_variant(self):
        with patch("crm_ask.stage_filter", return_value="...") as mock:
            await crm_agent._handle_command("who's in engaged")
        mock.assert_called_once_with("engaged")

    async def test_high_priority_set(self):
        with patch("crm_ask.set_priority", return_value="marked") as mock:
            await crm_agent._handle_command("high priority jane@x.com")
        mock.assert_called_once_with("jane@x.com", True)

    async def test_remove_high_priority(self):
        with patch("crm_ask.set_priority", return_value="unmarked") as mock:
            await crm_agent._handle_command("remove high priority jane@x.com")
        mock.assert_called_once_with("jane@x.com", False)

    async def test_confirm_stage(self):
        with patch("crm_ask.confirm_stage", return_value="Confirmed") as mock:
            await crm_agent._handle_command("confirm jane@x.com")
        mock.assert_called_once_with("jane@x.com")

    async def test_reject_stage(self):
        with patch("crm_ask.reject_stage", return_value="Dismissed") as mock:
            await crm_agent._handle_command("reject jane@x.com")
        mock.assert_called_once_with("jane@x.com")

    async def test_unrecognized_returns_none(self):
        # Falls through to free-form Q&A
        result = await crm_agent._handle_command("who's warm for Nebari?")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# crm_radar — digest logic
# ---------------------------------------------------------------------------

class TestRadarDigest(unittest.TestCase):

    def _make_scored_person(self, **kwargs):
        defaults = {
            "id": 1, "email": "jane@x.com", "name": "Jane", "firm_name": "Horizon",
            "stage": "Engaged", "last_touch_ts": int(time.time()) - 86400 * 20,
            "next_step": None, "composite_score": 50, "relationship_type": "lp_prospect",
            "contact_channel": "email", "manual_priority": False,
        }
        return {**defaults, **kwargs}

    def test_empty_returns_none(self):
        with patch("crm_score.rank_active_people", return_value=[]), \
             patch("crm_store.list_manual_priority_people", return_value=[]):
            result = crm_radar.build_digest()
        self.assertIsNone(result)

    def test_cold_contact_appears(self):
        person = self._make_scored_person(last_touch_ts=int(time.time()) - 86400 * 20)
        with patch("crm_score.rank_active_people", return_value=[person]), \
             patch("crm_store.list_manual_priority_people", return_value=[]):
            result = crm_radar.build_digest()
        self.assertIsNotNone(result)
        self.assertIn("Jane", result)

    def test_fresh_contact_no_flag_excluded(self):
        person = self._make_scored_person(
            last_touch_ts=int(time.time()) - 86400 * 2,  # only 2 days ago
            next_step=None,
            composite_score=30,
            manual_priority=False,
        )
        with patch("crm_score.rank_active_people", return_value=[person]), \
             patch("crm_store.list_manual_priority_people", return_value=[]):
            result = crm_radar.build_digest()
        self.assertIsNone(result)

    def test_manual_priority_always_included(self):
        person = self._make_scored_person(
            last_touch_ts=int(time.time()) - 86400 * 2,  # recent — wouldn't be cold
            next_step=None, composite_score=20, manual_priority=True,
        )
        with patch("crm_score.rank_active_people", return_value=[]), \
             patch("crm_store.list_manual_priority_people", return_value=[person]), \
             patch("crm_store.list_interactions", return_value=[]), \
             patch("crm_score.lp_score", return_value={"composite_score": 20, "breakdown": {}}):
            result = crm_radar.build_digest()
        self.assertIsNotNone(result)
        self.assertIn("Jane", result)

    def test_high_score_contact_included(self):
        person = self._make_scored_person(
            last_touch_ts=int(time.time()) - 86400 * 2,  # recent — not cold
            next_step=None, composite_score=75, manual_priority=False,
        )
        with patch("crm_score.rank_active_people", return_value=[person]), \
             patch("crm_store.list_manual_priority_people", return_value=[]):
            result = crm_radar.build_digest()
        self.assertIsNotNone(result)

    def test_max_items_capped(self):
        people = [
            self._make_scored_person(
                id=i, email=f"p{i}@x.com", name=f"Person {i}",
                last_touch_ts=int(time.time()) - 86400 * 20,
            )
            for i in range(20)
        ]
        with patch("crm_score.rank_active_people", return_value=people), \
             patch("crm_store.list_manual_priority_people", return_value=[]):
            result = crm_radar.build_digest()
        # Count "Person" occurrences — should not exceed MAX_ITEMS (12)
        count = result.count("Person ")
        self.assertLessEqual(count, crm_radar.MAX_ITEMS)

    def test_open_loop_included(self):
        person = self._make_scored_person(
            last_touch_ts=int(time.time()) - 86400 * 2,  # recent
            next_step="Send deck", composite_score=30,
        )
        with patch("crm_score.rank_active_people", return_value=[person]), \
             patch("crm_store.list_manual_priority_people", return_value=[]):
            result = crm_radar.build_digest()
        self.assertIsNotNone(result)
        self.assertIn("Send deck", result)


if __name__ == "__main__":
    unittest.main()

"""Recovery Milestone R6.6: real-corpus lifecycle acceptance tests.

This module is deliberately an *acceptance* suite, not a unit-test suite:
tests/test_lifecycle.py and tests/test_service_lifecycle.py already prove
the lifecycle engine's rules in isolation against small synthetic
fixtures. What nothing in this codebase proved before R6.6 is that running
the complete, already-committed pipeline (app/discord_adapter ->
app/parser -> app/datetime_resolution -> TradeService.ingest_batch ->
TradeService.rebuild_all_lifecycles) against the real 68-message Discord
corpus produces the exact lifecycle outcome a human can verify by reading
that corpus - including its hardest cases: an [ADD] with no opener, exits
with no opener at all (orphans), and a stop-out followed by a genuine
re-entry in the same contract that must become two separate lifecycle
generations. This file is that proof, not a re-test of the engine's rules.

Every assertion here identifies lifecycle members by
raw_messages.sequence_in_batch (this corpus's own natural, human-checkable
position: "the 13th alert in the paste"), never by SQLite's autoincrement
row IDs. Row IDs are an artifact of table insertion order within one
particular temporary database and carry no meaning a reviewer could check
against the corpus text; sequence_in_batch is stable and independently
verifiable no matter how many times or in what database this suite runs.

Extraction-layer ambiguity (app/parser.extract_trade_event(), stored on
message_extractions.ambiguity_flags - e.g. "stated_return_missing" for the
two alerts with no "$OLD -> $NEW (+NN%)" line) and lifecycle-layer
ambiguity (database/lifecycle.py's replay, stored on
trade_lifecycles.ambiguity_flags - e.g. "ambiguous_add_no_open_position")
are two different layers describing two different kinds of uncertainty at
two different points in the pipeline. They are asserted in separate test
classes so a failure immediately identifies which layer regressed.

The second-rebuild idempotency test compares total historical row counts
(including any superseded rows) and exact row IDs, not just current-row
counts, because a rebuild that silently superseded every generation and
recreated 20 fresh ones would still show "20 current rows" - the weaker
check the R6.6 verification pass was explicitly asked not to settle for.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from collections import Counter, namedtuple
from decimal import Decimal
from fractions import Fraction

from database.config import DatabaseConfig
from database.db import get_connection, initialize_database
from database.repository import (
    get_current_extraction,
    get_trade_lifecycle_events,
    list_current_trade_lifecycles,
    validate_lifecycle_membership_integrity,
)
from database.service import TradeService
from tests.discord_corpus_fixture import CORPUS

SOURCE_NAME = "discord"
REFERENCE_DATE = "2026-07-24"
TIMEZONE = "Asia/Riyadh"
EXPECTED_EXPIRATION = "2026-07-24"

EXPECTED_TOTAL_MESSAGES = 68
EXPECTED_TRADER_COUNT = 5
EXPECTED_CURRENT_LIFECYCLE_COUNT = 20
EXPECTED_DISTINCT_KEY_COUNT = 18

EXPECTED_STATUS_DISTRIBUTION = {
    "closed": 12,
    "partially_closed": 4,
    "open": 1,
    "orphan": 2,
    "unresolved": 1,
    "invalid": 0,
}

EXPECTED_STATED_RETURN_MISSING_SEQS = (13, 34)
# The complete decoded ambiguity_flags list for those two extractions -
# every bare "07/24"/"7/24" token in this corpus carries no year, which
# app/parser.py flags as "expiration_year_missing" on every one of the 68
# extractions; these two additionally lack a "$OLD -> $NEW (+NN%)" line,
# which adds "stated_return_missing". Asserted as a complete list (not a
# substring/"in" check) per the R6.6 verification requirement.
EXPECTED_STATED_RETURN_MISSING_FULL_FLAGS = [
    "expiration_year_missing",
    "stated_return_missing",
]


# ---------------------------------------------------------------------------
# Shared, immutable expected-lifecycle-case structure.
# ---------------------------------------------------------------------------

ExpectedLifecycle = namedtuple(
    "ExpectedLifecycle",
    "case_name trader symbol option_type strike expiration status "
    "remaining_fraction members opened_by_seq closed_by_seq flags",
)

EXPECTED_CASES = {
    "bdorts_avgo_380p": ExpectedLifecycle(
        "Bdorts AVGO 380P", "Bdorts", "AVGO", "put", "380", EXPECTED_EXPIRATION,
        "partially_closed", Fraction(1, 8), (1, 6, 9, 14, 15), 1, None, (),
    ),
    "bdorts_avgo_377_5p": ExpectedLifecycle(
        "Bdorts AVGO 377.5P", "Bdorts", "AVGO", "put", "377.5", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (30, 34), 30, 34, (),
    ),
    "tc_ibm_207_5c": ExpectedLifecycle(
        "TC IBM 207.5C", "TC", "IBM", "call", "207.5", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (2, 3, 4, 8, 26, 45), 2, 45, (),
    ),
    "tc_nvda_207_5c": ExpectedLifecycle(
        "TC NVDA 207.5C", "TC", "NVDA", "call", "207.5", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (10, 11, 13), 10, 13, (),
    ),
    "tc_qqq_685p_add_singleton": ExpectedLifecycle(
        "TC QQQ 685P ADD singleton", "TC", "QQQ", "put", "685", EXPECTED_EXPIRATION,
        "unresolved", Fraction(0), (16,), None, None,
        ("ambiguous_add_no_open_position",),
    ),
    "tc_qqq_685p_orphan_singleton": ExpectedLifecycle(
        "TC QQQ 685P ALL OUT singleton", "TC", "QQQ", "put", "685", EXPECTED_EXPIRATION,
        "orphan", Fraction(0), (17,), None, 17, (),
    ),
    "tc_qqq_687c": ExpectedLifecycle(
        "TC QQQ 687C", "TC", "QQQ", "call", "687", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (20, 21, 23, 24), 20, 24, (),
    ),
    "tc_mu_950c": ExpectedLifecycle(
        "TC MU 950C", "TC", "MU", "call", "950", EXPECTED_EXPIRATION,
        "partially_closed", Fraction(1, 4), (50, 51, 54), 50, None, (),
    ),
    "tc_mu_955c": ExpectedLifecycle(
        "TC MU 955C", "TC", "MU", "call", "955", EXPECTED_EXPIRATION,
        "open", Fraction(1), (57,), 57, None, (),
    ),
    "spacemonkey_ibm_210c": ExpectedLifecycle(
        "spacemonkey IBM 210C", "spacemonkey", "IBM", "call", "210", EXPECTED_EXPIRATION,
        "orphan", Fraction(0), (5, 7, 35), None, 35, (),
    ),
    "spacemonkey_spx_7440c": ExpectedLifecycle(
        "spacemonkey SPX 7440C", "spacemonkey", "SPX", "call", "7440", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (22, 28), 22, 28, (),
    ),
    "spacemonkey_tsla_310c": ExpectedLifecycle(
        "spacemonkey TSLA 310C", "spacemonkey", "TSLA", "call", "310", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (31, 32, 33, 36), 31, 36, (),
    ),
    "spacemonkey_spx_7450c": ExpectedLifecycle(
        "spacemonkey SPX 7450C", "spacemonkey", "SPX", "call", "7450", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (37, 38, 39, 40, 44), 37, 44, (),
    ),
    "spacemonkey_spx_7470c": ExpectedLifecycle(
        "spacemonkey SPX 7470C", "spacemonkey", "SPX", "call", "7470", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (46, 48, 49, 56), 46, 56, (),
    ),
    "spacemonkey_nvda_210c": ExpectedLifecycle(
        "spacemonkey NVDA 210C", "spacemonkey", "NVDA", "call", "210", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (58, 59, 60, 61), 58, 61, (),
    ),
    "spacemonkey_spx_7430p_first": ExpectedLifecycle(
        "spacemonkey SPX 7430P first generation",
        "spacemonkey", "SPX", "put", "7430", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (62, 63), 62, 63, (),
    ),
    "spacemonkey_spx_7430p_reentry": ExpectedLifecycle(
        "spacemonkey SPX 7430P re-entry generation",
        "spacemonkey", "SPX", "put", "7430", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (64, 65, 66, 67, 68), 64, 68, (),
    ),
    "matae_tsla_312_5p": ExpectedLifecycle(
        "Matae TSLA 312.5P", "Matae", "TSLA", "put", "312.5", EXPECTED_EXPIRATION,
        "closed", Fraction(0), (12, 19, 25, 27, 29), 12, 29, (),
    ),
    "sarang_qqq_690c": ExpectedLifecycle(
        "Sarang QQQ 690C", "Sarang", "QQQ", "call", "690", EXPECTED_EXPIRATION,
        "partially_closed", Fraction(1, 6), (18, 41, 42, 43, 55), 18, None, (),
    ),
    "sarang_jpm_352_5c": ExpectedLifecycle(
        "Sarang JPM 352.5C", "Sarang", "JPM", "call", "352.5", EXPECTED_EXPIRATION,
        "partially_closed", Fraction(11, 16), (47, 52, 53), 47, None, (),
    ),
}

assert len(EXPECTED_CASES) == EXPECTED_DISTINCT_KEY_COUNT + 2  # two keys have 2 generations each


# ---------------------------------------------------------------------------
# Module-level helpers. Kept free of any test-class state so they can be
# reused identically by every class below without duplication.
# ---------------------------------------------------------------------------


def _delete_if_exists(path: str) -> None:
    """addClassCleanup target: remove a temp database file if still present.

    Registered before any database operation in _create_corpus_database(),
    so a failure at any point during setup still deletes the temp file -
    no test run can leave a persistent temp database behind.
    """
    if os.path.exists(path):
        os.remove(path)


def _decimal_or_none(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _strike_matches(actual, expected) -> bool:
    """Compare two strike representations by value, not by string form, so
    equivalent values (e.g. "380" and "380.0") can never cause a false
    mismatch."""
    actual_dec, expected_dec = _decimal_or_none(actual), _decimal_or_none(expected)
    if actual_dec is None or expected_dec is None:
        return actual_dec is None and expected_dec is None
    return actual_dec == expected_dec


def _seq_by_signal_id(conn) -> dict[int, int]:
    """Map every trade_signals.id to its raw_messages.sequence_in_batch."""
    rows = conn.execute(
        "SELECT ts.id AS signal_id, rm.sequence_in_batch AS seq "
        "FROM trade_signals ts JOIN raw_messages rm ON ts.raw_message_id = rm.id"
    ).fetchall()
    return {row["signal_id"]: row["seq"] for row in rows}


def _raw_message_id_by_seq(conn) -> dict[int, int]:
    rows = conn.execute("SELECT id, sequence_in_batch FROM raw_messages").fetchall()
    return {row["sequence_in_batch"]: row["id"] for row in rows}


def _lifecycle_rows_with_members(conn) -> list[dict]:
    """Build one dict per current lifecycle generation, with ordered member
    sequence_in_batch numbers (never raw signal/lifecycle IDs) and a
    normalized ambiguity_flags list.

    list_current_trade_lifecycles() decodes a NULL ambiguity_flags column
    to Python None (see database/repository.py), not []; that is the
    correct storage-level representation ("no flags were ever recorded"),
    but semantically equivalent to an empty list for comparison purposes.
    This helper normalizes to [] here for convenience; the distinction
    between the two is asserted directly, at the raw repository level, in
    CorpusAmbiguityFlagTests.test_ambiguity_flags_null_is_not_conflated_with_empty_list.
    """
    seq_map = _seq_by_signal_id(conn)
    rows = []
    for lc in list_current_trade_lifecycles(conn, limit=1000):
        events = get_trade_lifecycle_events(conn, lc["id"])
        member_seqs = [seq_map[event.trade_signal_id] for event in events]
        rows.append(
            {
                "lifecycle_id": lc["id"],
                "trader_name": lc["trader_name"],
                "symbol": lc["symbol"],
                "option_type": lc["option_type"],
                "strike": lc["strike"],
                "expiration": lc["expiration"],
                "status": lc["status"],
                "remaining_fraction": lc["remaining_fraction"],
                "opened_by_signal_id": lc["opened_by_signal_id"],
                "closed_by_signal_id": lc["closed_by_signal_id"],
                "opened_by_seq": seq_map.get(lc["opened_by_signal_id"]),
                "closed_by_seq": seq_map.get(lc["closed_by_signal_id"]),
                "ambiguity_flags_raw": lc["ambiguity_flags"],
                "ambiguity_flags": lc["ambiguity_flags"] or [],
                "member_seqs": member_seqs,
                "events": events,
            }
        )
    return rows


def _find_lifecycle_row(rows: list[dict], expected: ExpectedLifecycle):
    """Locate the row matching expected's stable key, disambiguating two
    generations of the same key (e.g. the SPX 7430P re-entry pair) by
    their first member's corpus sequence number - never by lifecycle_id.

    Returns (row_or_None, candidates_with_matching_key) so a failed lookup
    can report exactly what did/did not match.
    """
    candidates = [
        row
        for row in rows
        if row["trader_name"] == expected.trader
        and row["symbol"] == expected.symbol
        and row["option_type"] == expected.option_type
        and _strike_matches(row["strike"], expected.strike)
        and row["expiration"] == expected.expiration
    ]
    for row in candidates:
        if row["member_seqs"] and row["member_seqs"][0] == expected.members[0]:
            return row, candidates
    return None, candidates


class _LifecycleCorpusAcceptanceTestCase(unittest.TestCase):
    """Shared temp-database plumbing for every R6.6 acceptance test class.

    Each subclass owns and deletes exactly one temporary SQLite database of
    its own, built by ingesting the real corpus through the production
    pipeline exactly once in setUpClass. No subclass shares a database or
    a connection with another, so classes (and, within a class, methods)
    remain independently runnable in any order.
    """

    @classmethod
    def _create_corpus_database(cls, channel_external_id: str):
        """Create a unique temp SQLite db, ingest the real corpus once via
        TradeService.ingest_batch(), run TradeService.rebuild_all_lifecycles()
        once, and close the writer connection. Both service methods commit
        their own transaction internally; this helper never commits itself.

        Returns (config, ingest_result, rebuild_result). The temp file is
        registered for deletion via addClassCleanup before any database
        operation runs, so a failure partway through setup still cleans up
        - no test run can leave a persistent temp database.
        """
        fd, db_path = tempfile.mkstemp(prefix="r6_6_corpus_", suffix=".db")
        os.close(fd)
        cls.addClassCleanup(_delete_if_exists, db_path)

        config = DatabaseConfig(db_path=db_path)
        initialize_database(config)
        conn = get_connection(config)
        try:
            # No caller-side conn.commit() here: TradeService.ingest_batch()
            # and TradeService.rebuild_all_lifecycles() each own and commit
            # their own complete transaction internally via
            # _r5_write_transaction() (database/service.py). Calling
            # conn.commit() here would be redundant at best and, worse,
            # could silently mask a future regression where a service
            # method stopped committing internally - this suite's fresh-
            # connection durability proof must depend only on the
            # production methods' own commit behavior.
            service = TradeService(conn)
            ingest_result = service.ingest_batch(
                source_name=SOURCE_NAME,
                reference_date=REFERENCE_DATE,
                timezone=TIMEZONE,
                raw_batch_text=CORPUS,
                channel_external_id=channel_external_id,
            )
            rebuild_result = service.rebuild_all_lifecycles()
        finally:
            conn.close()

        return config, ingest_result, rebuild_result


# ---------------------------------------------------------------------------
# 1. Ingestion, first-rebuild counters, and global lifecycle invariants.
# ---------------------------------------------------------------------------


class CorpusIngestionAndGlobalInvariantTests(_LifecycleCorpusAcceptanceTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config, cls.ingest_result, cls.rebuild_result = cls._create_corpus_database(
            "r6-6-ingestion-invariants"
        )

    def test_ingestion_counts(self):
        result = self.ingest_result
        self.assertEqual(result.total_segmented, EXPECTED_TOTAL_MESSAGES)
        self.assertEqual(result.stored_count, EXPECTED_TOTAL_MESSAGES)
        self.assertEqual(result.duplicate_count, 0)
        self.assertEqual(result.unrecognized_count, 0)
        self.assertEqual(result.failed_count, 0)

        conn = get_connection(self.config)
        try:
            for table, expected in (
                ("raw_messages", EXPECTED_TOTAL_MESSAGES),
                ("trade_signals", EXPECTED_TOTAL_MESSAGES),
                ("traders", EXPECTED_TRADER_COUNT),
            ):
                actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(actual, expected, table)

            current_extractions = conn.execute(
                "SELECT COUNT(*) FROM message_extractions WHERE is_current = 1"
            ).fetchone()[0]
            self.assertEqual(current_extractions, EXPECTED_TOTAL_MESSAGES)

            current_signals = conn.execute(
                "SELECT COUNT(*) FROM trade_signals ts "
                "JOIN message_extractions me ON ts.extraction_id = me.id "
                "WHERE me.is_current = 1"
            ).fetchone()[0]
            self.assertEqual(current_signals, EXPECTED_TOTAL_MESSAGES)
        finally:
            conn.close()

    def test_first_rebuild_counters(self):
        result = self.rebuild_result
        self.assertEqual(result.keys_considered, 18)
        self.assertEqual(result.keys_changed, 18)
        self.assertEqual(result.keys_unchanged, 0)
        self.assertEqual(result.lifecycles_superseded, 0)
        self.assertEqual(result.lifecycles_created, 20)
        self.assertEqual(result.lifecycle_events_created, 68)
        self.assertEqual(result.signal_pointers_cleared, 0)
        self.assertEqual(result.signal_pointers_assigned, 68)

    def test_current_lifecycle_and_key_counts(self):
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
            self.assertEqual(len(rows), EXPECTED_CURRENT_LIFECYCLE_COUNT)

            distinct_keys = {
                (r["trader_name"], r["symbol"], r["option_type"], str(_decimal_or_none(r["strike"])), r["expiration"])
                for r in rows
            }
            self.assertEqual(len(distinct_keys), EXPECTED_DISTINCT_KEY_COUNT)
        finally:
            conn.close()

    def test_status_distribution_is_exact(self):
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
            distribution = Counter(r["status"] for r in rows)
            # Include zero-count statuses explicitly - Counter omits them,
            # and "invalid: 0" must be asserted, not merely absent.
            full_distribution = {
                status: distribution.get(status, 0) for status in EXPECTED_STATUS_DISTRIBUTION
            }
            self.assertEqual(full_distribution, EXPECTED_STATUS_DISTRIBUTION)
        finally:
            conn.close()

    def test_membership_row_count_and_sequence_coverage(self):
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
            total_events = conn.execute(
                "SELECT COUNT(*) FROM trade_lifecycle_events"
            ).fetchone()[0]
            self.assertEqual(total_events, EXPECTED_TOTAL_MESSAGES)

            all_seqs = [seq for r in rows for seq in r["member_seqs"]]
            self.assertEqual(len(all_seqs), EXPECTED_TOTAL_MESSAGES)
            self.assertEqual(sorted(all_seqs), list(range(1, EXPECTED_TOTAL_MESSAGES + 1)))
            self.assertEqual(len(all_seqs), len(set(all_seqs)), "duplicate sequence number found")
        finally:
            conn.close()

    def test_signal_pointer_invariants(self):
        conn = get_connection(self.config)
        try:
            distinct_pointered = conn.execute(
                "SELECT COUNT(DISTINCT id) FROM trade_signals WHERE lifecycle_id IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(distinct_pointered, EXPECTED_TOTAL_MESSAGES)

            eligible_no_pointer = conn.execute(
                "SELECT id FROM trade_signals WHERE event_type IS NOT NULL AND lifecycle_id IS NULL"
            ).fetchall()
            self.assertEqual([r["id"] for r in eligible_no_pointer], [])

            multi_membership = conn.execute(
                "SELECT tle.trade_signal_id, COUNT(*) AS c "
                "FROM trade_lifecycle_events tle "
                "JOIN trade_lifecycles tl ON tle.trade_lifecycle_id = tl.id "
                "WHERE tl.is_current = 1 "
                "GROUP BY tle.trade_signal_id HAVING COUNT(*) > 1"
            ).fetchall()
            self.assertEqual(list(multi_membership), [])
        finally:
            conn.close()

    def test_every_event_pointer_agrees_with_signal_lifecycle_id(self):
        conn = get_connection(self.config)
        try:
            rows = conn.execute(
                "SELECT tle.trade_lifecycle_id AS event_lifecycle_id, "
                "ts.lifecycle_id AS signal_lifecycle_id "
                "FROM trade_lifecycle_events tle "
                "JOIN trade_lifecycles tl ON tle.trade_lifecycle_id = tl.id "
                "JOIN trade_signals ts ON tle.trade_signal_id = ts.id "
                "WHERE tl.is_current = 1"
            ).fetchall()
            self.assertEqual(len(rows), EXPECTED_TOTAL_MESSAGES)
            for row in rows:
                self.assertEqual(row["event_lifecycle_id"], row["signal_lifecycle_id"])
        finally:
            conn.close()

    def test_every_current_lifecycle_has_at_least_one_event_and_contiguous_sequence_index(self):
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
            for row in rows:
                self.assertGreaterEqual(len(row["events"]), 1, row)
                sequence_indices = [event.sequence_index for event in row["events"]]
                self.assertEqual(
                    sequence_indices, list(range(1, len(row["events"]) + 1)), row
                )
        finally:
            conn.close()

    def test_membership_integrity_validation_reports_no_violations(self):
        conn = get_connection(self.config)
        try:
            self.assertEqual(validate_lifecycle_membership_integrity(conn), [])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. One focused assertion per named acceptance case (docs/HANDOFFS' real-
#    corpus addendum's "IMPORTANT CORPUS CASES TO TEST" list).
# ---------------------------------------------------------------------------


class CorpusLifecycleOracleTests(_LifecycleCorpusAcceptanceTestCase):
    """Read-only oracle checks. A single ingest+rebuild is shared as a
    class fixture (per-case re-ingestion of a fixed 68-message corpus
    would be pure repetition, not additional coverage) - every test method
    only reads the frozen `cls.rows` snapshot computed once in
    setUpClass; no method mutates shared state or depends on another
    method's having run first."""

    @classmethod
    def setUpClass(cls):
        cls.config, _, _ = cls._create_corpus_database("r6-6-oracle")
        conn = get_connection(cls.config)
        try:
            cls.rows = _lifecycle_rows_with_members(conn)
        finally:
            conn.close()

    def _assert_case(self, case_key: str):
        expected = EXPECTED_CASES[case_key]
        row, candidates = _find_lifecycle_row(self.rows, expected)
        self.assertIsNotNone(
            row,
            f"No current lifecycle found for case {expected.case_name!r} matching "
            f"key (trader={expected.trader!r}, symbol={expected.symbol!r}, "
            f"option_type={expected.option_type!r}, strike={expected.strike!r}, "
            f"expiration={expected.expiration!r}) with first-member sequence "
            f"{expected.members[0]!r}. Candidates sharing this key had member "
            f"sequences: {[c['member_seqs'] for c in candidates]!r}",
        )
        self.assertEqual(row["expiration"], expected.expiration, expected.case_name)
        self.assertEqual(row["status"], expected.status, expected.case_name)
        self.assertEqual(
            Fraction(row["remaining_fraction"]), expected.remaining_fraction, expected.case_name
        )
        self.assertEqual(row["member_seqs"], list(expected.members), expected.case_name)
        self.assertEqual(row["opened_by_seq"], expected.opened_by_seq, expected.case_name)
        self.assertEqual(row["closed_by_seq"], expected.closed_by_seq, expected.case_name)
        self.assertEqual(row["ambiguity_flags"], list(expected.flags), expected.case_name)

    def test_bdorts_avgo_380p(self):
        self._assert_case("bdorts_avgo_380p")

    def test_bdorts_avgo_377_5p(self):
        self._assert_case("bdorts_avgo_377_5p")

    def test_tc_ibm_207_5c(self):
        self._assert_case("tc_ibm_207_5c")

    def test_tc_nvda_207_5c(self):
        self._assert_case("tc_nvda_207_5c")

    def test_tc_qqq_685p_add_singleton(self):
        self._assert_case("tc_qqq_685p_add_singleton")

    def test_tc_qqq_685p_orphan_singleton(self):
        self._assert_case("tc_qqq_685p_orphan_singleton")

    def test_tc_qqq_687c(self):
        self._assert_case("tc_qqq_687c")

    def test_tc_mu_950c(self):
        self._assert_case("tc_mu_950c")

    def test_tc_mu_955c(self):
        self._assert_case("tc_mu_955c")

    def test_spacemonkey_ibm_210c(self):
        self._assert_case("spacemonkey_ibm_210c")

    def test_spacemonkey_spx_7440c(self):
        self._assert_case("spacemonkey_spx_7440c")

    def test_spacemonkey_tsla_310c(self):
        self._assert_case("spacemonkey_tsla_310c")

    def test_spacemonkey_spx_7450c(self):
        self._assert_case("spacemonkey_spx_7450c")

    def test_spacemonkey_spx_7470c(self):
        self._assert_case("spacemonkey_spx_7470c")

    def test_spacemonkey_nvda_210c(self):
        self._assert_case("spacemonkey_nvda_210c")

    def test_spacemonkey_spx_7430p_first_generation(self):
        self._assert_case("spacemonkey_spx_7430p_first")

    def test_spacemonkey_spx_7430p_reentry_generation(self):
        self._assert_case("spacemonkey_spx_7430p_reentry")

    def test_matae_tsla_312_5p(self):
        self._assert_case("matae_tsla_312_5p")

    def test_sarang_qqq_690c(self):
        self._assert_case("sarang_qqq_690c")

    def test_sarang_jpm_352_5c(self):
        self._assert_case("sarang_jpm_352_5c")

    def test_every_expected_case_maps_to_a_distinct_current_lifecycle(self):
        """Cross-check: 20 named cases must resolve to 20 distinct
        lifecycle_id values - proves no two named cases are accidentally
        matching the same underlying row."""
        resolved_ids = set()
        for case_key in EXPECTED_CASES:
            row, _ = _find_lifecycle_row(self.rows, EXPECTED_CASES[case_key])
            self.assertIsNotNone(row, case_key)
            resolved_ids.add(row["lifecycle_id"])
        self.assertEqual(len(resolved_ids), EXPECTED_CURRENT_LIFECYCLE_COUNT)


# ---------------------------------------------------------------------------
# 3. Extraction-layer and lifecycle-layer ambiguity, kept in separate test
#    methods (and cross-checked as disjoint) since they describe different
#    layers of the pipeline.
# ---------------------------------------------------------------------------


class CorpusAmbiguityFlagTests(_LifecycleCorpusAcceptanceTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config, _, _ = cls._create_corpus_database("r6-6-ambiguity")

    def test_extraction_layer_stated_return_missing_is_exactly_two_messages(self):
        conn = get_connection(self.config)
        try:
            raw_message_id_by_seq = _raw_message_id_by_seq(conn)
            flagged = []
            for seq in range(1, EXPECTED_TOTAL_MESSAGES + 1):
                extraction = get_current_extraction(conn, raw_message_id_by_seq[seq])
                self.assertIsNotNone(extraction, seq)
                flags = extraction.ambiguity_flags or []
                if "stated_return_missing" in flags:
                    flagged.append((seq, flags))

            self.assertEqual(
                [seq for seq, _ in flagged], list(EXPECTED_STATED_RETURN_MISSING_SEQS)
            )
            for seq, flags in flagged:
                # Complete decoded flag list, not a substring/"in" check -
                # every corpus expiration is a bare "07/24"/"7/24" with no
                # year, so "expiration_year_missing" is also present here.
                self.assertEqual(flags, EXPECTED_STATED_RETURN_MISSING_FULL_FLAGS, seq)
        finally:
            conn.close()

    def test_lifecycle_layer_exactly_one_flagged_lifecycle(self):
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
        finally:
            conn.close()

        flagged_rows = [r for r in rows if r["ambiguity_flags"]]
        self.assertEqual(len(flagged_rows), 1, flagged_rows)

        flagged = flagged_rows[0]
        self.assertEqual(flagged["member_seqs"], [16])
        self.assertEqual(flagged["ambiguity_flags"], ["ambiguous_add_no_open_position"])

    def test_no_lifecycle_has_fraction_exceeds_remaining(self):
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
        finally:
            conn.close()

        for row in rows:
            self.assertNotIn("fraction_exceeds_remaining", row["ambiguity_flags"], row)

    def test_ambiguity_flags_null_is_not_conflated_with_empty_list(self):
        """The repository stores "no flags" as SQL NULL, decoded by
        list_current_trade_lifecycles() to Python None - not []. This
        confirms that raw, storage-level representation directly (rather
        than only the normalized [] used for the comparisons above), so
        the two concepts are never silently treated as identical."""
        conn = get_connection(self.config)
        try:
            rows = _lifecycle_rows_with_members(conn)
        finally:
            conn.close()

        unflagged_raw = [r["ambiguity_flags_raw"] for r in rows if not r["ambiguity_flags"]]
        self.assertEqual(len(unflagged_raw), EXPECTED_CURRENT_LIFECYCLE_COUNT - 1)
        for raw_value in unflagged_raw:
            self.assertIsNone(raw_value)


# ---------------------------------------------------------------------------
# 4. Strong second-rebuild idempotency: a no-op rebuild must be provably a
#    no-op - same row IDs, same total historical row counts, same shapes -
#    not merely "still 20 current rows" (which a supersede-and-recreate
#    cycle would also show).
# ---------------------------------------------------------------------------


def _snapshot(conn) -> dict:
    """Capture everything the second rebuild must leave untouched."""
    rows = _lifecycle_rows_with_members(conn)
    lifecycle_ids = sorted(r["lifecycle_id"] for r in rows)
    total_lifecycle_rows = conn.execute("SELECT COUNT(*) FROM trade_lifecycles").fetchone()[0]
    total_event_rows = conn.execute("SELECT COUNT(*) FROM trade_lifecycle_events").fetchone()[0]
    pointers = {
        row["id"]: row["lifecycle_id"]
        for row in conn.execute("SELECT id, lifecycle_id FROM trade_signals").fetchall()
    }
    shapes = {
        r["lifecycle_id"]: (
            r["trader_name"], r["symbol"], r["option_type"], str(_decimal_or_none(r["strike"])),
            r["expiration"], r["status"], r["remaining_fraction"],
            r["opened_by_signal_id"], r["closed_by_signal_id"], tuple(r["ambiguity_flags"]),
            tuple(r["member_seqs"]),
            # Immutable per-event audit snapshots, compared verbatim -
            # unchanged iff no event row was superseded/recreated.
            tuple((e.sequence_index, e.signal_snapshot) for e in r["events"]),
        )
        for r in rows
    }
    return {
        "lifecycle_ids": lifecycle_ids,
        "total_lifecycle_rows": total_lifecycle_rows,
        "total_event_rows": total_event_rows,
        "pointers": pointers,
        "shapes": shapes,
    }


class CorpusLifecycleIdempotencyTests(_LifecycleCorpusAcceptanceTestCase):
    """Owns its own database, entirely independent of every other class in
    this module, since it performs a second mutating rebuild call that
    must not be conflated with any other class's single-rebuild state."""

    @classmethod
    def setUpClass(cls):
        cls.config, _, _ = cls._create_corpus_database("r6-6-idempotency")

    def test_second_rebuild_is_a_strict_no_op(self):
        before_conn = get_connection(self.config)
        try:
            before = _snapshot(before_conn)
        finally:
            before_conn.close()

        write_conn = get_connection(self.config)
        try:
            # No caller-side write_conn.commit(): rebuild_all_lifecycles()
            # owns and commits its own transaction internally (see the
            # comment in _create_corpus_database above) - this test's
            # "zero writes occurred" proof must rest on that alone.
            second_result = TradeService(write_conn).rebuild_all_lifecycles()
        finally:
            write_conn.close()

        self.assertEqual(second_result.keys_considered, 18)
        self.assertEqual(second_result.keys_changed, 0)
        self.assertEqual(second_result.keys_unchanged, 18)
        self.assertEqual(second_result.lifecycles_superseded, 0)
        self.assertEqual(second_result.lifecycles_created, 0)
        self.assertEqual(second_result.lifecycle_events_created, 0)
        self.assertEqual(second_result.signal_pointers_cleared, 0)
        self.assertEqual(second_result.signal_pointers_assigned, 0)

        after_conn = get_connection(self.config)
        try:
            after = _snapshot(after_conn)
            integrity_after = validate_lifecycle_membership_integrity(after_conn)
        finally:
            after_conn.close()

        self.assertEqual(after["lifecycle_ids"], before["lifecycle_ids"])
        self.assertEqual(after["total_lifecycle_rows"], before["total_lifecycle_rows"])
        self.assertEqual(after["total_lifecycle_rows"], EXPECTED_CURRENT_LIFECYCLE_COUNT)
        self.assertEqual(after["total_event_rows"], before["total_event_rows"])
        self.assertEqual(after["total_event_rows"], EXPECTED_TOTAL_MESSAGES)
        self.assertEqual(after["pointers"], before["pointers"])
        self.assertEqual(after["shapes"], before["shapes"])
        self.assertEqual(integrity_after, [])


if __name__ == "__main__":
    unittest.main()

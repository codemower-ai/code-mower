"""Strict raw transports, trusted grammar, cumulative replay, and rendering."""
from dataclasses import FrozenInstanceError
from itertools import chain as concatenate
import json
import unittest

from code_mower.builder_lineage import (
    Authorities, Chain, ContractError, History, Identity, LINEAGE_MARKER, LINEAGE_SCHEMA,
    admit, parse_markers, render, resolve,
)
from minimal_lineage_fixtures import (
    AUTHORITY, BRANCH, VALID_COMMENTS, comment, episode, episodes, head, identity_mapping, target,
)


def marker(payload):
    return f"<!-- {LINEAGE_MARKER}: {json.dumps(payload)} -->"


def payload(items):
    return {"schema": LINEAGE_SCHEMA, "episodes": [e.to_mapping() for e in items]}


class HistoryTests(unittest.TestCase):
    def test_raw_history_is_explicit_and_pages_have_a_distinct_factory(self):
        invalid = (None, False, 0, "", {}, {"comments": []}, (), [None], [False],
                   [comment(), "wrong"], [[comment()]])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ContractError):
                History(value)
        self.assertEqual(History([]).comments, ())
        self.assertEqual(History.from_pages([]), History([]))
        self.assertEqual(History.from_pages([[], [comment()], []]), History([comment()]))
        for value in (None, False, "", {}, [comment()], [[], comment()], [[[comment()]]], [None]):
            with self.subTest(pages=value), self.assertRaises(ContractError):
                History.from_pages(value)
        with self.assertRaises(TypeError):
            History()

    def test_every_present_field_is_validated_even_for_untrusted_comments(self):
        records = [comment() | {"body": value} for value in (None, False, 7, {}, [])]
        for field in ("user", "author"):
            records += [{field: value} for value in (False, 7, "owner", [])]
            records += [{field: {"login": value}} for value in (None, False, 7, {}, [], "")]
        records += [{"user": {"login": AUTHORITY}, "author": "invalid"},
                    {"user": {"login": AUTHORITY}, "author": {"login": "outsider"}}]
        for record in records:
            with self.subTest(record=record), self.assertRaises(ContractError):
                History([comment(), record])
            with self.subTest(page_record=record), self.assertRaises(ContractError):
                History.from_pages([[comment()], [record]])

    def test_nullable_optional_fields_and_immutable_storage(self):
        history = History(VALID_COMMENTS)
        self.assertEqual(len(history.comments), len(VALID_COMMENTS))
        self.assertEqual(list(parse_markers(history, Authorities([AUTHORITY]))), [])
        raw = comment("original")
        snapshot = History([raw])
        raw["body"] = "changed"
        raw["user"]["login"] = "outsider"
        self.assertEqual(snapshot.comments[0].body, "original")
        self.assertEqual(snapshot.comments[0].account, AUTHORITY)
        with self.assertRaises(FrozenInstanceError):
            snapshot.comments = ()
        with self.assertRaises(FrozenInstanceError):
            snapshot.comments[0].body = "changed"

    def test_authority_is_explicit_accounts_and_never_a_callback_or_identity(self):
        raw = [f" {AUTHORITY.upper()} ", AUTHORITY]
        authorities = Authorities(raw)
        self.assertEqual(authorities.accounts, frozenset({AUTHORITY}))
        raw.append("outsider")
        self.assertNotIn("outsider", authorities.accounts)
        for value in (None, False, "owner", {"owner": True}, lambda _: True,
                      Identity(identity_mapping()), [None], [False]):
            with self.subTest(value=value), self.assertRaises(ContractError):
                Authorities(value)
        with self.assertRaises(FrozenInstanceError):
            authorities.accounts = frozenset()


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.authorities = Authorities([AUTHORITY])
        self.identity = Identity(identity_mapping())

    def parsed_chain(self, body, *, bound=None, account=AUTHORITY):
        return Chain.from_arrivals(target() if bound is None else bound,
                                   parse_markers(History([comment(body, account)]), self.authorities))

    def test_marker_presence_absence_and_trust_before_grammar(self):
        body = render(Chain.from_arrivals(target(), [episode()]))
        for account in ("outsider", "devin-bot[bot]"):
            for text in (body, LINEAGE_MARKER, f"<!-- {LINEAGE_MARKER}: broken"):
                self.assertEqual(self.parsed_chain(text, account=account).episodes, ())
        self.assertEqual(self.parsed_chain("ordinary comment").episodes, ())
        self.assertEqual(self.parsed_chain(body).episodes, (episode(),))
        empty_trust = parse_markers(History([comment(body)]), Authorities([]))
        self.assertEqual(Chain.from_arrivals(target(), empty_trust).episodes, ())
        for invalid in (None, [], False):
            with self.assertRaises(ContractError):
                parse_markers(invalid, self.authorities)
            with self.assertRaises(ContractError):
                parse_markers(History([]), invalid)

    def test_announced_marker_grammar_is_complete_unique_and_nonempty(self):
        good = render(Chain.from_arrivals(target(), [episode()]))
        duplicate_outer = f'<!-- {LINEAGE_MARKER}: {{"schema":"{LINEAGE_SCHEMA}","schema":"{LINEAGE_SCHEMA}","episodes":[]}} -->'
        duplicate_episode = good.replace('"sequence":1', '"sequence":1,"sequence":1')
        invalid = [f"<!-- {LINEAGE_MARKER}: {{", good[:-3], good + good,
                   good + LINEAGE_MARKER, good.replace(": {", " {", 1),
                   marker({"schema": LINEAGE_SCHEMA, "episodes": []}),
                   marker({"schema": LINEAGE_SCHEMA, "episodes": None}),
                   marker({"schema": LINEAGE_SCHEMA, "episodes": {}}),
                   marker({"schema": "other", "episodes": [episode().to_mapping()]}),
                   marker({"schema": LINEAGE_SCHEMA, "episodes": [None]}),
                   marker(payload([episode()]) | {"unknown": True}),
                   duplicate_outer, duplicate_episode,
                   f"<!-- {LINEAGE_MARKER}: NaN -->"]
        for text in invalid:
            with self.subTest(text=text[:100]), self.assertRaises(ContractError):
                self.parsed_chain(text)

    def test_marker_name_in_prose_inline_code_and_fences_is_not_control_data(self):
        examples = (
            LINEAGE_MARKER,
            f"The reserved name is {LINEAGE_MARKER}; do not copy it into a control.",
            f"Use `{LINEAGE_MARKER}` only when documenting the contract.",
            f"`<!-- {LINEAGE_MARKER}: broken -->`",
            f"```json\n<!-- {LINEAGE_MARKER}: broken -->\n```",
            f"```json\n```not-a-close\n<!-- {LINEAGE_MARKER}: broken -->\n```",
            f"~~~~\n<!-- {LINEAGE_MARKER}: broken -->\n~~~~",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(self.parsed_chain(text).episodes, ())

    def test_marker_target_binding_and_mixed_target_chain(self):
        good = render(Chain.from_arrivals(target(), [episode()]))
        for bound in (target(repo="owner/other-repo"), target(pr_number=43), target(branch=BRANCH.lower())):
            with self.subTest(bound=bound), self.assertRaises(ContractError):
                self.parsed_chain(good, bound=bound)
        items = episodes(2)
        items[1] = type(items[1]).from_mapping(items[1].to_mapping() | {"repo": "owner/other-repo"})
        with self.assertRaises(ContractError):
            self.parsed_chain(marker(payload(items)))

    def test_528_public_plus_32_private_arrivals_share_one_budget(self):
        items = episodes(32)
        comments = [comment(render(Chain.from_arrivals(target(head_sha=head(i)), items[:i])))
                    for i in range(1, 33)]
        history = History(comments)
        combined = concatenate(parse_markers(history, self.authorities), items)
        chain = Chain.from_arrivals(target(head_sha=head(32)), combined)
        self.assertEqual(chain.raw_arrival_count, 560)
        self.assertEqual(chain.episodes, tuple(items))
        decision = resolve(chain, self.identity, "devin-bot[bot]", ["builder:codex"])
        self.assertTrue(admit(decision, "claude"))
        with self.assertRaisesRegex(ContractError, "arrival budget"):
            Chain.from_arrivals(target(head_sha=head(32)),
                                concatenate(parse_markers(history, self.authorities), items, [items[0]]))
        conflicting = episode(resulting_head=head(99))
        with self.assertRaisesRegex(ContractError, "conflicting duplicate"):
            Chain.from_arrivals(target(head_sha=head(32)),
                                concatenate(parse_markers(history, self.authorities), [conflicting]))

    def test_current_head_is_judged_after_combining_public_and_private(self):
        items = episodes(2)
        public = History([comment(render(Chain.from_arrivals(target(head_sha=head(1)), items[:1])))])
        bound = target(head_sha=head(2))
        stale = Chain.from_arrivals(bound, parse_markers(public, self.authorities))
        self.assertEqual(resolve(stale, self.identity, "", []).status, "waiting")
        combined = Chain.from_arrivals(bound, concatenate(parse_markers(public, self.authorities), items[1:]))
        self.assertEqual(resolve(combined, self.identity, "", []).status, "ready")

    def test_render_roundtrip_one_and_32_episodes(self):
        for count in (1, 32):
            bound = target(head_sha=head(count))
            original = Chain.from_arrivals(bound, episodes(count))
            body = render(original)
            for field in ("user", "author"):
                restored = Chain.from_arrivals(bound, parse_markers(
                    History([comment(body, f" {AUTHORITY.upper()} ", field)]), self.authorities))
                self.assertEqual(restored, original)
                self.assertEqual(render(restored), body)

    def test_arbitrary_empty_oversized_malformed_or_mixed_input_cannot_render(self):
        for value in (None, False, {}, [], (), [episode()], episodes(33),
                      [episode(), episode(repo="owner/other-repo")], Chain.from_arrivals(target(), [])):
            with self.subTest(value=type(value)), self.assertRaises(ContractError):
                render(value)
        with self.assertRaises(ContractError):
            Chain.from_arrivals(target(), [{"source_lane": "devin"}])
        with self.assertRaises(ContractError):
            Chain.from_arrivals(target(), episodes(33))


if __name__ == "__main__":
    unittest.main()

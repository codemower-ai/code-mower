"""Strict marker grammar and raw comment/page validation.

Two opposite answers keep getting collapsed into one. "There is no published
lineage here" admits an independent reviewer on the ordinary single-builder
story; "this could not be read" has to stop. Every case below is about keeping
them apart -- in the marker grammar, in the raw comment record, in the page
array, and in the choice of which history to read at all.

Trust is decided before parsing throughout, so an outsider can neither assert a
takeover into existence by posting a marker nor force a refusal by posting a
broken one.
"""

from __future__ import annotations

import json
import unittest

from code_mower.builder_lineage import (
    LINEAGE_MARKER,
    MAX_EPISODE_ARRIVALS,
    MAX_EPISODES,
    MAX_MARKER_BODY_CHARS,
    NO_LINEAGE,
    OMITTED,
    LineageError,
    bounded_arrivals,
    comment_author_login,
    comment_body,
    episodes_from_comment_body,
    flatten_comment_pages,
    lineage_comment_marker,
    lineage_context,
    merge_episodes,
    published_episodes,
    require_comment_list,
    require_comment_record,
    resolve_lineage,
    resolve_lineage_context,
    select_comment_history,
)
from code_mower.lineage_identity import marker_author_trust

from lineage_contract_fixtures import (
    AUTHORITY,
    BRANCH,
    IDENTITY,
    INVALID_COMMENT_RESPONSES,
    MALFORMED_COMMENT_RECORDS,
    OUTSIDER,
    PR,
    REPO,
    TAKEN,
    VALID_COMMENT_RECORDS,
    chain,
    comment,
    published,
    takeover,
    variant,
)


TRUST = marker_author_trust((AUTHORITY,))


def valid_marker() -> str:
    return lineage_comment_marker((takeover(),))


class MarkerGrammarTests(unittest.TestCase):
    """A broken marker is unreadable evidence, never absent evidence.

    The payload pattern matches only a complete, object-shaped, terminated
    marker. Looking for evidence with it alone means an unterminated or
    non-object marker is not read as broken -- it is not seen at all, and a
    trusted comment announcing lineage reports none.
    """

    def unreadable(self, body: str) -> None:
        with self.assertRaises(LineageError):
            episodes_from_comment_body(body)
        with self.assertRaises(LineageError):
            published_episodes([comment(body=body)], trusted_author=TRUST)

    def test_a_well_formed_marker_round_trips_metadata_only(self):
        marker = valid_marker()
        self.assertNotIn("/Users", marker)
        self.assertNotIn("session", marker)
        self.assertEqual(
            episodes_from_comment_body("context\n" + marker + "\nmore"), (takeover(),)
        )

    def test_an_unterminated_marker_is_unreadable_not_absent(self):
        self.unreadable(valid_marker().replace("-->", ""))

    def test_a_non_object_payload_is_unreadable(self):
        self.unreadable(f"<!-- {LINEAGE_MARKER} [1, 2, 3] -->")

    def test_malformed_json_is_unreadable(self):
        self.unreadable(f'<!-- {LINEAGE_MARKER} {{"schema": -->')

    def test_an_empty_payload_is_unreadable(self):
        self.unreadable(f"<!-- {LINEAGE_MARKER} -->")

    def test_two_markers_on_one_comment_are_ambiguous(self):
        self.unreadable(f"{valid_marker()}\n\n{valid_marker()}")

    def test_a_valid_marker_beside_a_broken_one_is_still_ambiguous(self):
        self.unreadable(f"{valid_marker()}\n\n<!-- {LINEAGE_MARKER} [] -->")

    def test_a_marker_past_the_body_bound_is_unreadable_not_absent(self):
        self.unreadable("x" * MAX_MARKER_BODY_CHARS + "\n" + valid_marker())

    def test_an_announced_but_empty_chain_is_unreadable(self):
        body = json.dumps({"schema": "code_mower.builderLineage.v1", "episodes": []})
        self.unreadable(f"<!-- {LINEAGE_MARKER} {body} -->")

    def test_a_chain_past_the_lineage_bound_is_unreadable(self):
        payload = {
            "schema": "code_mower.builderLineage.v1",
            "episodes": [takeover().as_dict()] * (MAX_EPISODES + 1),
        }
        self.unreadable(f"<!-- {LINEAGE_MARKER} {json.dumps(payload)} -->")

    def test_an_unsupported_schema_is_unreadable(self):
        body = json.dumps({"schema": "something.else", "episodes": []})
        self.unreadable(f"<!-- {LINEAGE_MARKER} {body} -->")

    def _duplicated(self, key: str, extra: str) -> str:
        marker = valid_marker()
        head, _, tail = marker.partition("{")
        return f"{head}{{{json.dumps(key)}:{extra},{tail}"

    def test_a_duplicate_top_level_key_is_unreadable(self):
        for key, extra in (
            ("schema", '"code_mower.builderLineage.v1"'),
            ("schema", '"something.else"'),
            ("episodes", "[]"),
        ):
            with self.subTest(key=key, extra=extra):
                self.unreadable(self._duplicated(key, extra))

    def test_a_duplicate_key_inside_an_episode_is_unreadable(self):
        marker = valid_marker()
        forged = marker.replace(
            '"resulting_head"', '"resulting_head":"' + "c" * 40 + '","resulting_head"', 1
        )
        self.assertNotEqual(forged, marker)
        self.unreadable(forged)

    def test_a_duplicate_nested_identity_key_is_unreadable(self):
        marker = valid_marker()
        forged = marker.replace(
            '"destination_lane"', '"destination_lane":"claude","destination_lane"', 1
        )
        self.assertNotEqual(forged, marker)
        self.unreadable(forged)

    def test_a_body_without_a_marker_yields_nothing(self):
        self.assertEqual(episodes_from_comment_body("Codex took this over."), ())
        self.assertEqual(episodes_from_comment_body(""), ())

    def test_a_genuinely_empty_comment_history_stays_ordinary(self):
        self.assertEqual(published_episodes([], trusted_author=TRUST), ())
        self.assertEqual(require_comment_list([], what="history"), ())


class MarkerTrustTests(unittest.TestCase):
    """Trust is decided before parsing, and only from configured authorities."""

    def test_an_untrusted_broken_marker_is_not_authoritative(self):
        body = valid_marker().replace("-->", "")
        untrusted = [comment(body=body, author=OUTSIDER)]
        self.assertEqual(published_episodes(untrusted, trusted_author=TRUST), ())

    def test_an_untrusted_duplicate_key_marker_is_not_authoritative(self):
        marker = valid_marker()
        head, _, tail = marker.partition("{")
        ambiguous = f'{head}{{"episodes":[],{tail}'
        self.assertEqual(
            published_episodes(
                [comment(body=ambiguous, author=OUTSIDER)], trusted_author=TRUST
            ),
            (),
        )

    def test_an_untrusted_valid_marker_asserts_nothing(self):
        self.assertEqual(
            published_episodes([published((takeover(),), author=OUTSIDER)], trusted_author=TRUST),
            (),
        )

    def test_unrelated_trusted_comments_stay_non_authoritative(self):
        self.assertEqual(
            published_episodes(
                [comment(body="Looks good to me. Shipping after CI.")],
                trusted_author=TRUST,
            ),
            (),
        )

    def test_a_mixed_history_stops_on_the_broken_comment(self):
        history = [
            published((takeover(),)),
            comment(body=valid_marker().replace("-->", "")),
        ]
        with self.assertRaises(LineageError):
            published_episodes(history, trusted_author=TRUST)

    def test_an_unconfigured_deployment_trusts_nobody(self):
        nobody = marker_author_trust(())
        self.assertFalse(nobody(AUTHORITY))
        self.assertEqual(
            published_episodes([published((takeover(),))], trusted_author=nobody), ()
        )

    def test_authority_matching_ignores_case_and_a_leading_at_sign(self):
        trusted = marker_author_trust(("@CodeMower-AI", " "))
        self.assertTrue(trusted("codemower-ai"))
        self.assertTrue(trusted("@codemower-ai"))
        self.assertFalse(trusted(OUTSIDER))

    def test_the_gh_transport_author_field_is_trusted_the_same_way(self):
        """A marker published through `gh --json comments` names `author`."""

        history = [published((takeover(),), field="author")]
        self.assertEqual(published_episodes(history, trusted_author=TRUST), (takeover(),))
        self.assertEqual(comment_author_login(history[0]), AUTHORITY)

    def test_the_full_cumulative_publication_is_read_whole(self):
        links = chain(MAX_EPISODES)
        history = [
            published(links[:length]) for length in range(1, len(links) + 1)
        ]
        arrivals = published_episodes(history, trusted_author=TRUST)
        self.assertEqual(len(arrivals), MAX_EPISODES * (MAX_EPISODES + 1) // 2)
        self.assertEqual(len(arrivals), 528)


class RawRecordValidationTests(unittest.TestCase):
    """Present-and-unreadable is rejected before anything normalizes it."""

    def test_a_malformed_present_field_is_rejected(self):
        for record in MALFORMED_COMMENT_RECORDS:
            with self.subTest(record=record):
                with self.assertRaises(LineageError):
                    require_comment_record(record, what="history")
                with self.assertRaises(LineageError):
                    require_comment_list([record], what="history")
                with self.assertRaises(LineageError):
                    published_episodes([record], trusted_author=TRUST)

    def test_githubs_own_schema_keeps_working(self):
        for record in VALID_COMMENT_RECORDS:
            with self.subTest(record=record):
                require_comment_record(record, what="history")
        self.assertEqual(
            len(require_comment_list(list(VALID_COMMENT_RECORDS), what="history")),
            len(VALID_COMMENT_RECORDS),
        )
        self.assertEqual(
            published_episodes(list(VALID_COMMENT_RECORDS), trusted_author=TRUST), ()
        )

    def test_a_successful_but_invalid_read_is_not_an_empty_history(self):
        for response in INVALID_COMMENT_RESPONSES:
            with self.subTest(response=response):
                with self.assertRaises(LineageError):
                    require_comment_list(response, what="history")

    def test_a_deleted_account_names_no_author_and_no_marker(self):
        self.assertEqual(comment_author_login({"user": None, "body": "hi"}), "")
        self.assertEqual(comment_author_login({"author": None}), "")
        self.assertEqual(comment_author_login({"user": {}}), "")

    def test_an_omitted_body_reads_as_empty_and_a_null_body_raises(self):
        self.assertEqual(comment_body({"user": {"login": AUTHORITY}}), "")
        with self.assertRaises(LineageError):
            comment_body({"user": {"login": AUTHORITY}, "body": None})


class SlurpedPageTests(unittest.TestCase):
    """`gh api --paginate --slurp` gives arrays of arrays, and only that."""

    def test_pages_of_comments_flatten_in_order(self):
        pages = [[comment(body="one")], [], [comment(body="two")]]
        flattened = flatten_comment_pages(pages)
        self.assertEqual([item["body"] for item in flattened], ["one", "two"])

    def test_a_genuinely_empty_page_set_stays_ordinary(self):
        self.assertEqual(flatten_comment_pages([]), [])
        self.assertEqual(flatten_comment_pages([[], []]), [])

    def test_a_response_that_is_not_a_page_array_is_refused(self):
        for payload in (None, False, {}, {"comments": []}, "text", 7):
            with self.subTest(payload=payload):
                with self.assertRaises(LineageError):
                    flatten_comment_pages(payload)

    def test_an_object_wrapper_is_never_accepted_as_a_one_comment_page(self):
        for page in ({}, {"comments": []}, comment(body="hi"), None, "text"):
            with self.subTest(page=page):
                with self.assertRaises(LineageError):
                    flatten_comment_pages([page])

    def test_a_malformed_record_on_a_later_page_still_refuses(self):
        first = [comment(body="ordinary") for _ in range(100)]
        for record in MALFORMED_COMMENT_RECORDS:
            with self.subTest(record=record):
                with self.assertRaises(LineageError):
                    flatten_comment_pages([first, [record]])

    def test_flattened_pages_are_copies_that_do_not_alias_the_input(self):
        page = [comment(body="one")]
        flattened = flatten_comment_pages([page])
        flattened[0]["body"] = "changed"
        self.assertEqual(page[0]["body"], "one")


class SelectedHistoryTests(unittest.TestCase):
    """An embedded comment list and a REST comment count are not the same thing."""

    def test_a_rest_numeric_count_is_metadata_not_a_history(self):
        for count in (0, 1, 42):
            with self.subTest(count=count):
                self.assertEqual(select_comment_history(embedded=count), ())

    def test_a_supported_embedded_list_is_still_read(self):
        history = [published((takeover(),), field="author")]
        self.assertEqual(len(select_comment_history(embedded=history)), 1)

    def test_an_omitted_history_field_is_ordinary(self):
        self.assertEqual(select_comment_history(), ())
        self.assertEqual(select_comment_history(embedded=OMITTED), ())

    def test_an_explicit_selection_wins_over_whatever_sits_beside_it(self):
        chosen = [published((takeover(),))]
        self.assertEqual(len(select_comment_history(selected=chosen, embedded=7)), 1)
        self.assertEqual(len(select_comment_history(selected=chosen, embedded=[])), 1)

    def test_an_explicitly_empty_selection_wins_over_an_embedded_takeover(self):
        self.assertEqual(
            select_comment_history(selected=[], embedded=[published((takeover(),))]), ()
        )

    def test_a_present_but_malformed_history_fails_closed(self):
        for embedded in (
            None,
            False,
            {},
            "comments",
            [[comment(body="hi")]],
            [comment(body="hi"), "text"],
            [{"user": {"login": 7}, "body": "hi"}],
            [{"user": {"login": AUTHORITY}, "body": None}],
        ):
            with self.subTest(embedded=embedded):
                with self.assertRaises(LineageError):
                    select_comment_history(embedded=embedded)

    def test_a_malformed_selection_fails_closed_beside_a_valid_count(self):
        with self.assertRaises(LineageError):
            select_comment_history(
                selected=[{"user": {"login": 7}, "body": "hi"}], embedded=3
            )

    def test_a_boolean_is_never_a_comment_count(self):
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(LineageError):
                    select_comment_history(embedded=value)


class BoundedArrivalTests(unittest.TestCase):
    """The raw budget is enforced while the input is walked, not afterwards."""

    def _counting(self, items):
        seen = []

        def walk():
            for item in items:
                seen.append(item)
                yield item

        return walk(), seen

    def test_exactly_the_documented_budget_is_accepted(self):
        links = chain(MAX_EPISODES)
        cumulative = [
            episode for length in range(1, len(links) + 1) for episode in links[:length]
        ]
        arrivals = cumulative + list(links)
        self.assertEqual(len(arrivals), MAX_EPISODE_ARRIVALS)
        self.assertEqual(len(merge_episodes(cumulative, links)), MAX_EPISODE_ARRIVALS)
        self.assertEqual(len(tuple(bounded_arrivals(arrivals))), MAX_EPISODE_ARRIVALS)

    def test_one_arrival_past_the_budget_refuses_even_when_it_repeats(self):
        links = chain(MAX_EPISODES)
        cumulative = [
            episode for length in range(1, len(links) + 1) for episode in links[:length]
        ]
        with self.assertRaises(LineageError):
            merge_episodes(cumulative + list(links), (links[-1],))

    def test_a_lazy_source_is_not_consumed_past_the_first_refused_arrival(self):
        links = chain(2)
        oversized = [links[0]] * (MAX_EPISODE_ARRIVALS + 50)
        walked, seen = self._counting(oversized)
        with self.assertRaises(LineageError):
            tuple(bounded_arrivals(walked))
        self.assertEqual(len(seen), MAX_EPISODE_ARRIVALS + 1)

    def test_independently_collapsed_inputs_cannot_reset_the_cap(self):
        """Collectors keep raw arrivals; only the resolver collapses them."""

        links = chain(4)
        merged = merge_episodes(links, links)
        self.assertEqual(len(merged), 8, "repeats are preserved as raw arrivals")
        resolved = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=merged,
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.episodes, 4)

    def test_a_malformed_member_is_the_contract_error_not_an_attribute_failure(self):
        for item in ("text", None, 7, {"schema": "nope"}, [takeover()]):
            with self.subTest(item=item):
                with self.assertRaises(LineageError):
                    merge_episodes((), (item,))

    def test_a_contradicting_episode_survives_to_be_refused(self):
        links = chain(3)
        forged = variant(links[1], destination_lane="claude", source_lane="claude")
        merged = merge_episodes(links, (forged,))
        self.assertEqual(len(merged), 4)
        self.assertEqual(
            resolve_lineage(
                repo=REPO,
                pr_number=PR,
                branch=BRANCH,
                head_sha=links[-1].resulting_head,
                episodes=merged,
            ).reason,
            "episode_duplicated",
        )

    def test_a_published_collection_is_bounded_too(self):
        links = chain(MAX_EPISODES)
        history = [published(links) for _ in range(MAX_EPISODES)]
        with self.assertRaises(LineageError):
            published_episodes(history, trusted_author=TRUST)


class RendererTests(unittest.TestCase):
    """Rendering is not a truncation operation."""

    def test_one_and_thirty_two_episodes_round_trip_unchanged(self):
        for length in (1, MAX_EPISODES):
            with self.subTest(length=length):
                links = chain(length)
                parsed = episodes_from_comment_body(lineage_comment_marker(links))
                self.assertEqual(parsed, links)

    def test_an_empty_chain_refuses_rather_than_publishing_nothing(self):
        with self.assertRaises(LineageError):
            lineage_comment_marker(())

    def test_a_thirty_third_entry_cannot_disappear(self):
        links = chain(MAX_EPISODES)
        contradictory = variant(
            links[-1], destination_lane="claude", source_lane="claude"
        )
        with self.assertRaises(LineageError):
            lineage_comment_marker(links + (contradictory,))

    def test_repeats_beyond_the_bound_render_without_losing_an_episode(self):
        """Slicing to the bound silently dropped the newest episodes."""

        links = chain(MAX_EPISODES)
        marker = lineage_comment_marker(links + links[:2])
        self.assertEqual(episodes_from_comment_body(marker), links)

    def test_malformed_unchained_and_conflicting_input_refuse(self):
        links = chain(3)
        for episodes in (
            ("not an episode",),
            ({"schema": "nope"},),
            (links[1],),
            links[:1] + links[2:],
            links + (variant(links[1], destination_lane="claude", source_lane="claude"),),
        ):
            with self.subTest(episodes=episodes):
                with self.assertRaises(LineageError):
                    lineage_comment_marker(episodes)

    def test_a_refused_chain_produces_no_marker_text(self):
        with self.assertRaises(LineageError) as raised:
            lineage_comment_marker(())
        self.assertNotIn(LINEAGE_MARKER, str(raised.exception))


class EvidenceAssemblyTests(unittest.TestCase):
    """Carried evidence binds to a complete target or refuses."""

    def test_a_partially_populated_target_refuses_rather_than_reading_as_absent(self):
        for missing in (
            {"head_sha": ""},
            {"repo": ""},
            {"pr_number": 0},
            {"branch": ""},
            {"head_sha": TAKEN[:39]},
        ):
            kwargs = dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN)
            kwargs.update(missing)
            with self.subTest(**missing):
                with self.assertRaises(LineageError):
                    lineage_context(**kwargs)

    def test_a_deliberately_absent_target_is_the_ordinary_no_context_case(self):
        self.assertIs(lineage_context(repo="", pr_number=0), NO_LINEAGE)
        self.assertIs(
            lineage_context(repo="", pr_number=0, comments=[], trusted_author=TRUST),
            NO_LINEAGE,
        )

    def test_evidence_with_no_target_to_bind_it_to_refuses(self):
        with self.assertRaises(LineageError):
            lineage_context(
                repo="",
                pr_number=0,
                comments=[published((takeover(),))],
                trusted_author=TRUST,
            )

    def test_a_context_carrying_episodes_is_never_treated_as_absent(self):
        carried = lineage_context(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            comments=[published((takeover(),))],
            trusted_author=TRUST,
        )
        self.assertNotEqual(carried, NO_LINEAGE)
        resolved = resolve_lineage_context(
            carried,
            identity=IDENTITY,
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
        )
        self.assertEqual(resolved.current_writer, "codex")

    def test_a_context_carries_only_trusted_published_evidence(self):
        context = lineage_context(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            comments=[
                comment(body="unrelated"),
                published((takeover(),)),
                published((takeover(),), author=OUTSIDER),
            ],
            trusted_author=TRUST,
        )
        self.assertEqual(context.episodes, (takeover(),))
        resolved = resolve_lineage_context(
            context,
            identity=IDENTITY,
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "codex")

    def test_an_unreadable_published_history_propagates_rather_than_emptying(self):
        with self.assertRaises(LineageError):
            lineage_context(
                repo=REPO,
                pr_number=PR,
                branch=BRANCH,
                head_sha=TAKEN,
                comments=[comment(body=valid_marker().replace("-->", ""))],
                trusted_author=TRUST,
            )

    def test_no_evidence_still_reaches_the_one_shared_decision(self):
        resolved = resolve_lineage_context(
            None, identity=IDENTITY, labels=["builder:claude"], author="a-human"
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "claude")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

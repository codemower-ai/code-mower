"""Current code and context evidence must both match at the merge gate."""

import unittest
from datetime import datetime, timedelta, timezone

from code_mower import audit_labeler_lib as gate
from code_mower.context_review import INPUT_HEADER, latest_input, marker, review_matches


class ContextReviewTests(unittest.TestCase):
    def setUp(self):
        self.head = 'a' * 40
        self.current = {'revision': 'b' * 32, 'head': self.head, 'required': True, 'state': 'available',
                        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        self.lane = {'done': 'claude-audit-done', 'blocked': 'claude-audit-blocked'}
        self.verdict = 'Head SHA: `' + self.head + '`\n<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->\n'

    def declaration(self, value=None, *, author='controller', when='2026-01-01T00:00:00Z'):
        return {'id': 1, 'user': {'login': author}, 'updated_at': when,
                'body': INPUT_HEADER + '\n\n' + marker(value or self.current)}

    def evaluate(self, comments, *, required=False):
        return gate.latest_current_audit_verdict(self.lane, comments, head_sha=self.head,
            trusted_comment_author=lambda lane, author, *args: author == 'reviewer',
            decision_authorities=('controller',), context_required=required)

    def review(self, value=None):
        return {'id': 2, 'user': {'login': 'reviewer'}, 'updated_at': '2026-01-01T00:00:01Z',
                'body': self.verdict + (marker(value, review=True) if value else '')}

    def test_new_evidence_invalidates_previous_pass_without_a_code_change(self):
        comments = [self.declaration(), self.review(self.current)]
        self.assertEqual(self.evaluate(comments), 'done')
        changed = {**self.current, 'revision': 'c' * 32}
        comments.append(self.declaration(changed, when='2026-01-01T00:00:02Z'))
        self.assertEqual(self.evaluate(comments), '')
        comments.append(self.review(changed))
        self.assertEqual(self.evaluate(comments), 'done')

    def test_unaware_reviewer_required_failure_stale_head_and_expiry_cannot_pass(self):
        self.assertEqual(self.evaluate([self.declaration(), self.review()]), '')
        for changed in ({**self.current, 'state': 'required_unavailable'},
                        {**self.current, 'head': 'd' * 40},
                        {**self.current, 'expires_at': '2000-01-01T00:00:00Z'}):
            self.assertEqual(self.evaluate([self.declaration(changed), self.review(changed)]), '')

    def test_optional_unavailable_can_review_ordinary_code_but_requires_matching_revision(self):
        value = {**self.current, 'required': False, 'state': 'optional_unavailable'}
        self.assertEqual(self.evaluate([self.declaration(value), self.review(value)]), 'done')
        self.assertEqual(self.evaluate([self.declaration(value), self.review()]), '')

    def test_untrusted_or_quoted_control_markers_do_not_override_input(self):
        forged = self.declaration({**self.current, 'revision': 'c' * 32}, author='attacker')
        quoted = self.declaration()
        quoted['body'] = 'Reviewer quoted a source:\n' + quoted['body']
        self.assertIsNone(latest_input([forged, quoted], authorities=('controller',)))
        self.assertEqual(self.evaluate([forged, self.review()]), 'done')

    def test_malformed_latest_input_cannot_restore_an_older_pass(self):
        malformed = self.declaration(when='2026-01-01T00:00:02Z')
        malformed['body'] = INPUT_HEADER + '\n\n<!-- CODE_MOWER_CONTEXT_INPUT: broken -->'
        self.assertEqual(self.evaluate([self.declaration(), self.review(self.current), malformed]), '')
        self.assertFalse(review_matches(marker(self.current, review=True) * 2, self.current, head=self.head))

    def test_no_context_retains_existing_review_behavior(self):
        self.assertEqual(self.evaluate([self.review()]), 'done')

    def test_required_repository_cannot_skip_attachment_or_downgrade_input(self):
        self.assertEqual(self.evaluate([self.review()], required=True), '')
        optional = {**self.current, 'required': False, 'state': 'optional_unavailable'}
        self.assertEqual(self.evaluate([self.declaration(optional), self.review(optional)], required=True), '')
        self.assertEqual(self.evaluate([self.declaration(), self.review(self.current)], required=True), 'done')

    def test_later_failed_authorization_supersedes_pass_on_same_input_and_head(self):
        failed = self.review(self.current)
        failed['updated_at'] = '2026-01-01T00:00:02Z'
        failed['body'] = failed['body'].replace('claude-audit-done', 'needs-claude-audit')
        failed['body'] += '\n<!-- CODE_MOWER_AUDIT_REQUEUE: kind=unknown -->'
        self.assertEqual(self.evaluate([self.declaration(), self.review(self.current), failed]), 'unknown')

    def test_failed_input_discovery_invalidates_pass_but_other_revision_failure_does_not(self):
        failed = self.review()
        failed['updated_at'] = '2026-01-01T00:00:02Z'
        failed['body'] = 'Head SHA: `' + self.head + '`\n<!-- CODE_MOWER_AUDIT_REQUEUE: kind=unknown -->'
        comments = [self.declaration(), self.review(self.current)]
        self.assertEqual(self.evaluate([*comments, failed]), 'unknown')
        failed['body'] += marker({**self.current, 'revision': 'c' * 32}, review=True)
        self.assertEqual(self.evaluate([*comments, failed]), 'done')
        failed['body'] = failed['body'].split('<!-- CODE_MOWER_CONTEXT_REVIEW')[0].replace(self.head, 'd' * 40)
        self.assertEqual(self.evaluate([*comments, failed]), 'done')

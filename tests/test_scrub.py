import unittest

from apprentice.scrub import scrub, mask_outbound, luhn_ok, iban_ok, EMAIL_RE, TICKET_RE, UUID_RE
from tests.base import ApprenticeTestCase


class ScrubKindsTest(ApprenticeTestCase):
    def assertRedacted(self, text, kind, replacement, leftover=""):
        out, flags = scrub(text)
        self.assertIn(replacement, out)
        self.assertIn("redacted:" + kind, flags)
        if leftover:
            self.assertIn(leftover, out)
        return out

    def assertKept(self, text):
        out, flags = scrub(text)
        self.assertEqual(out, text)
        self.assertEqual([f for f in flags if f.startswith("redacted:")], [])

    # private-key
    def test_private_key_block(self):
        begin = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
        end = "-----END " + "RSA PRIVATE" + " KEY-----"
        out = self.assertRedacted("before " + begin + "\nabcdef\n" + end + " after",
                                  "private-key", "<redacted:private-key>", "after")
        self.assertNotIn("abcdef", out)

    def test_private_key_unterminated_runs_to_end(self):
        begin = "-----BEGIN " + "PRIVATE" + " KEY-----"
        out, flags = scrub("x " + begin + "\nabcdef trailing")
        self.assertEqual(out, "x <redacted:private-key>")
        self.assertIn("redacted:private-key", flags)

    def test_public_key_block_kept(self):
        self.assertKept("-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----")

    # github-token
    def test_github_token_prefix(self):
        tok = "gh" + "p_" + "a" * 24
        out = self.assertRedacted("use " + tok + " now", "github-token", "<redacted:github-token>")
        self.assertNotIn(tok, out)

    def test_github_pat(self):
        tok = "github_" + "pat_" + "A1" * 15
        self.assertRedacted("t=" + tok, "github-token", "<redacted:github-token>")

    def test_github_token_too_short_kept(self):
        self.assertKept("gh" + "p_" + "a" * 5)

    # gitlab-token
    def test_gitlab_token(self):
        tok = "gl" + "pat-" + "a" * 20
        self.assertRedacted("x " + tok, "gitlab-token", "<redacted:gitlab-token>")

    def test_gitlab_token_too_short_kept(self):
        self.assertKept("gl" + "pat-" + "a" * 5)

    # npm-token
    def test_npm_token(self):
        tok = "np" + "m_" + "a" * 30
        self.assertRedacted("x " + tok, "npm-token", "<redacted:npm-token>")

    def test_npm_token_too_short_kept(self):
        self.assertKept("np" + "m_" + "a" * 10)

    # api-key
    def test_api_key_sk(self):
        tok = "s" + "k-" + "a" * 24
        self.assertRedacted("key " + tok, "api-key", "<redacted:api-key>")

    def test_api_key_live_prefix(self):
        tok = "s" + "k_live_" + "a" * 16
        self.assertRedacted("x " + tok, "api-key", "<redacted:api-key>")

    def test_api_key_restricted_test_prefix(self):
        tok = "r" + "k_test_" + "b" * 16
        self.assertRedacted("x " + tok, "api-key", "<redacted:api-key>")

    def test_api_key_short_kept(self):
        self.assertKept("s" + "k-" + "a" * 5)

    # google-key
    def test_google_api_key(self):
        tok = "AI" + "za" + "a" * 35
        self.assertRedacted("x " + tok, "google-key", "<redacted:google-key>")

    def test_google_oauth_token(self):
        tok = "ya" + "29." + "a" * 20
        self.assertRedacted("x " + tok, "google-key", "<redacted:google-key>")

    def test_google_key_short_kept(self):
        self.assertKept("AI" + "za" + "a" * 10)

    # aws-key
    def test_aws_key(self):
        tok = "AK" + "IA" + "A" * 16
        self.assertRedacted("x " + tok, "aws-key", "<redacted:aws-key>")

    def test_aws_key_lowercase_kept(self):
        self.assertKept("AK" + "IA" + "a" * 16)

    # slack-token
    def test_slack_token(self):
        tok = "xo" + "xb-" + "1234567890"
        self.assertRedacted("x " + tok, "slack-token", "<redacted:slack-token>")

    def test_slack_token_short_kept(self):
        self.assertKept("xo" + "xb-" + "123")

    # jwt
    def test_jwt(self):
        tok = "ey" + "J" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10
        self.assertRedacted("x " + tok, "jwt", "<redacted:jwt>")

    def test_jwt_two_segments_kept(self):
        self.assertKept("ey" + "J" + "a" * 10 + "." + "b" * 10)

    # bearer
    def test_bearer(self):
        out = self.assertRedacted("Authorization: Bearer " + "a" * 20, "bearer", "Bearer <redacted>")
        self.assertNotIn("a" * 20, out)

    def test_bearer_lowercase(self):
        tok = "abcdefghij" + "0123456789"
        out = self.assertRedacted("Authorization: bear" + "er " + tok, "bearer", "bearer <redacted>")
        self.assertNotIn(tok, out)

    def test_basic_auth(self):
        tok = "dXNlcjpwYXNz" + "d29yZDEyMzQ1Ng=="
        out = self.assertRedacted("Authorization: Ba" + "sic " + tok, "bearer", "Basic <redacted>")
        self.assertNotIn(tok, out)

    def test_bearer_short_kept(self):
        self.assertKept("Bearer abc")

    # url-credentials
    def test_url_credentials(self):
        url = "https://user:hunter2" + "@" + "host.test/repo"
        out = self.assertRedacted("clone " + url, "url-credentials",
                                  "https://<redacted>@host.test/repo")
        self.assertNotIn("hunter2", out)

    def test_url_without_credentials_kept(self):
        self.assertKept("clone https://host.test:8080/repo")

    # assignment
    def test_assignment_colon(self):
        out = self.assertRedacted("Password: hunter2 ok", "assignment", "Password: <redacted>", "ok")
        self.assertNotIn("hunter2", out)

    def test_assignment_equals_api_key(self):
        out = self.assertRedacted("api_key=abc123", "assignment", "api_key=<redacted>")
        self.assertNotIn("abc123", out)

    def test_assignment_word_without_value_kept(self):
        self.assertKept("the password policy and token budget")

    def test_assignment_prose_kept(self):
        self.assertKept("the token limit is high")

    def test_assignment_prefixed_env_names(self):
        for key, value in (("DB_" + "PASSWORD", "hunter2hunter2"),
                           ("AWS_SECRET_" + "ACCESS_KEY", "wJalrXUtnFEMI" + "K7MDENGbPxRfiCYEXAMPLEKEY"),
                           ("client_" + "secret", "abcDEF123ghiJKL456")):
            out = self.assertRedacted(key + "=" + value + " next", "assignment",
                                      key + "=<redacted>", " next")
            self.assertNotIn(value, out)

    def test_assignment_json_forms(self):
        pw = '{"pass' + 'word": "hunter2hunter2"}'
        out = self.assertRedacted(pw, "assignment", '"password": "<redacted>"')
        self.assertNotIn("hunter2", out)
        key = '{"api_' + 'key": "abcd1234abcd1234", "x": 1}'
        out = self.assertRedacted(key, "assignment", '"api_key": "<redacted>"', '"x": 1')
        self.assertNotIn("abcd1234", out)

    # hex
    def test_hex_near_keyword(self):
        run = "ab12" * 10
        out = self.assertRedacted("the key is " + run + " done", "hex", "<redacted:hex>")
        self.assertNotIn(run, out)

    def test_hex_keyword_after_run(self):
        run = "ab12" * 10
        self.assertRedacted(run + " secret", "hex", "<redacted:hex>")

    def test_hex_short_run_kept(self):
        self.assertKept("the key is " + "ab12" * 4)

    def test_git_sha_without_keyword_kept(self):
        self.assertKept("commit " + "a1b2c3d4e5" * 4 + " fixes it")

    # pan
    def test_pan_passing_luhn(self):
        out = self.assertRedacted("card 4111 1111 1111 1111 end", "pan", "<redacted:pan>", "end")
        self.assertNotIn("4111", out)

    def test_pan_hyphenated(self):
        self.assertRedacted("card 4111-1111-1111-1111", "pan", "<redacted:pan>")

    def test_pan_short_digits_kept(self):
        self.assertKept("order 1234567890")

    def test_pan_followed_by_cvv(self):
        out = self.assertRedacted("card 4111 1111 1111 1111 123 thanks", "pan", "<redacted:pan>")
        self.assertEqual(out, "card <redacted:pan> 123 thanks")

    def test_pan_compact_followed_by_digits(self):
        out = self.assertRedacted("card 4111111111111111 12", "pan", "<redacted:pan>")
        self.assertEqual(out, "card <redacted:pan> 12")

    def test_pan_preceded_by_digits(self):
        out = self.assertRedacted("exp 12 4111 1111 1111 1111", "pan", "<redacted:pan>")
        self.assertEqual(out, "exp 12 <redacted:pan>")

    def test_lists_of_small_numbers_kept(self):
        self.assertKept(" ".join("%03d" % i for i in range(190, 300)))

    def test_16_digits_failing_luhn_kept(self):
        self.assertKept("4111 1111 1111 1112")

    # iban
    def test_iban_passing_mod97(self):
        out = self.assertRedacted("pay GB82 WEST 1234 5698 7654 32 today", "iban", "<redacted:iban>", "today")
        self.assertNotIn("GB82", out)

    def test_iban_compact(self):
        self.assertRedacted("pay GB82WEST12345698765432", "iban", "<redacted:iban>")

    def test_iban_followed_by_capitals(self):
        out, flags = scrub("pay DE89 3704 0044 0532 0130 00 ASAP")
        self.assertEqual(out, "pay <redacted:iban> ASAP")
        self.assertIn("redacted:iban", flags)
        self.assertNotIn("redacted:pan", flags)

    def test_iban_shape_failing_mod97_kept(self):
        self.assertKept("pay GB82WEST12345698765433")

    def test_iban_lowercase_kept(self):
        self.assertKept("pay gb82west12345698765432")

    # custom
    def test_custom_pattern(self):
        cfg = {"extra_redact_patterns": [r"ZZ\d{4}"]}
        out, flags = scrub("id ZZ1234 end", cfg)
        self.assertEqual(out, "id <redacted:custom> end")
        self.assertIn("redacted:custom", flags)

    def test_custom_pattern_no_match_kept(self):
        cfg = {"extra_redact_patterns": [r"ZZ\d{4}"]}
        self.assertEqual(scrub("id ZZ12 end", cfg), ("id ZZ12 end", []))

    def test_custom_invalid_pattern_ignored(self):
        cfg = {"extra_redact_patterns": ["(", r"ZZ\d{4}"]}
        out, _ = scrub("id ZZ1234", cfg)
        self.assertEqual(out, "id <redacted:custom>")


class ScrubFlagsTest(ApprenticeTestCase):
    def test_email_flagged_not_removed(self):
        text = "mail " + "alice" + "@" + "example.test" + " please"
        out, flags = scrub(text)
        self.assertEqual(out, text)
        self.assertIn("email", flags)
        self.assertIn("foreign_email", flags)

    def test_own_domain_not_foreign(self):
        text = "mail " + "alice" + "@" + "example.test"
        out, flags = scrub(text, {"own_email_domains": ["example.test"]})
        self.assertEqual(out, text)
        self.assertIn("email", flags)
        self.assertNotIn("foreign_email", flags)

    def test_no_email_no_flag(self):
        self.assertEqual(scrub("nothing here"), ("nothing here", []))

    def test_none_passthrough(self):
        self.assertEqual(scrub(None), (None, []))

    def test_flags_kinds_once(self):
        a = "gh" + "p_" + "a" * 24
        b = "gh" + "o_" + "b" * 24
        out, flags = scrub(a + " " + b)
        self.assertEqual(out, "<redacted:github-token> <redacted:github-token>")
        self.assertEqual(flags.count("redacted:github-token"), 1)


class HelpersTest(ApprenticeTestCase):
    def test_mask_outbound_uuid_and_email(self):
        uid = "123e4567-e89b-12d3-a456-426614174000"
        text = "id " + uid + " from " + "bob" + "@" + "example.test"
        self.assertEqual(mask_outbound(text), "id <uuid> from <email>")

    def test_luhn_ok(self):
        self.assertTrue(luhn_ok("4111111111111111"))
        self.assertFalse(luhn_ok("4111111111111112"))

    def test_iban_ok(self):
        self.assertTrue(iban_ok("GB82 WEST 1234 5698 7654 32"))
        self.assertFalse(iban_ok("GB82WEST12345698765433"))

    def test_exported_patterns(self):
        self.assertTrue(EMAIL_RE.search("bob" + "@" + "example.test"))
        self.assertEqual(TICKET_RE.findall("see XX-123 and x-1"), ["XX-123"])
        self.assertTrue(UUID_RE.search("123e4567-e89b-12d3-a456-426614174000"))


if __name__ == "__main__":
    unittest.main()

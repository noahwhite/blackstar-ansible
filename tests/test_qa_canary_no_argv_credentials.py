#!/usr/bin/env python3
"""Regression guard: the QA canary never puts a credential on a command line.

Any user on the QA runner can read a process's argv through ps or
/proc/<pid>/cmdline, and probes on the box copy the canary's pattern. These
tests fail if the canary passes Infisical credentials as flags, builds a JSON
body with `jq --arg`, or expands a variable inside an inline curl header or
body. Run with
    python3 -m unittest discover -s tests -v
"""
import pathlib
import re
import unittest

CANARY = (
    pathlib.Path(__file__).resolve().parent.parent
    / "roles"
    / "qa-runner"
    / "files"
    / "qa-canary.sh"
)


def code_lines():
    for n, line in enumerate(CANARY.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


class QaCanaryNoArgvCredentials(unittest.TestCase):
    def assert_absent(self, pattern, why):
        hits = [f"{n}: {line.strip()}" for n, line in code_lines() if re.search(pattern, line)]
        self.assertEqual(hits, [], why)

    def test_no_infisical_credential_flags(self):
        self.assert_absent(
            r"--(client-id|client-secret|token)\b",
            "pass Infisical credentials in INFISICAL_UNIVERSAL_AUTH_* / INFISICAL_TOKEN",
        )

    def test_no_jq_arg(self):
        self.assert_absent(r"\bjq\b.*--(arg|argjson)\b", "build JSON from env.* in jq")

    def test_no_variable_in_inline_curl_header_or_body(self):
        self.assert_absent(
            r"(-H|--header|-d|--data(-binary|-raw|-ascii)?|--json|-u|--user|--proxy-user|--oauth2-bearer|-b|--cookie)(\s+|=)\S*\$",
            "feed secret headers and bodies via -H @<(printf ...) or stdin",
        )

    def test_no_cookie_jar_on_disk(self):
        self.assert_absent(r"\s(-c|--cookie-jar)\s", "check Set-Cookie in memory, not in a jar file")

    def test_no_header_array_expansion(self):
        self.assert_absent(r"\"\$\{H\[@\]\}\"", "a header array expands onto argv")

    def test_infisical_auth_via_env(self):
        text = CANARY.read_text()
        self.assertIn("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=", text)
        self.assertIn("export INFISICAL_TOKEN", text)


if __name__ == "__main__":
    unittest.main()

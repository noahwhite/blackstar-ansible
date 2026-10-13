#!/usr/bin/env python3
"""Regression guard: the QA canary never puts a credential on a command line.

Any user on the QA runner can read a process's argv through ps or
/proc/<pid>/cmdline, and probes on the box copy the canary's pattern. These
tests fail if the canary passes Infisical credentials as flags, builds a JSON
body with `jq --arg`, expands a variable inside a curl header, body, user or
cookie argument, or writes a cookie jar. Run with
    python3 -m unittest discover -s tests -v
"""
import pathlib
import re
import shlex
import subprocess
import unittest

CANARY = (
    pathlib.Path(__file__).resolve().parent.parent
    / "roles"
    / "qa-runner"
    / "files"
    / "qa-canary.sh"
)

# curl short options that take a value (to find -c inside a cluster such as -sc).
SHORT_WITH_VALUE = set("HdubcowDXeArTFKxUEmY")
# Every curl argument that expands a variable is a violation unless it is one of
# these non-secret expansions: the Loki query window, an output file in the private
# work dir, or a URL on the Grafana base.
ALLOWED_VALUES = {"start=$START", "end=$END"}
ALLOWED_PREFIXES = ("$W/", "$GURL/")
# A process substitution may only call the script's own printf-builtin helpers.
PROCSUB_BODY = re.compile(r'cfh|bearer "\$\w+"')


def code_lines(text):
    for n, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


def logical_lines(text):
    code = "\n".join(line for _, line in code_lines(text))
    return code.replace("\\\n", " ").splitlines()


def curl_segments(line):
    """Yield the argument text of each curl call: up to an unquoted |, ;, & or the ) closing its $(...)."""
    for m in re.finditer(r"\bcurl\b", line):
        quote, depth, end = None, 0, len(line)
        for k in range(m.end(), len(line)):
            c = line[k]
            if quote:
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    end = k
                    break
                depth -= 1
            elif c in "|;&" and depth == 0 and not (c == "&" and (line[k - 1] in "<>" or line[k + 1:k + 2] == ">")):
                end = k
                break
        yield line[m.end():end]


def allowed(tok):
    """An allowed expansion: an exact allowed value, or an allowed prefix with no further expansion."""
    if tok in ALLOWED_VALUES:
        return True
    return any(tok.startswith(p) and "$" not in tok[len(p):] for p in ALLOWED_PREFIXES)


def procsubs(segment):
    """Split out each @<(...) body; return the bodies and the segment with each replaced by @PROCSUB."""
    bodies, out, k = [], [], 0
    while True:
        s = segment.find("@<(", k)
        if s < 0:
            out.append(segment[k:])
            return bodies, "".join(out)
        depth, e = 1, s + 3
        while e < len(segment) and depth:
            depth += {"(": 1, ")": -1}.get(segment[e], 0)
            e += 1
        bodies.append(segment[s + 3:e - 1].strip())
        out.append(segment[k:s] + "@PROCSUB")
        k = e


def curl_violations(text):
    """Return the curl arguments that would expand a variable onto argv or name a cookie jar."""
    hits = []
    for line in logical_lines(text):
        for segment in curl_segments(line):
            bodies, rest = procsubs(segment)
            hits += [f"process substitution runs more than a helper: {b}" for b in bodies if not PROCSUB_BODY.fullmatch(b)]
            try:
                tokens = shlex.split(rest)
            except ValueError:
                hits.append(f"unparseable curl call: {segment.strip()}")
                continue
            for tok in tokens:
                if tok == "--cookie-jar" or tok.startswith("--cookie-jar="):
                    hits.append(f"cookie jar: {segment.strip()}")
                elif tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
                    for ch in tok[1:]:
                        if ch in SHORT_WITH_VALUE:
                            if ch == "c":
                                hits.append(f"cookie jar: {segment.strip()}")
                            break
                if "$" in tok and not allowed(tok):
                    hits.append(f"expanded argument: {tok}")
    return hits


class CurlGuard(unittest.TestCase):
    """The guard itself: each credential-bearing form must trip it, each safe form must not."""

    MUST_FAIL = [
        'curl -s -H "Authorization: Bearer $TOK" https://x',
        "curl -s -H 'X: '$TOK https://x",
        "curl -s -H$TOK https://x",
        'curl -sH "Authorization: Bearer $TOK" https://x',
        'curl -s -d "password=$P" https://x',
        'curl -s --data-binary "{\\"p\\":\\"$P\\"}" https://x',
        'curl -s --data-raw "$BODY" https://x',
        'curl -s --data-urlencode "token=$TOK" https://x',
        'curl -s --json "$BODY" https://x',
        'curl -s -u "user:$P" https://x',
        "curl -s --user user:$P https://x",
        'curl -s --oauth2-bearer "$TOK" https://x',
        'curl -s -b "officina_session=$C" https://x',
        "curl -s -F token=$TOK https://x",
        "curl -s -c jar https://x",
        "curl -s -cjar https://x",
        "curl -sc jar https://x",
        "curl -s --cookie-jar jar https://x",
        'x=$(curl -s -o /dev/null \\\n  -H "Authorization: Bearer $TOK" https://x)',
        'curl -s https://x 2>&1 -H "Authorization: Bearer $TOK"',
        'curl -s "https://x/api?token=$TOK"',
        'curl -s --url "https://x/?k=$TOK"',
        'curl -s -A "$TOK" https://x',
        'curl -s -e "https://x/$TOK" https://x',
        'curl -s --url-query "k=$TOK" https://x',
        'curl -s --variable "t=$TOK" https://x',
        'curl -s -H @<(env printf "Authorization: Bearer %s\\n" "$TOK") https://x',
        'curl -s -H @<(/usr/bin/printf "X: %s" "$TOK") https://x',
        'curl -s -H @<(cfh; env printf "%s" "$TOK") https://x',
        'curl -s "$GURL/api?token=$TOK"',
        'curl -s -o "$W/$TOK" https://x',
    ]
    MUST_PASS = [
        'curl -s -H @<(bearer "$SA") "$GURL/api"',
        "curl -s -H @<(cfh) https://x",
        'curl -s -H "Content-Type: application/json" --data-binary @- https://x',
        'curl -s -o "$W/q.json" -w "%{http_code}" -G --data-urlencode "start=$START" https://x',
        "curl -s -d '{\"email\":\"canary@example.com\"}' https://x",
        "curl -s --cacert /etc/ca.pem https://x",
        'curl -s -o "$W/me.json" -w "%{http_code}" -H @<(cfh) "$GURL/api/x"',
        "jq -nc '{a:env.A}' | curl -s --data-binary @- https://x",
    ]

    def test_guard_trips_on_every_credential_form(self):
        for line in self.MUST_FAIL:
            with self.subTest(line=line):
                self.assertNotEqual(curl_violations(line), [])

    def test_guard_passes_safe_forms(self):
        for line in self.MUST_PASS:
            with self.subTest(line=line):
                self.assertEqual(curl_violations(line), [])


class QaCanaryNoArgvCredentials(unittest.TestCase):
    def setUp(self):
        self.text = CANARY.read_text()

    def assert_absent(self, pattern, why):
        hits = [f"{n}: {line.strip()}" for n, line in code_lines(self.text) if re.search(pattern, line)]
        self.assertEqual(hits, [], why)

    def test_no_infisical_credential_flags(self):
        self.assert_absent(
            r"--(client-id|client-secret|token)\b",
            "pass Infisical credentials in INFISICAL_UNIVERSAL_AUTH_* / INFISICAL_TOKEN",
        )

    def test_no_jq_arg(self):
        self.assert_absent(r"\bjq\b.*--(arg|argjson)\b", "build JSON from env.* in jq")

    def test_no_credential_on_curl_argv_or_cookie_jar(self):
        self.assertEqual(
            curl_violations(self.text),
            [],
            "feed secret headers and bodies via -H @<(printf ...) or stdin; check Set-Cookie in memory",
        )

    def test_no_array_expansion(self):
        self.assert_absent(r"\$\{\w+\[[@*]\]\}", "an argument array hides its contents from this guard")

    def test_array_guard_trips(self):
        self.assertTrue(re.search(r"\$\{\w+\[[@*]\]\}", 'curl -s "${args[@]}" https://x'))

    def test_infisical_auth_via_env(self):
        self.assertIn("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=", self.text)
        self.assertIn("export INFISICAL_TOKEN", self.text)


class SessionCookieCheck(unittest.TestCase):
    """Run the canary's own cookie test against synthetic response headers."""

    VALUE = "s3cr3t-session-value"

    def setUp(self):
        lines = [l for l in CANARY.read_text().splitlines() if "=~" in l and "officina_session=" in l]
        self.assertEqual(len(lines), 1, "expected one session-cookie check in the canary")
        self.check = lines[0].strip()

    def run_check(self, headers):
        script = 'hdrs=$(cat); pc=${hdrs##*HTTP_CODE=}; ck=no\n' + self.check + '\necho "ck=$ck"\n'
        out = subprocess.run(
            ["bash", "-c", script], input=headers, capture_output=True, text=True, check=True
        ).stdout
        self.assertNotIn(self.VALUE, out)
        return out.strip()

    def test_populated_cookie_matches(self):
        hdrs = f"HTTP/2 200\r\nSet-Cookie: officina_session={self.VALUE}; Path=/; HttpOnly\r\n\r\n\nHTTP_CODE=200"
        self.assertEqual(self.run_check(hdrs), "ck=yes")

    def test_header_name_is_case_insensitive(self):
        hdrs = f"HTTP/2 200\r\nset-cookie: officina_session={self.VALUE}; Path=/\r\n\r\n\nHTTP_CODE=200"
        self.assertEqual(self.run_check(hdrs), "ck=yes")

    def test_cleared_cookie_does_not_match(self):
        hdrs = "HTTP/2 401\r\nSet-Cookie: officina_session=; Max-Age=0\r\n\r\n\nHTTP_CODE=401"
        self.assertEqual(self.run_check(hdrs), "ck=no")

    def test_cookie_on_failed_login_does_not_count(self):
        hdrs = f"HTTP/2 401\r\nSet-Cookie: officina_session={self.VALUE}; Max-Age=0\r\n\r\n\nHTTP_CODE=401"
        self.assertEqual(self.run_check(hdrs), "ck=no")

    def test_other_cookie_does_not_match(self):
        hdrs = f"HTTP/2 200\r\nSet-Cookie: other_session={self.VALUE}\r\nX-Note: officina_session={self.VALUE}\r\nX-Upstream-Set-Cookie: officina_session={self.VALUE}\r\n\r\n\nHTTP_CODE=200"
        self.assertEqual(self.run_check(hdrs), "ck=no")


if __name__ == "__main__":
    unittest.main()

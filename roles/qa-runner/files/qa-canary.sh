#!/usr/bin/env bash
# QA canary (OFF-856) - verifies each dev-verification lane end-to-end from the
# QA workstation, through the real Cloudflare edge, each lane authenticating with
# a credential pulled from Infisical /qa at runtime. Read-only throughout.
#
# Delivered to /opt/qa/canary.sh by the qa-runner Ansible role. Run: bash canary.sh
#
# No credential ever reaches a command line: any user on the box can read argv
# through ps or /proc/<pid>/cmdline. Credentials travel in env vars the tools
# read (infisical, gh, linear), as curl headers read from a bash process
# substitution that only the printf builtin writes (-H @<(...)), and as JSON
# built by jq from env.* and piped on stdin. Probes copied from this script
# inherit the pattern.
set -uo pipefail

: "${QA_INFISICAL_ENV_FILE:=/etc/officina-qa/infisical.env}"
set -a; . "$QA_INFISICAL_ENV_FILE"; set +a

# Response bodies live in a private dir, removed on exit.
W=$(mktemp -d) || { echo "QA CANARY: mktemp failed" >&2; exit 1; }
trap 'rm -rf "$W"' EXIT

export INFISICAL_API_URL="$QA_INFISICAL_HOST/api"
export INFISICAL_TOKEN
INFISICAL_TOKEN=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$QA_INFISICAL_CLIENT_ID" \
  INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$QA_INFISICAL_CLIENT_SECRET" \
  infisical login --method=universal-auth --plain --silent 2>/dev/null)
unset QA_INFISICAL_CLIENT_SECRET
g() {
  infisical secrets get "$1" --projectId="$QA_INFISICAL_PROJECT_ID" \
    --env="$QA_INFISICAL_ENV" --path="$QA_INFISICAL_PATH" --plain --silent 2>/dev/null
}
bearer() { printf 'Authorization: Bearer %s\n' "$1"; }

echo "QA CANARY $(date -u +%FT%TZ)  egress=$(curl -s https://api.ipify.org)"

# --- Portal ---------------------------------------------------------------
# The session cookie is checked in the response headers, held in memory only.
E=$(g QA_TEST_CUSTOMER_EMAIL); P=$(g QA_TEST_CUSTOMER_PASSWORD); ck=no
if [ -n "$E" ] && [ -n "$P" ]; then
  hdrs=$(E="$E" P="$P" jq -nc '{email:env.E,password:env.P}' | curl -s -D - -o /dev/null \
    -w '\nHTTP_CODE=%{http_code}' -H "Content-Type: application/json" --data-binary @- \
    "https://my.dev-officina.work/api/auth/login")
  pc=${hdrs##*HTTP_CODE=}
  # Matched in bash: a pipe into grep -q can SIGPIPE under pipefail, and a
  # cleared cookie (officina_session=;) must not count.
  [[ ${hdrs,,} =~ $'\n'set-cookie:[[:space:]]*officina_session=[^\;[:space:]] ]] && ck=yes
  unset hdrs
else
  pc=MISSING_CREDS
fi
unset E P
echo "PORTAL   login=$pc  officina_session_cookie=$ck"

# --- Console (read 200 / mutation 403) ------------------------------------
CID=$(g QA_OPERATOR_CF_ACCESS_CLIENT_ID); CSEC=$(g QA_OPERATOR_CF_ACCESS_CLIENT_SECRET)
cfh() { printf 'CF-Access-Client-Id: %s\nCF-Access-Client-Secret: %s\n' "$CID" "$CSEC"; }
me=$(curl -s -o "$W/me.json" -w "%{http_code}" -H @<(cfh) "https://admin.dev-officina.work/api/admin/me")
kind=$(jq -r .kind "$W/me.json" 2>/dev/null); role=$(jq -r .role "$W/me.json" 2>/dev/null)
ops=$(curl -s -o /dev/null -w "%{http_code}" -H @<(cfh) "https://admin.dev-officina.work/api/admin/operators")
mut=$(curl -s -o /dev/null -w "%{http_code}" -H @<(cfh) -X POST -H "Content-Type: application/json" \
  -d '{"email":"canary@example.com","role":"read_only"}' "https://admin.dev-officina.work/api/admin/operators")
echo "CONSOLE  me=$me(kind=$kind,role=$role)  operators=$ops  mutation=$mut"

# --- Backend (Loki/Tempo) -------------------------------------------------
SA=$(g QA_GRAFANA_SA_TOKEN); GURL=$(g QA_GRAFANA_URL); GURL="${GURL:-https://grafana343c.grafana.net}"
ll=$(curl -s -o /dev/null -w "%{http_code}" -H @<(bearer "$SA") \
  "$GURL/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1/labels")
END=$(date +%s)000000000; START=$(( $(date +%s) - 3600 ))000000000
qc=$(curl -s -o "$W/q.json" -w "%{http_code}" -G -H @<(bearer "$SA") \
  --data-urlencode "query=count_over_time({environment=\"dev\"}[5m])" \
  --data-urlencode "start=$START" --data-urlencode "end=$END" \
  "$GURL/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1/query_range")
series=$(jq '.data.result|length' "$W/q.json" 2>/dev/null)
te=$(curl -s -o /dev/null -w "%{http_code}" -H @<(bearer "$SA") \
  "$GURL/api/datasources/proxy/uid/grafanacloud-traces/api/echo")
echo "BACKEND  loki_labels=$ll  loki_dev_query=$qc(series=$series)  tempo_echo=$te"

# --- Billing (Paddle sandbox) ---------------------------------------------
PK=$(g QA_PADDLE_SANDBOX_API_KEY)
pd=$(curl -s -o "$W/pd.json" -w "%{http_code}" -H @<(bearer "$PK") \
  "https://sandbox-api.paddle.com/event-types")
evt=$(jq '.data|length' "$W/pd.json" 2>/dev/null)
echo "PADDLE   event_types=$pd(count=$evt)"

# --- GitHub (gh CLI, read-only) -------------------------------------------
# gh reads GH_TOKEN from the env; the token is a READ-ONLY fine-grained PAT from
# /qa. Prove read works AND that a write is denied. The write probe is a no-op
# even if it somehow succeeded: it PATCHes the repo description to its CURRENT
# value, so a 200 changes nothing and a 403 confirms read-only.
export GH_TOKEN=$(g QA_GH_TOKEN)
if ghname=$(gh api repos/officina-pub/officina --jq .full_name 2>/dev/null); then
  ghread="ok($ghname)"
else
  ghread="FAIL"
fi
curdesc=$(gh api repos/officina-pub/officina --jq '.description // ""' 2>/dev/null)
if gh api -X PATCH repos/officina-pub/officina -f description="$curdesc" >/dev/null 2>&1; then
  ghwrite="ALLOWED(token is NOT read-only!)"
else
  ghwrite="denied(read-only confirmed)"
fi
echo "GITHUB   read=$ghread  write=$ghwrite"

# --- Linear (linear CLI, read-only) ---------------------------------------
# `linear` reads LINEAR_API_KEY from the env; the token is a READ-ONLY Linear
# API key from /qa. Read this very story (OFF-856) as the probe. The key's scope
# is what keeps it read-only.
export LINEAR_API_KEY=$(g QA_LINEAR_API_KEY)
if linear issue view OFF-856 >"$W/lin.txt" 2>/dev/null && grep -qi 'OFF-856' "$W/lin.txt"; then
  linread="ok"
else
  linread="FAIL"
fi
echo "LINEAR   read=$linread"

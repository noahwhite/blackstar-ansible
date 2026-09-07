#!/usr/bin/env bash
# QA canary (OFF-856) - verifies each dev-verification lane end-to-end from the
# QA workstation, through the real Cloudflare edge, each lane authenticating with
# a credential pulled from Infisical /qa at runtime. Read-only throughout.
#
# Delivered to /opt/qa/canary.sh by the qa-runner Ansible role. Run: bash canary.sh
set -uo pipefail

: "${QA_INFISICAL_ENV_FILE:=/etc/officina-qa/infisical.env}"
set -a; . "$QA_INFISICAL_ENV_FILE"; set +a

TOKEN=$(infisical login --method=universal-auth \
  --client-id="$QA_INFISICAL_CLIENT_ID" --client-secret="$QA_INFISICAL_CLIENT_SECRET" \
  --domain="$QA_INFISICAL_HOST/api" --plain --silent 2>/dev/null)
g() {
  infisical secrets get "$1" --projectId="$QA_INFISICAL_PROJECT_ID" \
    --env="$QA_INFISICAL_ENV" --path="$QA_INFISICAL_PATH" \
    --domain="$QA_INFISICAL_HOST/api" --token="$TOKEN" --plain --silent 2>/dev/null
}

echo "QA CANARY $(date -u +%FT%TZ)  egress=$(curl -s https://api.ipify.org)"

# --- Portal ---------------------------------------------------------------
E=$(g QA_TEST_CUSTOMER_EMAIL); P=$(g QA_TEST_CUSTOMER_PASSWORD)
body=$(jq -nc --arg e "$E" --arg p "$P" '{email:$e,password:$p}')
pc=$(curl -s -o /dev/null -w "%{http_code}" -c /tmp/qa_cj.txt \
  -H "Content-Type: application/json" -d "$body" "https://my.dev-officina.work/api/auth/login")
grep -q officina_session /tmp/qa_cj.txt && ck=yes || ck=no
echo "PORTAL   login=$pc  officina_session_cookie=$ck"

# --- Console (read 200 / mutation 403) ------------------------------------
CID=$(g QA_OPERATOR_CF_ACCESS_CLIENT_ID); CSEC=$(g QA_OPERATOR_CF_ACCESS_CLIENT_SECRET)
H=(-H "CF-Access-Client-Id: $CID" -H "CF-Access-Client-Secret: $CSEC")
me=$(curl -s -o /tmp/qa_me.json -w "%{http_code}" "${H[@]}" "https://admin.dev-officina.work/api/admin/me")
kind=$(jq -r .kind /tmp/qa_me.json 2>/dev/null); role=$(jq -r .role /tmp/qa_me.json 2>/dev/null)
ops=$(curl -s -o /dev/null -w "%{http_code}" "${H[@]}" "https://admin.dev-officina.work/api/admin/operators")
mut=$(curl -s -o /dev/null -w "%{http_code}" "${H[@]}" -X POST -H "Content-Type: application/json" \
  -d '{"email":"canary@example.com","role":"read_only"}' "https://admin.dev-officina.work/api/admin/operators")
echo "CONSOLE  me=$me(kind=$kind,role=$role)  operators=$ops  mutation=$mut"

# --- Backend (Loki/Tempo) -------------------------------------------------
SA=$(g QA_GRAFANA_SA_TOKEN); GURL=$(g QA_GRAFANA_URL); GURL="${GURL:-https://grafana343c.grafana.net}"
ll=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $SA" \
  "$GURL/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1/labels")
END=$(date +%s)000000000; START=$(( $(date +%s) - 3600 ))000000000
qc=$(curl -s -o /tmp/qa_q.json -w "%{http_code}" -G -H "Authorization: Bearer $SA" \
  --data-urlencode "query=count_over_time({environment=\"dev\"}[5m])" \
  --data-urlencode "start=$START" --data-urlencode "end=$END" \
  "$GURL/api/datasources/proxy/uid/grafanacloud-logs/loki/api/v1/query_range")
series=$(jq '.data.result|length' /tmp/qa_q.json 2>/dev/null)
te=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $SA" \
  "$GURL/api/datasources/proxy/uid/grafanacloud-traces/api/echo")
echo "BACKEND  loki_labels=$ll  loki_dev_query=$qc(series=$series)  tempo_echo=$te"

# --- Billing (Paddle sandbox) ---------------------------------------------
PK=$(g QA_PADDLE_SANDBOX_API_KEY)
pd=$(curl -s -o /tmp/qa_pd.json -w "%{http_code}" -H "Authorization: Bearer $PK" \
  "https://sandbox-api.paddle.com/event-types")
evt=$(jq '.data|length' /tmp/qa_pd.json 2>/dev/null)
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

rm -f /tmp/qa_cj.txt /tmp/qa_me.json /tmp/qa_q.json /tmp/qa_pd.json

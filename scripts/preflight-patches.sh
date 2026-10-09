#!/usr/bin/env bash
# usage: preflight-patches.sh <path to a claude_unlimited package dir, or a repo root / installed app root>
# Greps one marker per kept hunk of the 2026-10 gateway patches (they landed on this fork as PRs #2-#7),
# plus the markers that make this fork the updater's only source. Run it against the source tree, the
# BUILT package and the INSTALLED app/venv before and after every upgrade (scripts/upgrade-when-idle.sh does).
# Exit 0 only if every kept marker is present AND no superseded-patch marker is.
set -u
ROOT="${1:?path}"
[ -d "$ROOT/claude_unlimited" ] && PKG="$ROOT/claude_unlimited" || PKG="$ROOT"
fail=0
has()  { # file, fixed-string marker, label
  if grep -qF -- "$2" "$PKG/$1" 2>/dev/null; then printf '  ok    %-44s %s\n' "$3" "$1"; else printf '  MISSING %-42s %s  [%s]\n' "$3" "$1" "$2"; fail=1; fi; }
hasnt(){ if grep -qF -- "$2" "$PKG/$1" 2>/dev/null; then printf '  STALE %-44s %s  [%s]\n' "$3" "$1" "$2"; fail=1; else printf '  ok    %-44s %s absent\n' "$3" "$1"; fi; }
echo "== PR #13 billing-header strip";        has gateway.py '_BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"' "billing header prefix"
echo "== PR #14 1h cache / 400k compact";     has config.py 'claude_code_cache_ttl_1h: bool = True' "cache ttl setting"; has cli.py 'CLAUDE_CODE_PROMPT_CACHE_TTL' "cli env default"
echo "== PR #16 request size cap";            has proxy.py 'def check_request_size' "per-kind size check"; has proxy.py 'REQUEST_TOO_LARGE_STATUS = 413' "413 envelope"
echo "== PR #17 402 funding state";           has observation.py 'class BudgetUnavailable' "observation"; has router.py 'budget_unavailable: bool = False' "router field"
has router.py 'cooldown_until=now + timedelta(hours=1)' "1h park"; has gateway.py 'provider_budget_unavailable' "gateway 402 error"
has gateway.py '"budget_unavailable": rt.budget_unavailable' "persisted flag"; has daemon.py '"budget_unavailable": bool(' "dashboard field"
has static/app.js 'paymentRequiredBadge' "badge"; for l in en de es ro; do has locales/$l.json '"status.payment_required"' "locale $l"; done
echo "== PR #17 thinking-signature repair";   has proxy.py 'def retry_without_rejected_thinking' "proxy repair"; has gateway.py 'thinking_repair_attempted' "gateway once-only"
echo "== PR #17 routing";                     has gateway.py 'max(MAX_ROTATION_ATTEMPTS, len(pool_now.profiles))' "full rotation pass"; has gateway.py 'subscription_return' "api->subscription hand-back"
has gateway.py '_credential_check_not_before' "keystore read throttle"
echo "== PR #17 local /v1/models";            has daemon.py 'def _local_models_list' "models list"; has daemon.py 'path.rstrip("/") == "/v1/models"' "models route"
echo "== PR #15 dashboard load";              has db.py 'class _ReadPool' "read pool"; has usage_history.py 'class _Rollup' "incremental rollup"; has static/app.js 'POLL_STUCK_MS' "one tick at a time"
echo "== PR #18 poll cancel";                 has static/app.js 'function cancelLivePoll' "cancel"; has static/app.js '_livePollController' "abort controller"
echo "== fork is the only update source"
has updater.py 'GITHUB_OWNER = "makerxyz"' "updater owner"; hasnt updater.py 'DevDock-AI' "upstream owner"
echo "== interactive gateway scheduling"
has daemon_installer/macos_launchd.py '"ProcessType": "Interactive"' "interactive launchd service"
hasnt daemon_installer/macos_launchd.py '"ProcessType": "Background"' "background launchd service"
echo "== superseded local patches must be gone"
hasnt db.py 'def reporting_query' "read-only reporting connection"; hasnt db.py 'def cached_usage_query' "5 s usage cache"; hasnt usage_history.py 'snapshot=' "snapshot= plumbing"
[ $fail -eq 0 ] && echo "PREFLIGHT PASS" || echo "PREFLIGHT FAIL"; exit $fail

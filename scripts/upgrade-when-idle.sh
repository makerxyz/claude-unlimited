#!/usr/bin/env bash
# Upgrade the LIVE install from this fork with the shortest possible gateway gap.
#
#   scripts/upgrade-when-idle.sh [ref]        # ref = branch or tag of the fork, default main
#
# Why this exists: the gateway carries every Claude Code request on the machine, and a
# restart cuts in-flight streams (clients retry a dropped connection). So everything slow
# happens first, against nothing live: clone, pre-flight, build, and a fully staged copy of
# app/ and venv/ next to the live ones. Then, in one step, the live pair is renamed aside,
# the staged pair renamed into place and the daemon restarted through `claude-unlimited
# install`; health is checked at once and a failure renames the old pair back and restarts
# on it. By default it does NOT wait for the gateway to go quiet. Set CU_IDLE_MAX_WAIT=<s>
# to have it wait up to that long for a window with nothing in flight (idle_seconds >=
# CU_IDLE_SECONDS) before the swap; it proceeds anyway when the cap is reached.
#
# Launch it DETACHED so it survives the request that started it being cut:
#   python3 -c 'import subprocess;subprocess.Popen(["scripts/upgrade-when-idle.sh"],
#     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
#     start_new_session=True)'
# Log: ~/.local/state/claude-unlimited-upgrade.log (never contains a credential).
#
# Environment: CLAUDE_UNLIMITED_REPO (default: this fork), CLAUDE_UNLIMITED_PORT (4317),
# CU_IDLE_SECONDS (10), CU_IDLE_MAX_WAIT (0 = do not wait; e.g. 1200 for 20 min),
# CU_SRC (use this clean checkout instead of cloning).
set -uo pipefail

REPO="${CLAUDE_UNLIMITED_REPO:-https://github.com/makerxyz/claude-unlimited.git}"
REF="${1:-main}"
PORT="${CLAUDE_UNLIMITED_PORT:-4317}"
IDLE_SECONDS="${CU_IDLE_SECONDS:-10}"
MAX_WAIT="${CU_IDLE_MAX_WAIT:-0}"
BASE="http://127.0.0.1:${PORT}"
INSTALL_ROOT="$HOME/.local/share/claude-unlimited"
BIN_DIR="$HOME/.local/bin"
LOG="$HOME/.local/state/claude-unlimited-upgrade.log"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
export PATH="$BIN_DIR:/opt/homebrew/bin:/usr/local/bin:$PATH"
export CLAUDE_UNLIMITED_NO_OPEN=1          # an unattended upgrade must not open a browser tab

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
PY="$INSTALL_ROOT/venv/bin/python"; [ -x "$PY" ] || PY="python3"

status_field() { # field -> value from /api/status ("" if unreachable); never prints the csrf token
  curl -fsS --max-time 3 "$BASE/api/status" 2>/dev/null |
    "$PY" -c 'import sys,json; d=json.load(sys.stdin); v=d.get(sys.argv[1]); print("" if v is None else v)' "$1" 2>/dev/null
}
health_version() {
  curl -fsS --max-time 3 "$BASE/health" 2>/dev/null |
    "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("version",""))' 2>/dev/null
}
site_packages() { ls -d "$INSTALL_ROOT"/venv/lib/python3*/site-packages 2>/dev/null | head -1; }

log "==== upgrade start: repo=$REPO ref=$REF port=$PORT ===="
OLD_VERSION="$(health_version)"; log "running version: ${OLD_VERSION:-unreachable}"

# ---- 1. build and pre-flight (touches nothing live) --------------------------------------
W="$(mktemp -d /tmp/cu-upgrade.XXXXXX)"
if [ -n "${CU_SRC:-}" ]; then SRC="$CU_SRC"; else
  git clone --quiet --branch "$REF" "$REPO" "$W/src" || { log "FAIL: clone of $REPO @ $REF"; exit 1; }
  SRC="$W/src"
fi
cd "$SRC" || exit 1
[ -z "$(git status --porcelain)" ] || { log "FAIL: checkout is not clean"; exit 1; }
SHA="$(git rev-parse --short HEAD)"
EXPECT="$("$PY" -c 'import re,sys; print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", open("claude_unlimited/__init__.py").read()).group(1))')"
log "building $(git log -1 --oneline) -> expect version $EXPECT"
"$HERE/preflight-patches.sh" "$SRC" >/dev/null || { log "FAIL: preflight on source tree"; "$HERE/preflight-patches.sh" "$SRC" | grep -v '^  ok'; exit 1; }
node --check claude_unlimited/static/app.js || { log "FAIL: node --check"; exit 1; }
"$PY" -m pip install -q --no-deps --no-cache-dir --disable-pip-version-check --target "$W/site" "$SRC" ||
  { log "FAIL: building the package"; exit 1; }
"$HERE/preflight-patches.sh" "$W/site" >/dev/null || { log "FAIL: preflight on the BUILT package"; exit 1; }
log "pre-flight passed on source and built package"

# ---- 2. stage a complete app/ and venv/ beside the live ones (still touches nothing live) ----
TS="$(date +%Y%m%d-%H%M%S)"
STAGE="$INSTALL_ROOT/stage-$TS"
ROLL="$INSTALL_ROOT/rollback-$TS"
mkdir -p "$STAGE" || exit 1
cp -R "$SRC" "$STAGE/app" && rm -rf "$STAGE/app/.git" && find "$STAGE/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
"$PY" -m venv "$STAGE/venv" || { log "FAIL: creating the staged venv"; rm -rf "$STAGE"; exit 1; }
"$STAGE/venv/bin/pip" install -q --no-cache-dir --disable-pip-version-check "$STAGE/app" ||
  { log "FAIL: installing into the staged venv"; rm -rf "$STAGE"; exit 1; }
# The venv was built at the staging path; its console scripts carry that path in their shebang.
# Point them at the final location now so the rename below is all that is left to do.
grep -rlI --null -e "$STAGE/venv" "$STAGE/venv/bin" 2>/dev/null | xargs -0 perl -pi -e "s#\Q$STAGE/venv\E#$INSTALL_ROOT/venv#g"
SITE="$(ls -d "$STAGE"/venv/lib/python3*/site-packages | head -1)"
"$HERE/preflight-patches.sh" "$SITE" >/dev/null || { log "FAIL: preflight on the staged venv"; rm -rf "$STAGE"; exit 1; }
STAGED_VERSION="$("$STAGE/venv/bin/python" -c 'import claude_unlimited; print(claude_unlimited.__version__)')"
[ "$STAGED_VERSION" = "$EXPECT" ] || { log "FAIL: staged venv imports $STAGED_VERSION, wanted $EXPECT"; rm -rf "$STAGE"; exit 1; }
# (pip writes either a plain shebang or an sh trampoline when the path is long; both name the interpreter)
{ grep -qF "$INSTALL_ROOT/venv/bin/python" "$STAGE/venv/bin/claude-unlimited" && ! grep -rqF "$STAGE" "$STAGE/venv/bin"; } ||
  { log "FAIL: staged launchers not relocated to the final venv path"; rm -rf "$STAGE"; exit 1; }
log "staged $EXPECT ($SHA) at $STAGE: preflight and import ok"

# ---- 3. optional: wait for an idle window ----------------------------------------------------
waited=0
while [ "$MAX_WAIT" -gt 0 ]; do
  idle="$(status_field idle_seconds)"; serving="$(status_field serving_now)"
  if [ -z "$idle" ] && [ -z "$(health_version)" ]; then log "gateway not answering; nothing to interrupt"; break; fi
  # idle_seconds is 0 while a request is open, else seconds since the last one; empty = none served yet.
  if [ "$serving" = "[]" ] || [ -z "$serving" ]; then
    if [ -z "$idle" ] || "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)' "$idle" "$IDLE_SECONDS"; then
      log "idle window found after ${waited}s (idle_seconds=${idle:-none})"; break
    fi
  fi
  if [ "$waited" -ge "$MAX_WAIT" ]; then log "no idle window in ${MAX_WAIT}s; proceeding anyway"; break; fi
  sleep 1; waited=$((waited + 1))
done

# ---- 4. the HUD binary does not change between these versions: don't churn it ----------------
hud_carry_over() {
  local old_stamp; old_stamp="$(cat "$INSTALL_ROOT/hud-version" 2>/dev/null)"
  [ -n "$old_stamp" ] && [ -d "$HOME/Applications/HUD - Heads-Up Display.app" ] || return 0
  local old_digest new_digest
  old_digest="$(awk -v a="HUD-$old_stamp-macos.zip" '$2==a {print $1}' "$INSTALL_ROOT/app/macos-widget/HUD.sha256" 2>/dev/null)"
  new_digest="$(awk -v a="HUD-$EXPECT-macos.zip" '$2==a {print $1}' "$STAGE/app/macos-widget/HUD.sha256" 2>/dev/null)"
  if [ -n "$old_digest" ] && [ "$old_digest" = "$new_digest" ]; then
    cp "$INSTALL_ROOT/hud-version" "$STAGE/hud-version.old"
    printf '%s' "$EXPECT" > "$INSTALL_ROOT/hud-version"
    log "HUD binary unchanged ($old_stamp -> $EXPECT, same digest): stamp carried over, HUD left running"
  fi
}

rollback() {
  log "ROLLING BACK: renaming the previous app and venv back and restarting on them"
  rm -rf "$INSTALL_ROOT/app.failed" "$INSTALL_ROOT/venv.failed"
  mv "$INSTALL_ROOT/app" "$INSTALL_ROOT/app.failed" 2>/dev/null
  mv "$INSTALL_ROOT/venv" "$INSTALL_ROOT/venv.failed" 2>/dev/null
  mv "$ROLL/app" "$INSTALL_ROOT/app"; mv "$ROLL/venv" "$INSTALL_ROOT/venv"
  [ -f "$STAGE/hud-version.old" ] && cp "$STAGE/hud-version.old" "$INSTALL_ROOT/hud-version"
  "$BIN_DIR/claude-unlimited" install --port "$PORT" >/dev/null 2>&1 || { "$BIN_DIR/claude-unlimited" start --port "$PORT" >/dev/null 2>&1 & }
  for _ in $(seq 1 60); do [ "$(health_version)" = "$OLD_VERSION" ] && break; sleep 1; done
  log "after rollback the gateway reports version: $(health_version)"
  log "RESULT: ROLLED BACK (failed tree kept at $INSTALL_ROOT/app.failed and venv.failed)"
  exit 2
}

# ---- 5. swap and restart, in one step --------------------------------------------------------
# Leave the checkout: a daemon started from here (the `start` fallback inside install, or a
# rollback) would otherwise import this directory's claude_unlimited ahead of the installed one.
cd / || exit 1
hud_carry_over
mkdir -p "$ROLL"
t_swap=$(date +%s)
mv "$INSTALL_ROOT/app" "$ROLL/app" && mv "$INSTALL_ROOT/venv" "$ROLL/venv" ||
  { log "FAIL: could not move the live install aside"; mv "$ROLL/app" "$INSTALL_ROOT/app" 2>/dev/null; exit 1; }
mv "$STAGE/app" "$INSTALL_ROOT/app" && mv "$STAGE/venv" "$INSTALL_ROOT/venv" || { log "FAIL: could not move the staged install into place"; rollback; }
"$BIN_DIR/claude-unlimited" install --port "$PORT" >"$STAGE/install.out" 2>&1; install_rc=$?
v=""; for _ in $(seq 1 120); do v="$(health_version)"; [ "$v" = "$EXPECT" ] && break; sleep 0.5; done
log "swap + restart: install rc=$install_rc, health version '${v}', $(( $(date +%s) - t_swap ))s from first rename to healthy"
if [ "$install_rc" -ne 0 ] || [ "$v" != "$EXPECT" ]; then tail -5 "$STAGE/install.out" | cut -c1-200; rollback; fi

# ---- 6. verify -------------------------------------------------------------------------------
ok=1
"$HERE/preflight-patches.sh" "$INSTALL_ROOT/app" >/dev/null && log "verify markers on installed app: ok" || { log "verify markers on installed app: FAIL"; ok=0; }
"$HERE/preflight-patches.sh" "$(site_packages)" >/dev/null && log "verify markers on installed venv: ok" || { log "verify markers on installed venv: FAIL"; ok=0; }
best=""; for _ in 1 2 3; do
  t="$(curl -fsS -o /dev/null -w '%{time_total}' --max-time 10 "$BASE/api/profiles" 2>/dev/null)" || t=""
  [ -n "$t" ] && { if [ -z "$best" ] || "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)' "$t" "$best"; then best="$t"; fi; }
  sleep 1
done
if [ -n "$best" ] && "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) < 1.0 else 1)' "$best"; then log "verify /api/profiles: ok (${best}s)"; else log "verify /api/profiles: FAIL (best '${best}'s, need <1s)"; ok=0; fi
proxied="$("$PY" - "$PORT" <<'PYEOF' 2>&1
import json, sys, urllib.request
from claude_unlimited.cli import _fetch_placeholder_token
port = int(sys.argv[1])
token = _fetch_placeholder_token("127.0.0.1", port)
h = {"authorization": "Bearer " + token, "anthropic-version": "2023-06-01", "content-type": "application/json"}
body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 16,
                   "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}).encode()
r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=body, headers=h), timeout=90)
j = json.load(r)
print("OK" if r.status == 200 and j.get("type") == "message" and j.get("content") else "BAD", r.status, j.get("stop_reason"))
PYEOF
)"
case "$proxied" in OK*) log "verify real proxied request: ok ($proxied)";; *) log "verify real proxied request: FAIL ($(printf '%s' "$proxied" | tail -1 | cut -c1-200))"; ok=0;; esac

[ "$ok" -eq 1 ] || rollback
log "RESULT: UPGRADED to $EXPECT ($SHA); previous install kept at $ROLL"

# ---- 7. informational: one tiny request pinned to each enabled profile (never rolls back) ------
"$PY" - "$PORT" <<'PYEOF' 2>&1 | while IFS= read -r line; do log "profile smoke: $line"; done
import json, sys, urllib.request, urllib.error, urllib.parse
port = int(sys.argv[1]); base = f"http://127.0.0.1:{port}"
profiles = json.load(urllib.request.urlopen(f"{base}/api/profiles", timeout=20))
profiles = profiles.get("profiles", profiles) if isinstance(profiles, dict) else profiles
for p in profiles:
    if not p.get("enabled"):
        print(f"{p.get('name')}: disabled, skipped"); continue
    try:
        tok = json.load(urllib.request.urlopen(f"{base}/api/session-token?profile_id={urllib.parse.quote(p['id'])}", timeout=10))["token"]
        h = {"authorization": "Bearer " + tok, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 16,
                           "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}).encode()
        r = urllib.request.urlopen(urllib.request.Request(f"{base}/v1/messages", data=body, headers=h), timeout=90)
        j = json.load(r)
        print(f"{p.get('name')} [{p.get('kind')}]: HTTP {r.status} {j.get('type')} stop={j.get('stop_reason')}")
    except urllib.error.HTTPError as e:
        try: err = json.loads(e.read()).get("error", {})
        except Exception: err = {}
        print(f"{p.get('name')} [{p.get('kind')}]: HTTP {e.code} {err.get('type', '')}")
    except Exception as e:
        print(f"{p.get('name')} [{p.get('kind')}]: {type(e).__name__}")
PYEOF
rm -rf "$W" "$STAGE"
exit 0

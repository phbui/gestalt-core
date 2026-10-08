#!/usr/bin/env bash
# search-probe.sh — re-verify which external search/fetch routes actually work.
#
# The blocked-domain map in gestalt/knowledge/<kb-entry>.md is a snapshot.
# Sites change robots.txt, archives go down, anti-bot walls go up. Run this to
# find out what is true today rather than trusting the entry.
#
# Usage: bash gestalt/tools/search-probe.sh
#
# Note: this probes only the curl-reachable routes. The WebSearch allowed_domains
# gate cannot be tested from a shell — an agent must test it by calling WebSearch
# with allowed_domains=["<domain>"] and observing a 400 vs a clean result.

set -uo pipefail

UA="Mozilla/5.0 (X11; Linux x86_64)"
pass=0; fail=0

probe() {
  local label="$1" url="$2" expect="${3:-200}"
  local code
  code=$(curl -sL -m 15 -A "$UA" -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "ERR")
  if [[ "$code" == "$expect" ]]; then
    printf '  \033[32mOK  \033[0m %-34s %s\n' "$label" "$code"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m %-34s %s (wanted %s)\n' "$label" "$code" "$expect"; fail=$((fail+1))
  fi
}

# Probe that checks the body is real content, not a challenge/interstitial page.
# Status code alone is not enough: old.reddit.com serves a 200 "Welcome to Reddit"
# logged-out gate, and several redlib mirrors serve a 200 Anubis bot challenge.
# Retries because arctic-shift rate-limits on burst ("Timeout. Maybe slow down a bit").
probe_body() {
  local label="$1" url="$2" needle="$3" tries="${4:-3}"
  local body i
  for ((i=1; i<=tries; i++)); do
    body=$(curl -sL -m 25 -A "$UA" "$url" 2>/dev/null || echo "")
    if grep -qi -- "$needle" <<<"$body"; then
      printf '  \033[32mOK  \033[0m %-34s content confirmed\n' "$label"; pass=$((pass+1)); return
    fi
    [[ $i -lt $tries ]] && sleep 4
  done
  local hint=""
  grep -qi 'slow down\|rate' <<<"$body" && hint=" [rate-limited]"
  grep -qi 'anubis\|not a bot\|verifying your browser' <<<"$body" && hint=" [bot challenge]"
  grep -qi 'welcome to reddit' <<<"$body" && hint=" [logged-out gate]"
  printf '  \033[31mFAIL\033[0m %-34s no match for %s%s\n' "$label" "$needle" "$hint"; fail=$((fail+1))
}

echo
echo "=== Reddit ==================================================="
echo "  (direct reddit.com is expected to FAIL — that is the point)"
probe "reddit.com .json (expect block)" "https://www.reddit.com/r/ClaudeAI/search.json?q=x&limit=1" 403
# old.reddit answers 200 but with a logged-out interstitial; the content check is
# what matters. If this ever reports OK, direct Reddit reading has become possible.
probe_body "old.reddit.com (expect FAIL)" "https://old.reddit.com/r/ClaudeAI/" 'class="thing' 1
probe_body "arctic-shift posts/search" \
  "https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=ClaudeAI&limit=2&sort=desc" '"title"'
probe_body "arctic-shift title filter" \
  "https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=ClaudeAI&title=websearch&limit=3" '"title"'
# Public Atom feed works unauthenticated even though /*.json on the same host 403s.
# Recent posts only, no search/scores, and it 429s on burst — hence the retries.
probe_body "reddit public RSS" "https://www.reddit.com/r/ClaudeAI/.rss" '<entry'

echo
echo "=== Q&A / dev community ======================================"
probe_body "StackExchange API (SO)" \
  "https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q=asyncio&site=stackoverflow&pagesize=2" '"question_id"'
probe_body "Hacker News (Algolia)" \
  "https://hn.algolia.com/api/v1/search?query=claude+code&tags=story&hitsPerPage=2" '"objectID"'
probe_body "Discourse (discuss.python.org)" \
  "https://discuss.python.org/search.json?q=asyncio" '"posts"'

echo
echo "=== Social ==================================================="
probe_body "X syndication (single tweet)" \
  "https://cdn.syndication.twimg.com/tweet-result?id=20&token=1" 'twttr'
probe "x.com search (JS shell only)"  "https://x.com/search?q=claude" 200
probe "linkedin.com (JS shell only)"  "https://www.linkedin.com/feed/" 200

echo
echo "=== GitHub ==================================================="
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh search issues "claude code" --limit 1 >/dev/null 2>&1; then
    printf '  \033[32mOK  \033[0m %-34s authenticated\n' "gh search issues"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m %-34s gh search failed\n' "gh search issues"; fail=$((fail+1))
  fi
else
  printf '  \033[33mSKIP\033[0m %-34s gh not installed or not authed\n' "gh search issues"
fi

echo
echo "=============================================================="
printf '  %d passed, %d failed\n' "$pass" "$fail"
echo
echo "  A FAIL on reddit.com/old.reddit is expected and healthy."
echo "  A FAIL on arctic-shift or the StackExchange API means the"
echo "  documented workaround has broken — update the knowledge entry."
echo

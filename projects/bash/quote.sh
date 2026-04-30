#!/usr/bin/env bash
set -euo pipefail

FEED_URL="http://feeds.feedburner.com/azquotes/quoteoftheday"

feed=$(curl -fsSL "$FEED_URL")

pairs=$(printf '%s' "$feed" | awk '
  /<item>/        { in_item = 1; author = ""; quote = ""; next }
  /<\/item>/      {
    if (in_item && author != "" && quote != "") print quote "\t" author
    in_item = 0; next
  }
  in_item && /<title>/ {
    line = $0
    sub(/.*<title>/, "", line)
    sub(/<\/title>.*/, "", line)
    author = line
  }
  in_item && /<description>/ {
    line = $0
    sub(/.*<description>/, "", line)
    sub(/<\/description>.*/, "", line)
    quote = line
  }
')

if [[ -z "$pairs" ]]; then
  echo "No quotes found in feed." >&2
  exit 1
fi

selected=$(printf '%s\n' "$pairs" | shuf -n 1)
quote=${selected%%$'\t'*}
author=${selected#*$'\t'}

printf '%s  --  %s\n' "$quote" "$author"

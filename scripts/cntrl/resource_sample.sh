#!/usr/bin/env bash
# Resource sampler for the Hermes desktop stack (WR audit). Read-only: ps snapshots only.
#
#   scripts/cntrl/resource_sample.sh [samples=6] [interval_s=5] [label]
#
# Prints one block per sample (pid, RSS MB, %CPU, elapsed, role, command) and a summary of
# mean RSS / mean CPU per role across the samples. Roles: renderer, electron-main,
# electron-gpu, electron-other, backend (hermes serve), sdk-cli (claude stream-json),
# mcp-child (hermes_tools_mcp_server), bg-spare (Claude Code spare pool), gateway,
# windowserver.
set -euo pipefail

samples="${1:-6}"
interval="${2:-5}"
label="${3:-sample}"

classify() {
  case "$1" in
    *WindowServer*) echo windowserver ;;
    # Only the Hermes desktop's own Electron (other Chromium/Electron apps also run --type=renderer).
    *hermes-cntrl*electron*"--type=renderer"*) echo renderer ;;
    *hermes-cntrl*electron*"--type=gpu-process"*) echo electron-gpu ;;
    *hermes-cntrl*electron*"--type="*) echo electron-other ;;
    *hermes-cntrl*node_modules*electron*) echo electron-main ;;
    *"hermes_cli.main"*"serve"*) echo backend ;;
    *"hermes_cli.main gateway"*) echo gateway ;;
    *"claude --output-format stream-json"*|*"/claude --output-format stream-json"*) echo sdk-cli ;;
    *hermes_tools_mcp_server*) echo mcp-child ;;
    *"claude bg-spare"*) echo bg-spare ;;
    *) echo "" ;;
  esac
}

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
echo "# $label $(date '+%Y-%m-%d %H:%M:%S') samples=$samples interval=${interval}s"
for i in $(seq 1 "$samples"); do
  echo "## sample $i $(date +%T)"
  ps -axo pid=,rss=,%cpu=,etime=,command= | while read -r pid rss cpu etime cmd; do
    role="$(classify "$cmd")"
    [ -n "$role" ] || continue
    printf '%7s %9.1fMB %6s %12s %-15s %s\n' "$pid" "$(echo "$rss/1024" | bc -l)" "$cpu" "$etime" "$role" "${cmd:0:90}"
    printf '%s %s %s\n' "$role" "$rss" "$cpu" >> "$tmp"
  done
  [ "$i" -lt "$samples" ] && sleep "$interval"
done
echo "## summary (per sample: total RSS MB and total %CPU per role, averaged over $samples samples)"
awk -v n="$samples" '{rss[$1]+=$2; cpu[$1]+=$3; cnt[$1]++}
  END {for (r in rss) printf "%-15s procs/sample=%5.1f  rss_mb=%9.1f  cpu_pct=%6.1f\n", r, cnt[r]/n, rss[r]/1024/n, cpu[r]/n}' "$tmp" | sort

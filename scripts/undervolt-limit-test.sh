#!/usr/bin/env bash
# undervolt-limit-test.sh — find the deepest stable undervolt for this CPU.
#
# Steps CORE+CACHE from START towards STOP. At every step it applies the
# offset (via uv-apply.py, which reuses throttled's own MSR encoding), then
# hunts for silent bit corruption: stress-ng --verify recomputes and checks
# every result, and the kernel log is scanned for machine-check events.
# A too-deep undervolt usually shows up as (in order of likelihood):
#   1. verified stress failure (silent corruption caught red-handed),
#   2. an MCE in the kernel log,
#   3. a hard freeze.
# The journal survives all three: every step is recorded on disk (fsync'd)
# BEFORE it runs, so after a freeze+reboot the next run marks the pending
# step as FAIL and reports the stable floor found so far.
#
# Usage (root; keep the machine on AC and otherwise idle):
#   sudo ./undervolt-limit-test.sh [START STOP STEP [SECONDS_PER_STEP]]
# Defaults: START=-60 STOP=-150 STEP=-10, 600 s per step. Values in mV.
# Suggested flow: coarse pass at -10, then rerun with a -5 step around the
# first failure. Final config value = deepest PASS + MARGIN (default 20 mV).
#
# State journal: .uv-limit-state next to this script (one "mV VERDICT epoch"
# line per attempt). Delete it to start a fresh campaign.

set -euo pipefail

START=${1:--60}
STOP=${2:--150}
STEP=${3:--10}
DURATION=${4:-600}
MARGIN=${MARGIN:-20}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STATE="$SCRIPT_DIR/.uv-limit-state"
APPLY="$SCRIPT_DIR/uv-apply.py"

[ "$(id -u)" -eq 0 ] || { echo "E: run as root (MSR writes)" >&2; exit 1; }
[ "$START" -le 0 ] && [ "$STOP" -lt "$START" ] && [ "$STEP" -lt 0 ] ||
    { echo "E: expected negative mV values with STOP < START and STEP < 0" >&2; exit 1; }

# Re-exec inside a nix shell if the tools are not in PATH (NixOS-friendly).
if ! command -v stress-ng >/dev/null || ! command -v python3 >/dev/null; then
    echo "I: fetching stress-ng/python3 via nix shell..."
    exec nix shell nixpkgs#stress-ng nixpkgs#python3 -c "$0" "$@"
fi

deepest_pass() { awk '$2 == "PASS" && $1 < best { best = $1 } END { print best+0 }' "$STATE" 2>/dev/null || true; }

report() {
    local pass suggest
    pass=$(deepest_pass)
    if [ -z "$pass" ] || [ "$pass" -eq 0 ]; then
        echo "I: no PASS recorded yet."
        return
    fi
    suggest=$((pass + MARGIN))
    echo "I: deepest verified PASS: ${pass} mV -> suggested config value: ${suggest} mV"
    echo "I: set CORE/CACHE in nixos/throttled.nix to ${suggest} and rebuild."
}

# A pending ATTEMPT in the journal means the previous run died mid-step
# (hard freeze): record the verdict and stop for a human decision.
if [ -f "$STATE" ] && [ "$(tail -1 "$STATE" | awk '{print $2}')" = "ATTEMPT" ]; then
    crashed=$(tail -1 "$STATE" | awk '{print $1}')
    sed -i '$d' "$STATE"
    echo "$crashed FAIL $(date +%s)" >>"$STATE" && sync "$STATE"
    echo "W: previous run died while testing ${crashed} mV -> recorded as FAIL."
    report
    exit 0
fi

# Park the daemon so it cannot re-apply its own offsets mid-test.
throttled_was_active=0
if systemctl is-active --quiet throttled.service; then
    throttled_was_active=1
    systemctl stop throttled.service
fi
restore() {
    # Back to a safe offset, then hand the hardware back to the daemon.
    "$APPLY" 0 CORE CACHE >/dev/null || true
    if [ "$throttled_was_active" -eq 1 ]; then
        systemctl start throttled.service || true
    fi
    report
}
trap restore EXIT

nproc_count=$(nproc)
for ((mv = START; mv >= STOP; mv += STEP)); do
    grep -q "^$mv PASS" "$STATE" 2>/dev/null && { echo "I: ${mv} mV already PASSed, skipping"; continue; }
    echo "$mv ATTEMPT $(date +%s)" >>"$STATE" && sync "$STATE"
    echo "I: testing ${mv} mV for ${DURATION}s..."
    "$APPLY" "$mv" CORE CACHE
    step_start=$(date +%s)

    verdict=PASS
    # Two verified workloads: matrixprod (FP/cache heavy, the classic
    # undervolt killer) then fft. --verify makes corruption a hard failure.
    for method in matrixprod fft; do
        if ! stress-ng --cpu "$nproc_count" --cpu-method "$method" --verify \
            --timeout "$((DURATION / 2))s" --metrics-brief 2>&1 | tail -2; then
            echo "W: stress-ng --verify FAILED (${method}): silent corruption at ${mv} mV"
            verdict=FAIL
            break
        fi
        if journalctl -k -S "@$step_start" -q | grep -iE 'mce|machine check|hardware error'; then
            echo "W: machine-check events in the kernel log at ${mv} mV"
            verdict=FAIL
            break
        fi
    done

    sed -i '$d' "$STATE"
    echo "$mv $verdict $(date +%s)" >>"$STATE" && sync "$STATE"
    if [ "$verdict" = FAIL ]; then
        echo "I: limit found at ${mv} mV."
        break
    fi
done

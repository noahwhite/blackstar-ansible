#!/bin/sh
# Managed by Ansible (ed800g9 role) — do not edit.
#
# Disable the named deep CPU idle states on every CPU. Matched by NAME, not by
# stateN index: the index-to-name mapping is identical on all three ed800g9
# today (state3=C8, state4=C10) but a BIOS or microcode update can reorder it,
# and silently disabling the wrong state is worse than doing nothing.
#
# Exits 0 even when nothing matches. This unit must never latch `failed`: a
# stale failed oneshot is indistinguishable from a live one in
# node_systemd_unit_state and would page forever (the OFF-727 trap).
set -eu

[ "$#" -gt 0 ] || { echo "limit-deep-cstates: no states requested"; exit 0; }

changed=0
for idle in /sys/devices/system/cpu/cpu[0-9]*/cpuidle; do
    [ -d "$idle" ] || continue
    for state in "$idle"/state*/; do
        [ -r "$state/name" ] || continue
        name=$(cat "$state/name")
        for want in "$@"; do
            [ "$name" = "$want" ] || continue
            if echo 1 > "$state/disable" 2>/dev/null; then
                changed=$((changed + 1))
            else
                echo "limit-deep-cstates: WARNING could not disable $name at $state" >&2
            fi
        done
    done
done

if [ "$changed" -eq 0 ]; then
    echo "limit-deep-cstates: WARNING no cpuidle states matched: $*" >&2
else
    echo "limit-deep-cstates: disabled $changed cpuidle state(s) matching: $*"
fi
exit 0

#!/bin/sh
# Managed by Ansible (alloy role) — do not edit.
#
# Emit PCIe AER (Advanced Error Reporting) counters as node_exporter textfile
# metrics. Correctable RxErr/BadTLP counts climb for days before the atlantic
# Thunderbolt NIC throws a fatal, unrecoverable AER that drops the node off the
# Ceph net (PD #29, OFF-633) — they are the leading indicator, and nothing was
# scraping them.
#
# Cardinality: 14 AER-capable devices x 3 severities = 42 series per node, 126
# across the three ed800g9. ~1.3% of the Grafana Cloud free-tier 10k active
# series cap (see NWSOC-21 trims in config.alloy.j2). Only the per-device TOTAL
# is emitted, not the ~9 individual error-type fields, which would be 9x that
# for no extra alerting value.
#
# Usage: pcie-aer-textfile.sh <output.prom>
set -eu

out="$1"
dir="$(dirname "$out")"
tmp="$(mktemp "$dir/.pcie-aer.XXXXXX")"
# shellcheck disable=SC2064
trap "rm -f '$tmp'" EXIT

{
    echo '# HELP node_pcie_aer_errors_total PCIe AER error counters from sysfs, per device and severity.'
    echo '# TYPE node_pcie_aer_errors_total counter'

    for devpath in /sys/bus/pci/devices/*; do
        [ -r "$devpath/aer_dev_correctable" ] || continue
        bdf="$(basename "$devpath")"

        driver="none"
        if [ -L "$devpath/driver" ]; then
            driver="$(basename "$(readlink -f "$devpath/driver")")"
        fi

        for severity in correctable nonfatal fatal; do
            f="$devpath/aer_dev_$severity"
            [ -r "$f" ] || continue
            # Each file ends with a TOTAL_ERR_{COR,NONFATAL,FATAL} <n> line.
            total="$(awk '/^TOTAL_ERR_/ { print $2; exit }' "$f")"
            [ -n "$total" ] || continue
            printf 'node_pcie_aer_errors_total{device="%s",driver="%s",severity="%s"} %s\n' \
                "$bdf" "$driver" "$severity" "$total"
        done
    done
} >"$tmp"

chmod 644 "$tmp"
# Atomic swap so the collector never reads a half-written file.
mv "$tmp" "$out"
trap - EXIT

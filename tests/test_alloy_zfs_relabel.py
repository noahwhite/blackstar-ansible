#!/usr/bin/env python3
"""Behaviour of the Alloy `friendly_names` relabel rules on node_zfs_* series (OFF-1446).

The template is rendered with Jinja2, the rules of `prometheus.relabel
"friendly_names"` are parsed out of it, and Prometheus relabel semantics are
applied to one series per measured node_zfs_* name plus a few non-ZFS series.
Only the six dashboarded ZFS names may survive, with their labels unchanged, and
no series may leave the relabel stage carrying the temporary keep label.

If ALLOY_BIN points at an Alloy binary, the rendered config is also checked with
`alloy validate`. Run with
    python3 -m unittest discover -s tests -v
"""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import jinja2

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "roles" / "alloy" / "templates" / "config.alloy.j2"
ZFS_NAMES = ROOT / "tests" / "fixtures" / "node_zfs_metric_names.txt"

KEPT_ZFS = {
    "node_zfs_arc_size",
    "node_zfs_arc_c_max",
    "node_zfs_arc_c_min",
    "node_zfs_arc_hits",
    "node_zfs_arc_misses",
    "node_zfs_zpool_state",
}
NODES = [f"pve0{i}" for i in range(1, 8)]


def render():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(TEMPLATE.read_text()).render(
        alloy_textfile_dir="/var/lib/alloy/textfile_collector",
        alloy_scrape_interval="60s",
        alloy_remote_write_url="https://example.invalid/api/prom/push",
        alloy_remote_write_username="0",
        alloy_remote_write_password="not-a-secret",
    )


def component_body(config, header):
    start = config.index(header)
    i = config.index("{", start) + 1
    depth = 1
    while depth:
        if config[i] == "{":
            depth += 1
        elif config[i] == "}":
            depth -= 1
        i += 1
    return config[config.index("{", start) + 1 : i - 1]


def parse_rules(body):
    rules = []
    for block in re.findall(r"^\s*rule\s*\{(.*?)^\s*\}", body, re.S | re.M):
        rule = {}
        for line in block.splitlines():
            line = line.split("//", 1)[0].strip()
            if not line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            rule[key] = json.loads(value)
        rules.append(rule)
    return rules


def expand(template, match):
    return re.sub(
        r"\$\{?(\d+)\}?", lambda m: match.group(int(m.group(1))) or "", template
    )


def apply(rules, labels):
    """Prometheus relabel semantics for replace, drop, keep and labeldrop."""
    labels = dict(labels)
    for rule in rules:
        action = rule.get("action", "replace")
        regex = re.compile("^(?:" + rule.get("regex", "(.*)") + ")$")
        if action == "labeldrop":
            labels = {k: v for k, v in labels.items() if not regex.match(k)}
            continue
        value = rule.get("separator", ";").join(
            labels.get(name, "") for name in rule.get("source_labels", [])
        )
        match = regex.match(value)
        if action == "drop" and match:
            return None
        if action == "keep" and not match:
            return None
        if action == "replace" and match:
            new = expand(rule.get("replacement", "$1"), match)
            if new:
                labels[rule["target_label"]] = new
            else:
                labels.pop(rule["target_label"], None)
    return labels


def series(metric, **extra):
    return {"__name__": metric, "instance": "pve01", "job": "integrations/unix", **extra}


class FriendlyNamesZfsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = render()
        cls.rules = parse_rules(component_body(cls.config, 'prometheus.relabel "friendly_names"'))
        cls.zfs_names = [
            line.strip()
            for line in ZFS_NAMES.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def test_fixture_covers_the_measured_names(self):
        self.assertEqual(len(self.zfs_names), 321)
        self.assertTrue(KEPT_ZFS <= set(self.zfs_names))

    def test_only_the_six_dashboarded_zfs_names_survive(self):
        survivors = {
            name for name in self.zfs_names if apply(self.rules, series(name)) is not None
        }
        self.assertEqual(survivors, KEPT_ZFS)

    def test_unmeasured_zfs_names_are_dropped_too(self):
        # The drop is a prefix match, not a copy of the fixture: names that were
        # not active when the fixture was measured must go as well.
        for name in ("node_zfs_future_counter", "node_zfs_arc_size_bytes", "node_zfs_zpool_dataset_nread"):
            with self.subTest(name=name):
                self.assertIsNone(apply(self.rules, series(name)))

    def test_kept_zfs_series_keep_their_exact_label_set_on_every_node(self):
        for node in NODES:
            samples = [
                series(name, instance=node) for name in sorted(KEPT_ZFS - {"node_zfs_zpool_state"})
            ]
            samples += [
                series("node_zfs_zpool_state", instance=node, state=state, zpool=pool)
                for pool in ("rpool", "tank")
                for state in ("degraded", "faulted", "offline", "online", "removed", "suspended", "unavail")
            ]
            for labels in samples:
                with self.subTest(labels=labels):
                    self.assertEqual(apply(self.rules, labels), labels)

    def test_zfs_rules_select_on_name_and_mark_only(self):
        zfs_rules = [r for r in self.rules if "zfs" in r.get("regex", "")]
        self.assertEqual(len(zfs_rules), 3)
        for rule in zfs_rules:
            self.assertLessEqual(set(rule.get("source_labels", [])), {"__name__", "__tmp_zfs_keep"})

    def test_zfs_comment_records_count_date_and_why_three_rules(self):
        body = component_body(self.config, 'prometheus.relabel "friendly_names"')
        for text in ("OFF-1446", "3,231", "2026-11-08", "RE2", "negative", "lookahead"):
            with self.subTest(text=text):
                self.assertIn(text, body)

    def test_no_series_leaves_with_a_temporary_label(self):
        samples = [series(name) for name in self.zfs_names] + [
            series("node_load1"),
            series("node_hwmon_temp_celsius", chip="platform_coretemp_0", sensor="temp1"),
            series("node_pcie_aer_errors_total", device="0000:08:00.0", driver="atlantic", severity="correctable"),
            series("node_network_receive_bytes_total", device="enp8s0"),
        ]
        for labels in samples:
            out = apply(self.rules, labels)
            if out is not None:
                self.assertFalse(
                    [k for k in out if k.startswith("__") and k != "__name__"], out
                )

    def test_non_zfs_series_are_untouched(self):
        for labels in (
            series("node_load1"),
            series("node_pcie_aer_errors_total", device="0000:08:00.0", driver="atlantic", severity="correctable"),
            series("node_network_receive_bytes_total", device="enp8s0"),
            series("node_systemd_unit_state", name="ssh.service", state="failed"),
            series("zfs_like_but_not_node_zfs_arc_size"),
        ):
            with self.subTest(labels=labels):
                self.assertEqual(apply(self.rules, labels), labels)

    def test_existing_drops_still_apply(self):
        for name in ("node_scrape_collector_success", "node_cpu_scaling_governor"):
            self.assertIsNone(apply(self.rules, series(name)))
        self.assertIsNone(
            apply(self.rules, series("node_systemd_unit_state", name="ssh.service", state="active"))
        )

    @unittest.skipUnless(os.environ.get("ALLOY_BIN"), "ALLOY_BIN not set")
    def test_rendered_config_passes_alloy_validate(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "config.alloy"
            path.write_text(self.config)
            result = subprocess.run(
                [os.environ["ALLOY_BIN"], "validate", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""OFF-904 regression guard for the github-runner-vm cloud-init template.

The runner VMs join Tailscale on a reusable, ephemeral tag:officina-ci key. An
ephemeral node that stays offline past the tailnet reap window is deleted
server-side; if `tailscale up` only ran on first boot, the rebooted VM would sit
in NeedsLogin and fall off the tailnet, breaking every `docker run --network
host` provisioning step that resolves scarif.<tailnet>.

The durable fix is a systemd unit that re-runs `tailscale up` on EVERY boot,
deliberately WITHOUT the officina host modules' first-boot-only
`ConditionPathExists=!/var/lib/tailscale/tailscaled.state` guard. These tests
fail if that unit is removed or regains the first-boot guard.

The repo has no test harness or CI, so this is a dependency-free stdlib unittest
(the lightest durable check): run with
    python3 -m unittest discover -s tests -v
"""
import pathlib
import re
import unittest

TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "roles"
    / "github-runner-vm"
    / "templates"
    / "user-data.yaml.j2"
)


class ReauthUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = TEMPLATE.read_text()
        # Directives only: drop comment lines so explanatory prose that mentions
        # a construct (e.g. the first-boot guard we deliberately omit) cannot be
        # mistaken for the construct itself.
        cls.directives = "\n".join(
            ln
            for ln in cls.text.splitlines()
            if not ln.lstrip().startswith("#")
        )

    def test_template_exists(self):
        self.assertTrue(TEMPLATE.is_file(), f"missing template: {TEMPLATE}")

    def test_reauth_systemd_unit_is_written(self):
        self.assertIn(
            "/etc/systemd/system/tailscale-auth.service",
            self.text,
            "the boot re-auth systemd unit file must be written by cloud-init",
        )

    def test_unit_runs_tailscale_up_with_the_authkey(self):
        self.assertRegex(
            self.text,
            r"ExecStart=/usr/bin/tailscale up[^\n]*--authkey="
            r"[^\n]*officina_ci_tailscale_authkey",
            "the unit must run `tailscale up` with the officina auth key",
        )

    def test_unit_is_not_first_boot_guarded(self):
        # The whole point of OFF-904: the unit must re-run on every boot, so it
        # must NOT carry the host modules' first-boot-only guard.
        self.assertNotIn(
            "ConditionPathExists=!/var/lib/tailscale/tailscaled.state",
            self.directives,
            "re-auth unit must not be guarded to first boot only "
            "(would reintroduce the OFF-904 reap failure)",
        )

    def test_unit_is_enabled_on_boot(self):
        self.assertRegex(
            self.text,
            r"systemctl enable --now tailscale-auth\.service",
            "cloud-init must enable+start the re-auth unit",
        )
        self.assertRegex(
            self.text,
            r"WantedBy=multi-user\.target",
            "the unit must be wired into a boot target so it fires each boot",
        )

    def test_no_bare_first_boot_only_tailscale_up_in_runcmd(self):
        # A bare `tailscale up` line in runcmd (the pre-OFF-904 shape) runs only
        # at first boot and would leave the reap unrecovered. The invocation must
        # live in the unit instead.
        runcmd = self.text.split("runcmd:", 1)[-1]
        bare = [
            ln
            for ln in runcmd.splitlines()
            if re.search(r"^\s*-\s*tailscale up\b", ln)
        ]
        self.assertEqual(
            bare, [], f"tailscale up must not be a bare first-boot runcmd: {bare}"
        )


if __name__ == "__main__":
    unittest.main()

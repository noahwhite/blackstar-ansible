# Cluster Power-Off / Power-On Runbook

Safe procedures for fully shutting down and bringing back the Blackstar Proxmox +
Ceph cluster (e.g. for a house power event, rack work, or relocation).

The golden rule for shutdown is **guests off → Ceph frozen → all hosts fully
powered off → only then the switches**. Powering the CRS305 (10G Ceph fabric)
down while OSDs are still alive parks them in `failed` (see
[Troubleshooting](#troubleshooting)).

## Prerequisites

- Run everything from **blacksun** (`192.168.1.169`), the control host.
- SSH key `~/.ssh/id_ed25519_blackstar_ansible` present; the `ansible` service
  account has **NOPASSWD sudo** on every node.
- Alternatively use the Proxmox web UI (`https://pve05.home.arpa:8006`, realm
  **Linux PAM**, user `noah`) — UI equivalents are noted inline.

## Topology (what matters for power)

| Component | Address | Role | Power note |
|-----------|---------|------|------------|
| pve01–pve04 | `pve0N.home.arpa` | m75q nodes — **no Ceph OSDs/mons** | Shut down in any order |
| pve05 | `pve05.home.arpa` / `10.10.10.5` | ed800g9 — Ceph mon+mgr+2 OSDs, cluster master | Power off **last**, boot **first** |
| pve06 | `pve06.home.arpa` / `10.10.10.6` | ed800g9 — Ceph mon+mgr+2 OSDs | — |
| pve07 | `pve07.home.arpa` / `10.10.10.7` | ed800g9 — Ceph mon+mgr+2 OSDs | — |
| CRS305 | `192.168.1.190` | 10G Ceph switch (RouterOS) | **Power off** on shutdown; **on first** at startup |
| CSS610 | `192.168.1.195` | 1G mgmt switch (SwOS Lite) | **Leave powered on** — do not touch |

- Ceph: 3 mons / 3 mgrs / **6 OSDs** on pve05–07 only, pool `vm-storage`
  (size 3 / min_size 2), public+cluster network `10.10.10.0/24` over the CRS305.
- The ed800g9 10G links are **Thunderbolt SFP+ NICs** on a **passive DAC** to the
  CRS305 — the DAC carries no power (see the standby-LED note in shutdown step 5).

---

## Power-Off Procedure

### 1. Shut down all guests (every node)

```bash
for n in pve01 pve02 pve03 pve04 pve05 pve06 pve07; do
  ssh -o ConnectTimeout=8 -i ~/.ssh/id_ed25519_blackstar_ansible ansible@$n.home.arpa \
    'sudo bash -c '\''for v in $(qm list  | awk "NR>1{print \$1}"); do qm shutdown  $v --timeout 120 & done; \
                     for c in $(pct list | awk "NR>1{print \$1}"); do pct shutdown $c & done; wait'\'''
done
```

- UI equivalent: per node → **More ▾ → Bulk Shutdown** → select all → Shutdown.
- If a guest hangs, force it: swap `qm shutdown $v` → `qm stop $v`.
- Paste the block verbatim — the `'\''` sequences are the intentional `bash -c`
  single-quote escaping.

### 2. Freeze Ceph (once, from any ed800g9 node — pve05 shown)

```bash
ssh -i ~/.ssh/id_ed25519_blackstar_ansible ansible@pve05.home.arpa \
  'sudo ceph osd set noout && sudo ceph osd set norebalance && \
   sudo ceph osd set nobackfill && sudo ceph osd set norecover && sudo ceph -s'
```

- Expect `HEALTH_WARN` with `noout,nobackfill,norebalance,norecover flag(s) set`
  and all PGs `active+clean`. That WARN is **only** the flags — it is correct.
- UI equivalent: pve05 → **Ceph → OSD → Manage Global Flags** → tick `noout`,
  `norebalance`, `nobackfill`, `norecover`.

### 3. Power off the hosts — m75qs first, pve05 **last**

The m75qs hold no Ceph, so they go first. Among the ed800g9 nodes, pve05 (master)
goes last so a mon quorum answers while pve06/07 leave.

```bash
# m75q nodes (tolerant of any already off)
for n in pve01 pve02 pve03 pve04; do
  ping -c1 -W1 $n.home.arpa >/dev/null 2>&1 \
    && ssh -o ConnectTimeout=8 -i ~/.ssh/id_ed25519_blackstar_ansible ansible@$n.home.arpa sudo shutdown -h now \
    || echo "$n not reachable, skipping"
done

# ed800g9 nodes, pve05 last — wait for each to go dark
for n in pve07 pve06 pve05; do
  ssh -i ~/.ssh/id_ed25519_blackstar_ansible ansible@$n.home.arpa sudo shutdown -h now
  until ! ping -c1 -W1 $n.home.arpa >/dev/null 2>&1; do sleep 3; done; echo "$n down"
done
```

- UI equivalent: select each node → top-right **Shutdown**. Drive all shutdowns
  from the pve05 tab and do pve05 itself last.

### 4. Power off the CRS305 (leave CSS610 on)

Once **all** nodes are dark, cut power to the CRS305 at the plug/PDU.
RouterOS has no true soft power-off (`/system shutdown` only halts the CPU; the
unit stays energized and needs a power cycle to return), so just pull the plug —
its config is already in non-volatile storage. **Do not power off the CSS610.**

### 5. (Optional) Pull the node power bricks from mains for true-zero draw

A "powered down" node is in soft-off (S5); with its brick still in the wall the
**+5V standby rail stays live**, and the Thunderbolt/USB-C ports keep feeding
standby power — which keeps the 10G NIC's blue power LED lit even with the host
off and the switch unplugged. To make everything truly dark (and stop the
sub-watt no-load brick draw), unplug the node power bricks from the **wall**.

---

## Power-On Procedure

Reverse order. Bring the network back **before** the OSD nodes so Ceph never
starts against a missing fabric.

### 1. CRS305 first

Power on the CRS305 and wait for its SFP+ link LEDs to come up (~30–60 s).

### 2. Boot the ed800g9 nodes — pve05 first

Plug the G9 bricks back into mains and power on **pve05**, then **pve06** and
**pve07**. Wait for each to answer:

```bash
for n in pve05 pve06 pve07; do
  until ping -c1 -W1 $n.home.arpa >/dev/null 2>&1; do sleep 3; done; echo "$n up"
done
```

### 3. Boot the m75q nodes

Power on pve01–pve04 (any order).

### 4. Verify Ceph health and the fabric

```bash
ssh -i ~/.ssh/id_ed25519_blackstar_ansible ansible@pve05.home.arpa \
  'sudo ceph -s; echo ---; ping -c2 -I enp8s0 10.10.10.1'
```

- Expect mon quorum `pve05,pve06,pve07`, 6 OSDs `up`/`in`, and the Ceph gateway
  ping succeeding. Health will still show the flags as WARN until step 5.

### 5. Unset the Ceph flags

```bash
ssh -i ~/.ssh/id_ed25519_blackstar_ansible ansible@pve05.home.arpa \
  'sudo ceph osd unset noout && sudo ceph osd unset norebalance && \
   sudo ceph osd unset nobackfill && sudo ceph osd unset norecover && sudo ceph -s'
```

Health should return to `HEALTH_OK` with all PGs `active+clean`.
UI equivalent: pve05 → **Ceph → OSD → Manage Global Flags** → untick all four.

### 6. Start guests

Start the VMs/CTs (UI **Bulk Start**, or `qm start` / `pct start` per guest).

---

## Troubleshooting

### OSD or mgr stuck `failed` after boot

If an ed800g9 node comes up before the Ceph fabric is ready (late CRS305, cable
work), its OSD/mgr units fail to fetch the mon config, retry, hit systemd's
start-limit (`Start request repeated too quickly`), and get parked in `failed` —
they do **not** auto-recover when the network returns (the mon does). Once the
Ceph link is confirmed back (`ping -I enp8s0 10.10.10.1`), on the affected node:

```bash
sudo systemctl reset-failed ceph-osd@<N> ceph-osd@<M> ceph-mgr@<host>
sudo systemctl start       ceph-osd@<N> ceph-osd@<M> ceph-mgr@<host>
```

Data stays safe (`min_size 2` holds; PGs `active+undersized`) as long as only one
host is affected.

### `ceph -s` shows fewer than 3 mons

Give the last-booted node another minute; mons re-form quorum on their own once
all three are reachable on `10.10.10.0/24`. If one never joins, check that node's
`enp8s0` (`ip a show enp8s0`, MTU 9000) and its CRS305 SFP+ link.

# Cluster Provisioning Runbook

End-to-end procedure for provisioning Proxmox VE nodes via PXE and configuring them with Ansible.

## Prerequisites

- blacksun (192.168.1.169) running the PXE stack (`docker compose up -d` in blackstar-pxe-stack)
- Ansible installed on blacksun (`~/.ansible-venv/bin/ansible`)
- blackstar-ansible repo cloned to `~/blackstar-ansible`
- SSH key `~/.ssh/id_ed25519_blackstar_ansible` present on blacksun
- Node MAC address registered in `blackstar-pxe-stack/boot/proxmox/answers/mac-mapping.json`
- Matching answer file exists (e.g., `m75q-pve01.toml`)

## Node Inventory

| Node | FQDN | MAC | Hardware | Answer File |
|------|------|-----|----------|-------------|
| pve01 | pve01.home.arpa | E0:4F:43:E8:0A:F1 | Lenovo M75q Gen 2 | m75q-pve01.toml |
| pve02 | pve02.home.arpa | TBD | Lenovo M75q Gen 2 | m75q-pve02.toml |
| pve03 | pve03.home.arpa | 04:7B:CB:43:32:39 | Lenovo M75q Gen 2 | m75q-pve03.toml |
| pve04 | pve04.home.arpa | 38:7C:76:4B:D2:FC | Lenovo M75q Gen 2 | m75q-pve04.toml |

## Phase 1: PXE Install

### Per node:

1. Set BIOS boot order to PXE first (or use F12 one-time boot menu)

2. Power on — node PXE boots automatically:
   - iPXE chainloads from blacksun
   - Proxmox installer fetches answer file via MAC match
   - Automated install runs (~5-10 min)
   - Node powers off when complete

3. Set BIOS boot order to disk first

4. Power on — first-boot runs automatically:
   - Removes enterprise repos, adds no-subscription repo
   - Creates `ansible` service account with SSH key
   - Installs Ansible in venv
   - Clones blackstar-ansible and runs `site.yml` locally
   - Locks root account, hardens SSH

5. Verify from blacksun:
   ```bash
   ssh ansible@<node>.home.arpa "hostname -f && dpkg -l htop | grep '^ii' && systemctl is-active chronyd"
   ```

### Repeat for all nodes before proceeding to Phase 2.

## Phase 2: Verify Ansible Baseline

From blacksun, confirm all nodes are reachable and correctly configured:

```bash
cd ~/blackstar-ansible
ansible all -m ping
```

Run the full playbook in check mode to verify no drift:

```bash
ansible-playbook site.yml --check --diff
```

Expected result: `changed=0` on all nodes.

## Phase 3: Cluster Formation

The `proxmox-cluster` role creates the cluster on pve01 (master) and joins other nodes sequentially.

### Pre-requisites

SSH host key trust between nodes. From pve01, accept each node's host key:

```bash
ssh ansible@pve01.home.arpa
sudo ssh pve02.home.arpa exit
sudo ssh pve03.home.arpa exit
sudo ssh pve04.home.arpa exit
```

### Run cluster playbook

From blacksun:

```bash
cd ~/blackstar-ansible
ansible-playbook site.yml --tags cluster
```

Or run only the cluster role:

```bash
ansible-playbook -i inventories/homelab/hosts.yml roles/proxmox-cluster/tasks/main.yml
```

### Verify cluster

```bash
ssh ansible@pve01.home.arpa "sudo pvecm status"
```

Expected: all nodes listed as members, quorum established.

## Phase 4: Post-Cluster Configuration

After cluster formation, configure via the Proxmox UI at `https://pve01.home.arpa:8006`:

- Storage configuration (ZFS pools, NFS/CIFS mounts)
- Network bridges
- Backup schedules
- HA groups (if applicable)

These settings propagate across the cluster automatically.

## Troubleshooting

### PXE boot fails — no answer file match
Check `docker compose logs pxe | grep answer-server` on blacksun. Verify the node's MAC is in `mac-mapping.json`.

### First-boot script fails
SSH in as root (`changeme`) and check:
```bash
journalctl -u proxmox-first-boot.service --no-pager
journalctl -u proxmox-first-boot-multi-user.service --no-pager
```

### Ansible can't reach a node
```bash
ssh -i ~/.ssh/id_ed25519_blackstar_ansible ansible@<node>.home.arpa
```
If this fails, first-boot didn't complete. SSH as root and run the script manually.

### Cluster join fails
Verify SSH host key trust between pve01 and the joining node. Check `pvecm status` on both nodes.

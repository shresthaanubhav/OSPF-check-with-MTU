# OSPF Monitor

Automated OSPF neighbor state monitoring across Cisco IOS routers using Netmiko.

## What it does

- Connects to all routers via SSH using Netmiko
- Pulls `show ip ospf neighbor` from each device
- Parses neighbor states — flags anything not in FULL state
- Pulls `show ip route ospf` to verify route propagation
- Detects failure scenarios: EXSTART (MTU mismatch), INIT (one-way communication), LOADING (LSA issues)

## Why it matters

In a production NOC, OSPF neighbor drops cause routing failures and outages. This script catches state degradation before it becomes a customer-facing problem — same core logic used by tools like SolarWinds and Cisco DNA Center.

## Tech Stack

- Python 3
- Netmiko 4.x
- PyYAML
- Cisco IOSv (GNS3 lab)
- GitHub Actions CI/CD

## Project Structure
network-ospf-monitor/

├── monitor.py          # main monitoring script

├── devices.yaml        # device inventory

├── requirements.txt    # Python dependencies

├── ospf_log.txt        # output log

└── .github/

└── workflows/

└── ci.yaml     # syntax validation pipeline

## How to run

```bash
pip install -r requirements.txt
python monitor.py
```

## OSPF States Reference

| State | Meaning | Common Cause |
|---|---|---|
| FULL | Healthy adjacency | — |
| EXSTART | DBD exchange failing | MTU mismatch |
| INIT | One-way communication | ACL blocking multicast |
| LOADING | LSA exchange incomplete | Unstable link |
| 2-WAY | Non-DR/BDR relationship | Normal on broadcast |

## Lab Environment

GNS3 lab running Cisco IOSv routers on EndeavourOS (Arch Linux).

## Author

Anubhav Shrestha — github.com/shresthaanubhav

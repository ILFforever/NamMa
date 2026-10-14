# Huawei Cloud setup

The console handles one-time setup; `hcloud` (KooCLI) handles what repeats. Read the docs for a service before scripting it (CLAUDE.md §11).
**Ask the team before running anything that creates, changes or deletes resources. It spends the $200 credit.**

## 1. KooCLI
Install: see CLAUDE.md §11. Then:
```bash
hcloud version            # first run: accept the privacy statement
hcloud configure init     # IAM sub-user AK/SK + region (AP-Bangkok)
hcloud configure list
```

## 2. Console checklist (owner: ______)
- [ ] Region: AP-Bangkok. Record the region ID in `.env` as `HW_REGION`.
- [ ] IAM sub-user with programmatic access → AK/SK into `.env`
- [ ] Billing Center: budget alerts at $50 and $100
- [ ] MaaS: enable, list models available in Bangkok, create API key → `MAAS_*` in `.env`
- [ ] Check: does the $200 credit cover ECS/RDS/OBS or only MaaS?
- [ ] OBS bucket (pay-per-use)
- [ ] Day 3: ECS VM (small, Ubuntu, pay-per-use, EIP billed by traffic, SG opens 22 from your IP + 443)
- [ ] Day 3: RDS for PostgreSQL (smallest single node, pay-per-use, same VPC as ECS)

## 3. Outside Huawei
- [ ] LINE Messaging API channel → `LINE_CHANNEL_*`
- [ ] LINE Login channel → `LINE_LOGIN_*`
- [ ] GISTDA API key (https://api-gateway.gistda.or.th) → `GISTDA_API_KEY`

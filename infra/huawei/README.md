# Huawei Cloud setup (console)

All setup is done in the web console: by a teammate, or by Claude in Chrome on their machine. There is no CLI step
(decision 2026-10-17, CLAUDE.md §11). The app calls no Huawei API with account keys, so no AK/SK goes on the VM.
Read the docs for a service before changing it.
**Ask the team before anything that creates, changes or deletes resources. It spends the $200 credit.**

## 1. Account (owner: ______)
- [ ] IAM sub-user for console work, limited to ECS, EIP, VPC, RDS, OBS, MaaS. Don't use the main account day to day.
- [ ] Region: AP-Bangkok. Record the region ID in `.env` as `HW_REGION`.
- [ ] Billing Center: budget alerts at $50 and $100
- [ ] Check: does the $200 credit cover ECS/RDS/OBS or only MaaS?

## 2. Services
- [ ] MaaS: enable, list models available in Bangkok, create API key → `MAAS_*` in `.env`
- [ ] ECS VM: small, Ubuntu 24.04, **pay-per-use**, EIP **billed by traffic**, security group opens 22 from your IP
      and 80/443. Then follow `infra/deploy/README.md`.
- [ ] Later, optional: OBS bucket (pay-per-use) for raw archives; RDS for PostgreSQL (smallest single node, same VPC)

## 3. Outside Huawei
- [ ] LINE Messaging API channel → `LINE_CHANNEL_*`. Profile picture: `namma-web/public/brand/namma-line-profile.png`
      (the logo, บ้านบนคลื่น); OA name "น้ำมา NamMa".
- [ ] LINE Login channel (same provider as the OA) → `LINE_LOGIN_*`; responders' LINE userIds → `RESPONDER_LINE_IDS`
- [ ] Google Sign-In (optional second way to sign in on the web): Google Cloud Console → APIs & Services → Credentials →
      OAuth client ID, type "Web application", authorised JavaScript origin = the site's https URL → `GOOGLE_CLIENT_ID`
- [ ] GISTDA API key (https://api-gateway.gistda.or.th) → `GISTDA_API_KEY`

## After the demo
- [ ] Stop or delete the ECS VM, EIP, RDS and OBS bucket. Delete the MaaS API key.

# Human-action checkpoints (REQUIREMENTS.md section 17, PRE-01 .. PRE-17)

Every external service in this system is built behind an interface with a mock, and the build
stops at each checkpoint below (RUL-01, RUL-02). You, the operator, complete the checkpoint and put
the resulting value in `.env` locally and in Railway's variables for the deployed services. Until
then the matching `*_MODE` key stays `mock` and nothing leaves the machine.

Rules that hold at every checkpoint:

- Values go only into `.env` (ignored by git) or Railway variables. Never into a file in this repo,
  never into chat.
- Every key named below exists in `.env.example` with a comment. Settings parity is enforced by a
  unit test.
- `SMS_MODE=live` is rejected at startup in this build regardless of checkpoints (RUL-06).
- Clearing a checkpoint does not switch anything to live. Flipping a `*_MODE` key to `live` is a
  separate, deliberate action recorded in the RUNBOOK's change log.

PRE-18 (execution platform) is already CONFIRMED by v2: Temporal Cloud + Railway. It has no action.

Ordered by what they unblock: the two BO-03 deploy items first, then build steps in order, then
the go-live items.

## Summary

| ID | Checkpoint | Unblocks | Keys filled | Done |
|---|---|---|---|---|
| PRE-07 | Temporal Cloud account | BO-03 deploy | `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` (or `TEMPORAL_TLS_CLIENT_CERT_PATH` + `TEMPORAL_TLS_CLIENT_KEY_PATH`), `TEMPORAL_TLS`, `TEMPORAL_MODE` | [ ] |
| PRE-08 | Railway account and project | BO-03 deploy | `DATABASE_URL` (Railway Postgres), `GRAPH_WEBHOOK_URL` (later) | [ ] |
| PRE-05 | M365 mailbox + Entra app | BO-04 live test | `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_MAILBOX`, `GRAPH_WEBHOOK_URL`, `GRAPH_WEBHOOK_CLIENT_STATE`, `GRAPH_MODE` | [ ] |
| PRE-04 | Sending domain(s), SPF/DKIM/DMARC, warmup | Investor L0 live (start week 1) | none directly; `GRAPH_MAILBOX` is on this domain | [ ] |
| PRE-09 | Model API key | BO-05 live test | `ANTHROPIC_API_KEY`, `MODEL_PROVIDER`, `MODEL_*` | [ ] |
| PRE-10 | Deed/transaction data provider | BO-06 real data | none yet (provider-specific keys added with the integration) | [ ] |
| PRE-06 | Google account, Sheet, service account | BO-08 live test | `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEETS_SPREADSHEET_ID`, `SHEETS_MODE` | [ ] |
| PRE-02 | Business physical address | Any live send | none (goes into templates, OPN-15) | [ ] |
| PRE-16 | Approve sender identity, templates, windows, caps | Investor L0 live | none (`config/*.yaml` and templates) | [ ] |
| PRE-17 | Approve first Active markets | Investor L0 live | none (`config/m1.yaml` / DB) | [ ] |
| PRE-01 | LLC + EIN | Seller L0 live | none | [ ] |
| PRE-03 | Business bank account | Seller L0 live | none | [ ] |
| PRE-11 | Skip-trace provider | Seller phase | none yet | [ ] |
| PRE-15 | Comps/AVM provider | Seller phase | none yet | [ ] |
| PRE-13 | DocuSign account + attorney-reviewed templates | BO-12 live | `DOCUSIGN_INTEGRATION_KEY`, `DOCUSIGN_USER_ID`, `DOCUSIGN_ACCOUNT_ID`, `DOCUSIGN_PRIVATE_KEY_PATH`, `DOCUSIGN_MODE` | [ ] |
| PRE-14 | Title company per Active market | Seller L0 live | none | [ ] |
| PRE-12 | Twilio + 10DLC | Never in this build | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_MESSAGING_SERVICE_SID` (`SMS_MODE` stays `off`) | [ ] |

Also tracked here because they gate the same deploy: **ENG-Q12** (private GitHub repo; no remote
exists) and the optional **storage choice** for seller photos (`BLOB_MODE`, ENG-Q9).

---

## PRE-07 — Temporal Cloud account

- [ ] Done

**What you do**

1. Create a Temporal Cloud account and a namespace (suggested name `wholesaling-prod`; region
   close to Railway's region for the project, e.g. `us-west`).
2. Choose authentication. API key is the simpler path and is what `Settings.validate_for_production()`
   requires today. In Temporal Cloud: Settings > API Keys > create a key scoped to the namespace
   with write access. Copy it once; it is not shown again.
3. From the namespace page copy the **gRPC endpoint** (form `<namespace>.<account>.tmprl.cloud:7233`)
   and the **namespace id** (form `<namespace>.<account>`).

**What results**: endpoint, namespace id, one API key (or one mTLS cert + key pair).

**Keys filled** (`.env.example` section "Temporal")

| Key | Value |
|---|---|
| `TEMPORAL_MODE` | `live` |
| `TEMPORAL_ADDRESS` | the gRPC endpoint |
| `TEMPORAL_NAMESPACE` | `<namespace>.<account>` |
| `TEMPORAL_API_KEY` | the API key |
| `TEMPORAL_TLS` | `true` |
| `TEMPORAL_TLS_CLIENT_CERT_PATH`, `TEMPORAL_TLS_CLIENT_KEY_PATH` | only for the mTLS alternative; note that `validate_for_production()` currently still demands `TEMPORAL_API_KEY`, so mTLS-only production needs a settings change first |
| `TEMPORAL_TASK_QUEUE` | leave `wholesaling-main` |

**Unblocks**: BO-03 Railway deploy (the worker needs a server to poll). Locally nothing changes;
`temporal server start-dev` keeps working with `TEMPORAL_TLS=false`.

**Verify**: `temporal workflow list --address <endpoint> --namespace <ns> --api-key <key> --tls`
returns (an empty list) without error.

## PRE-08 — Railway account and project

- [ ] Done

**What you do**

1. Create a Railway account (a paid plan is needed for `ALWAYS` restarts and more than 10 retries;
   `ON_FAILURE` with 10 retries, which this repo declares, works on any plan).
2. Create an empty project named `wholesaling`. Create a production environment (default).
3. Install the Railway CLI (see docs/RUNBOOK.md "Deploy"), `railway login`, `railway link` to the
   project from the repo root.
4. Provision Postgres in the project. The repo's `.railway/railway.py` declares it as `postgres`;
   `railway config apply` creates it, or add it in the dashboard first and let `railway config pull`
   import it.
5. Set the secret variables that `.railway/railway.py` marks `preserve()` on **both** services
   (`api`, `worker`): `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` from PRE-07;
   `ALERT_EMAIL_TO`, `SLACK_WEBHOOK_URL` may stay blank.

**What results**: a linked Railway project with a Postgres database and two services.

**Keys filled**

| Key | Value |
|---|---|
| `DATABASE_URL` | assembled by `.railway/railway.py` from the Postgres plugin's variables as `postgresql+asyncpg://...` on the private network. If the template does not resolve, set it by hand from the plugin's `PGUSER`/`PGPASSWORD`/`RAILWAY_PRIVATE_DOMAIN`/`PGDATABASE` values with the `postgresql+asyncpg://` scheme. |
| `GRAPH_WEBHOOK_URL` | later, at PRE-05: `https://<api public domain>/webhooks/graph` once the `api` service has a domain |

**Unblocks**: BO-03 Railway deploy and the RUL-09 restart test. Together with PRE-07 this is the
whole gate for "the system runs somewhere other than a laptop".

**Also needed**: ENG-Q12. Create the private GitHub repository, push `main`, set `GITHUB_REPO` in
`.railway/railway.py`, so Railway builds on push and CI runs.

## PRE-05 — Microsoft 365 mailbox and Entra app registration

- [ ] Done

**What you do**

1. Microsoft 365 Business subscription; confirm the account type is a work/school tenant (personal
   Outlook.com accounts do not support application permissions on Graph).
2. Create the sending mailbox on the domain from PRE-04 (e.g. `deals@<yourdomain>`).
3. Entra admin center > App registrations > New: single tenant. Record Tenant ID and Client ID.
4. Certificates & secrets > new client secret (set an expiry you will track; 12 months suggested).
5. API permissions > Microsoft Graph > Application: `Mail.Read`, `Mail.Send`, `Mail.ReadWrite`
   (drafts); grant admin consent. Restrict with an Exchange application access policy to the one
   mailbox.
6. Generate a random 32+ character string for the webhook client state.

**What results**: tenant id, client id, client secret, mailbox UPN, a random client-state secret.

**Keys filled** (`.env.example` section "Microsoft Graph / Outlook")

| Key | Value |
|---|---|
| `GRAPH_TENANT_ID` | Directory (tenant) ID |
| `GRAPH_CLIENT_ID` | Application (client) ID |
| `GRAPH_CLIENT_SECRET` | the secret value (not its ID) |
| `GRAPH_MAILBOX` | `deals@<yourdomain>` |
| `GRAPH_WEBHOOK_URL` | `https://<api public domain>/webhooks/graph` (needs PRE-08) |
| `GRAPH_WEBHOOK_CLIENT_STATE` | the random string |
| `GRAPH_MODE` | `live` only when you decide to run the BO-04 live test |

**Unblocks**: BO-04 live test (read mailbox, create a draft, renew a subscription). Sending stays
policy-gated at L0 (POL-01) and is forbidden in this build (RUL-06).

## PRE-04 — Sending domain(s), SPF/DKIM/DMARC, warmup

- [ ] Done

**What you do**: buy a dedicated sending domain (not your main company domain), add it to M365,
publish SPF, DKIM (two CNAMEs from M365) and a DMARC record (`p=none` to start), and begin a
warmup schedule in week 1 so it is ready by week 4.

**What results**: a verified domain in M365 with passing SPF/DKIM/DMARC checks.

**Keys filled**: none directly. `GRAPH_MAILBOX` (PRE-05) lives on this domain.

**Unblocks**: Investor L0 live (BO-09b). Not needed for any build step.

## PRE-09 — Model API key

- [ ] Done

**What you do**: create an Anthropic Console account, set a monthly spend limit at or below
`SOFT_BUDGET_CAP_USD` (default 1500), create an API key.

**What results**: one API key.

**Keys filled** (`.env.example` section "Model provider")

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | the key |
| `MODEL_PROVIDER` | `anthropic` when you decide to run the BO-05 live test |
| `MODEL_CLASSIFY`, `MODEL_EXTRACT`, `MODEL_DRAFT`, `MODEL_QA`, `MODEL_COMP_AUDIT` | leave as shipped; the IDs are verified against the `claude-api` skill in BO-05 (ENG-Q8) |

**Unblocks**: BO-05 live test (one real classify call with cost logged). All agent development
continues on `MODEL_PROVIDER=mock`.

## PRE-10 — Deed / transaction data provider

- [ ] Done

**What you do**: pick and contract the provider for recorded deeds and cash transactions (the
input to A1 investor discovery and M1 market scoring). Confirm terms of use allow outreach to
parties found in public records.

**What results**: an account and API credential; the exact keys are added to `.env.example` and
`Settings` with the integration, behind a `*_MODE` switch like every other provider.

**Keys filled**: none yet.

**Unblocks**: BO-06 with real data. BO-06 is built and tested on the mock provider first.

## PRE-06 — Google account, Sheet, service account

- [ ] Done

**What you do**

1. Google Cloud project; enable the Google Sheets API.
2. Create a service account; create a JSON key; download it.
3. Create the operator spreadsheet; share it with the service account's email as Editor.
4. Store the key file outside the repo (Railway: as a file mounted from a variable or a volume;
   locally: anywhere outside the working tree).

**What results**: a JSON key file and a spreadsheet id (the long id in the Sheet URL).

**Keys filled** (`.env.example` section "Google Sheets")

| Key | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **path** to the key file, never the key contents |
| `SHEETS_SPREADSHEET_ID` | the id |
| `SHEETS_MODE` | `live` only for the BO-08 live test |

**Unblocks**: BO-08 live test (one-way Postgres to Sheets sync, STK-02).

## PRE-02 — Business physical address

- [ ] Done

**What you do**: obtain a physical mailing address for the business (CAN-SPAM requires one in
every commercial email; 10DLC registration requires one too). A registered-agent or virtual office
address is acceptable if it receives mail.

**What results**: an address string.

**Keys filled**: none. It is inserted into the approved templates at PRE-16 (OPN-15).

**Unblocks**: any live send.

## PRE-16 — Approve sender identity, templates, send windows, daily caps

- [ ] Done

**What you do**: review and approve in writing: the sender name and company name (OPN-15), the
investor outreach copy and follow-ups (REQUIREMENTS section 8), seller templates and the offer
template, send windows per market timezone, and the L1 daily caps in `config/caps.yaml`
(OPN-06: 50 investor, 200 seller email).

**What results**: a written approval that is recorded in the RUNBOOK change log; template text
committed under `wholesaling/` (templates directory to be created in BO-07).

**Keys filled**: none.

**Unblocks**: Investor L0 live (BO-09b). Outside this build.

## PRE-17 — Approve first Active markets

- [ ] Done

**What you do**: after M1's initial run (BO-06), review the scored list and approve the markets
promoted to Active (cap 5, `config/m1.yaml`).

**What results**: a list of market ids marked Active in the database by the operator.

**Keys filled**: none.

**Unblocks**: Investor L0 live.

## PRE-01 — Form LLC, get EIN

- [ ] Done

**What you do**: file the Texas LLC (start in week 1 so it does not block week 8); obtain the EIN
from the IRS.

**What results**: LLC formation documents, EIN.

**Keys filled**: none.

**Unblocks**: Seller L0 live (contracts are signed by the entity). PRE-03 and PRE-13 depend on it.

## PRE-03 — Business bank account

- [ ] Done

**What you do**: open the business account under the LLC (needs PRE-01).

**What results**: account for earnest money and assignment fees. No automation touches it in this
build (RUL-06).

**Keys filled**: none.

**Unblocks**: Seller L0 live.

## PRE-11 — Skip-trace provider

- [ ] Done

**What you do**: pick and contract a skip-trace provider for seller contact enrichment (S1/S2).
Confirm TCPA/DNC compliance obligations in the contract.

**What results**: credential; keys added with the integration behind a `*_MODE` switch.

**Keys filled**: none yet.

**Unblocks**: Seller phase (BO-10) with real data.

## PRE-15 — Comps / AVM provider

- [ ] Done

**What you do**: pick and contract the comps/AVM provider for A7's ARV passes (V2, V3).

**What results**: credential; keys added with the integration behind a `*_MODE` switch.

**Keys filled**: none yet.

**Unblocks**: Seller phase (BO-11) with real comps. A7 is built and tested on mock comps.

## PRE-13 — DocuSign account and attorney-reviewed templates

- [ ] Done

**What you do**

1. Have a Texas real-estate attorney review the Purchase and Sale Agreement and Assignment
   templates (needs PRE-01 for the entity name).
2. DocuSign developer account first, production account when going live. Create an integration key
   (app), enable JWT grant, generate an RSA key pair, grant consent for the API user.
3. Upload the templates as DocuSign templates.

**What results**: integration key, API user id, account id, RSA private key file.

**Keys filled** (`.env.example` section "DocuSign")

| Key | Value |
|---|---|
| `DOCUSIGN_INTEGRATION_KEY` | the integration key (client id) |
| `DOCUSIGN_USER_ID` | the API user's GUID |
| `DOCUSIGN_ACCOUNT_ID` | the account GUID |
| `DOCUSIGN_PRIVATE_KEY_PATH` | **path** to the RSA private key file |
| `DOCUSIGN_MODE` | stays `mock` in this build; contract automation is forbidden (RUL-06) |

**Unblocks**: BO-12 live. Outside this build.

## PRE-14 — Title company per Active market

- [ ] Done

**What you do**: for each Active market, engage a title company that handles assignment closings
and record its contact details for the disposition workflow (S6/DispositionWorkflow).

**What results**: a contact record per market, entered by the operator.

**Keys filled**: none.

**Unblocks**: Seller L0 live.

## PRE-12 — Twilio account and 10DLC registration

- [ ] Done

**Never enabled in this build.** `SMS_MODE=live` is rejected by `Settings` (RUL-06) and the SMS
integration is interface + mock only (STK-08).

**What you do (when a future build allows it)**: Twilio account; register the 10DLC brand (needs
PRE-01 EIN and PRE-02 address) and campaign; create a Messaging Service.

**Keys filled** (`.env.example` section "SMS"): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_MESSAGING_SERVICE_SID`. `SMS_MODE` remains `off`.

**Unblocks**: nothing in this build.

---

## Storage choice (ENG-Q9, not a PRE item)

`BLOB_MODE=local` writes seller photos to `BLOB_LOCAL_DIR` (`./var/blobs`). On Railway that path is
ephemeral unless a volume is attached to the `api` service at `/app/var`. When you choose an
S3-compatible store, fill `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`
and set `BLOB_MODE=s3`. Needed before BO-10 (web form with photo upload) runs anywhere but a
laptop.

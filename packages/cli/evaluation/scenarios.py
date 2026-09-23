"""Frozen, authored recall tasks split by whole project/scenario family.

Each line is ``id | event text | five natural-language questions``.  The
questions were written with the intended event in view; development and test
families are disjoint.  Missing-answer families contain related records, but
none answer the questions.  Revise labels in a separate reviewed change, not
while tuning a ranker against the held-out split.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    family: str
    slug: str
    record: str
    questions: tuple[str, ...]
    split: str
    answerable: bool = True


def _read(family: str, split: str, data: str, *, answerable: bool = True) -> list[Scenario]:
    scenarios = []
    for line in data.strip().splitlines():
        slug, record, questions = (part.strip() for part in line.split("|"))
        questions = tuple(part.strip() for part in questions.split(";"))
        if len(questions) != 5 or len(set(questions)) != 5:
            raise ValueError(f"{family}/{slug} needs five distinct questions")
        scenarios.append(Scenario(family, slug, record, questions, split, answerable))
    if len(scenarios) != 5:
        raise ValueError(f"{family} needs five scenarios")
    return scenarios


SCENARIOS = tuple(
    _read("continuous-integration", "development", """
pytest-shard | The CI Python test job uses four pytest shards, selected with --splits 4 --group 2 for shard two. | How do I run the second test shard?; What arguments pick CI shard two?; Which pytest group is the second one?; How many shards does the Python job use?; Remind me of the command flags for test shard two
runner-image | Runner image ci-python-312 was pinned after the May torch upgrade broke the older image. | Which image did we pin for Python CI?; What runner image fixed the torch upgrade break?; Find the Python 3.12 CI image name; Which old-runner problem led to ci-python-312?; What image should the Python test job use?
cache-key | CI cache key includes the Python version and the SHA256 of uv.lock. | What goes into the CI cache key?; How is the dependency cache invalidated?; Does the Python version affect our cache?; Find the lockfile used by the CI cache key; Which hash do we include for cached dependencies?
flaky-test | Quarantine test_resume_after_network_drop until its clock dependency is replaced with a fake clock. | Which test did we quarantine?; Why is the resume test flaky?; Find the network-drop test needing a fake clock; What needs changing before test_resume_after_network_drop returns?; Which clock issue caused the quarantine?
artifact | The release workflow uploads macos-arm64.zip and its SHA256SUMS file from the same build job. | Which archive does the release job upload?; Where is the checksum file produced?; Do the archive and checksums come from one build?; Find the macOS artifact name; What accompanies macos-arm64.zip?
""")
    + _read("documentation", "development", """
quickstart | The quickstart starts with python3.12 -m venv .venv, then make setup and make test-db. | What is the first command in the quickstart?; How do I initialize the development venv?; Which make targets follow venv creation?; Find the local setup sequence for docs readers; What should a new contributor run first?
diagram | The storage diagram belongs in docs/ARCHITECTURE.md under Data flow, not in the root README. | Where should the storage diagram live?; Which docs section owns the data-flow drawing?; Is the storage diagram in the README?; Find the architecture diagram placement note; What file should I edit for the storage picture?
glossary | The glossary calls the searchable unit an event; a project is a user-assigned label, not a detected folder. | What does project mean in the glossary?; What do we call one searchable item?; Is a project inferred from folders?; Find the wording for event and project; How should we explain project labels?
limitations | The capabilities page must say hash embeddings are test fixtures and do not provide semantic search. | Where do we explain hash embedding limits?; Are hash vectors semantic retrieval?; What claim about hash embeddings should docs avoid?; Find the capability note about test vectors; Which page describes semantic search limitations?
links | Internal Markdown links are checked by make docs; images must have useful alternative text. | How do we validate links in documentation?; What should diagram alt text contain?; Which target checks Markdown links?; Find the docs image accessibility reminder; What command catches broken internal links?
""")
    + _read("refactoring", "development", """
parser | Move duplicate source-flag parsing into one function but keep existing CLI spellings. | What parser duplication are we removing?; Which command flags must remain compatible?; Find the source flag refactor note; Can we rename the old source options?; How should source flags be consolidated?
database | Replace direct SQLite reads in the UI with the public timeline fetch helper. | What direct SQL should the UI stop using?; Which helper should the panel call for recent events?; Find the database access refactor; Where should timeline reads be routed?; What is the plan for UI SQLite queries?
logging | Remove the global basicConfig call from the library import path; configure logging only at entry points. | Why does library import change logging?; Where should logging be configured?; Find the basicConfig cleanup note; What global logging call should go?; Should imports configure the root logger?
errors | Search should raise storage failures while an empty result should mean no matches. | How do we distinguish no results from errors?; What should happen when search storage fails?; Find the search error semantics note; Does empty search output mean a database exception?; Which failures should search raise?
cleanup | Delete the unused JSON vector dump once the migration reader no longer consumes it. | When can the JSON vector dump be removed?; What legacy reader uses that dump?; Find the cleanup dependency for vectors; Which artifact becomes unused after migration?; Is it safe to remove the dump now?
""")
    + _read("onboarding", "development", """
consent | First launch asks separately for clipboard and shell-history capture; both begin off. | Which sources need first-launch consent?; Is clipboard capture on by default?; What are the initial history and clipboard states?; Find the new-user consent behavior; Does installation start collecting shell history?
sample | Onboarding offers a synthetic sample note so search can be tried without reading a personal clipboard. | What demo content can a new user search?; Does trying recall require my clipboard?; Find the onboarding sample note; How can I test search before capture?; What is the privacy-safe first-search example?
shortcut | The keyboard shortcut is configurable in Settings and the menu stays available when registration fails. | Where can I change the recall shortcut?; What if hotkey registration fails?; Find the fallback for opening recall; Is the menu usable without a global shortcut?; How is the search hotkey chosen?
remove | Uninstall instructions disable login startup before removing the app bundle. | What happens before deleting the app?; How do we stop login launch during uninstall?; Find the uninstall ordering note; Should I delete the bundle first?; Which startup setting must removal clear?
recovery | The recovery page explains that restoring a vault never turns capture back on automatically. | Does restore re-enable recording?; What capture state follows vault recovery?; Find the recovery consent rule; Will a restored vault start shell watching?; How do I resume capture after restore?
""")
    + _read("unrecorded-development", "development", """
runner | The CI runner migration remains a draft with no approved machine image. | Which runner image was approved for the next migration?; What date did the new CI runner go live?; Find the completed runner rollout report; Who signed off on the new runner image?; What is the final replacement machine type?
style | The documentation style draft asks whether screenshots should be recaptured. | When were the new documentation screenshots published?; Which screenshot set was approved for release?; Find the completed docs image refresh; Who captured the final screenshots?; What file contains the approved new images?
module | A refactor brainstorm mentions splitting the parser but records no decision. | Which module received the extracted parser?; When was the parser refactor merged?; Find the approved parser architecture decision; Who completed the parser extraction?; What was the final module name for the parser?
tour | The onboarding research note proposes testing a guided tour with volunteers. | Which guided tour shipped to new users?; How many volunteers completed the onboarding tour?; Find the final onboarding tour results; What date did the tour launch?; Which tour design was selected?
recovery | The backup review checklist says to schedule a restore exercise later. | Which backup was successfully restored in the exercise?; What was the measured restore time?; Find the completed recovery drill report; When did the backup restore test pass?; Who approved the recovery drill result?
""", answerable=False)
    + _read("deployments", "heldout", """
rollback | For the Alder API deploy, rollback with helm rollback alder-api 17 -n prod-east after checking revision 17 in helm history. | How did I roll back Alder API?; Which Helm revision restored the east deployment?; Find the production rollback command for alder; What namespace was used for the Alder rollback?; What did I check before rolling back alder-api?
drain | Before the Birch worker deploy, drain queue birch-events to under 100 messages and pause intake with ./ops/intake pause birch. | What was the Birch predeploy queue check?; How did I pause Birch intake?; Find the command used before deploying the Birch worker; What message count was acceptable for birch-events?; Which queue did we drain for Birch?
flag | Canary for Cedar web was controlled by launch flag cedar_new_nav at 5 percent for thirty minutes. | Which flag controlled the Cedar canary?; What percent did Cedar start at?; How long did we watch the new navigation rollout?; Find the Cedar web rollout setting; Which launch flag was set to five percent?
secret | Rotate the Dune webhook token in the secret manager, then restart dune-relay; do not paste the token into a ticket. | What followed the Dune webhook token rotation?; Where did I rotate the relay credential?; Find the Dune relay restart step; What should not go into the Dune ticket?; Which service needed restart after rotating its webhook token?
health | After Elm gateway deploy, verify /readyz on port 9443 and confirm the regional error budget graph stays flat. | How did I verify the Elm gateway deploy?; Which Elm endpoint checked readiness?; What port hosts the gateway readiness check?; Find the postdeploy error-budget check for Elm; Which graph should remain flat after Elm release?
""")
    + _read("database-migrations", "heldout", """
backfill | Backfill the Fennel account_slug column in batches of 500 before enforcing NOT NULL in migration 042. | What batch size did Fennel use for the account backfill?; Which migration adds the Fennel NOT NULL rule?; When can we enforce account_slug?; Find the Fennel schema migration order; Which column needed a backfill before a constraint?
index | Create the Gorse invoices_due index concurrently; the regular index locked writes during the rehearsal. | Why use CREATE INDEX CONCURRENTLY for Gorse?; Which Gorse index blocked writes?; Find the invoice index migration note; What changed after the rehearsal lock?; How should invoices_due be indexed?
check | On Heather, compare row counts in accounts_v1 and accounts_v2, then sample 50 migrated IDs for equality. | How did we validate Heather migration?; Which tables were compared after the Heather copy?; How many migrated IDs did I sample?; Find the account migration verification checklist; What equality sample accompanies row counts?
rollback | Keep Ivy's old status column through the first release so rollback can read it; remove it in the next migration. | Why keep Ivy's old status column?; When should the legacy Ivy column be removed?; Find the rollback compatibility note for Ivy; Which column must survive the first release?; What enables Ivy migration rollback?
backup | Before Juniper's schema change, take a logical backup with pg_dump --format=custom --file=juniper-pre042.dump. | What backup preceded Juniper migration?; Find the Juniper pg_dump command; Which dump format did I use before schema change?; What filename held the Juniper pre-migration backup?; Did I take a logical backup for Juniper?
""")
    + _read("customer-support", "heldout", """
refund | For case KITE-284, issue the duplicate-charge refund through billing console and link both transaction IDs in the case. | How did we handle KITE-284?; Which support case was a duplicate charge?; Where do I issue the KITE refund?; What transaction evidence should the case contain?; Find the duplicate billing resolution note
access | LARK-617 access issue was resolved by removing a stale SSO group mapping and asking the customer to sign in again. | What fixed LARK-617 login?; Which customer case involved stale SSO mapping?; Did we reset the account password for LARK?; Find the support access resolution; What should the customer do after the SSO mapping is removed?
export | For MOTH-033, the export only failed above 2 GB; support suggested splitting by month while the streaming fix shipped. | What workaround did we give MOTH-033?; At what size did the export fail?; Find the large-export support note; Why did we ask MOTH to split exports?; What permanent export fix was pending?
webhook | NEWT-411 webhook delivery resumed after correcting the customer's 410 response; retries were not disabled. | What caused NEWT webhook failures?; Were retries switched off for NEWT-411?; Find the 410 response support resolution; How did NEWT deliveries resume?; Which case involved a customer endpoint returning 410?
timezone | For ORYX-902, reports showed UTC because the workspace time zone was unset; set it to Europe/Oslo. | Why were ORYX reports in UTC?; Which time zone fixed ORYX-902?; Find the support note about report time zones; What workspace setting was missing for ORYX?; How did we resolve the Oslo report issue?
""")
    + _read("release-process", "heldout", """
freeze | Release 0.8 branch freeze was Tuesday 16:00 UTC; documentation patches could still land with maintainer approval. | When was the 0.8 branch freeze?; Could docs change after the freeze?; Find the release freeze exception; What time did version 0.8 stop taking normal commits?; Who approved post-freeze documentation patches?
changelog | The 0.8 changelog notes the safer import preview and a known delay rebuilding the search index after upgrade. | What known issue went into the 0.8 notes?; Which new import feature is in the changelog?; Find the release note about search rebuilds; What should users expect after 0.8 upgrade?; What did the 0.8 changelog highlight?
tag | Tag v0.8.2 only after the signed archive checksum matches the candidate tested in the VM. | When should I create v0.8.2?; Which artifact checksum must match before tagging?; Find the VM candidate tagging rule; Can I tag before testing the signed archive?; What gate precedes the 0.8.2 tag?
triage | Patch release 0.8.3 was held because the Windows importer duplicated rows on retry. | Why was 0.8.3 held?; What importer bug blocked the patch release?; Find the retry duplication release triage; Which platform had the import issue?; What happened to rows when Windows import retried?
announce | Announce 0.8 in the release channel after packages reach mirrors, using the verified download link. | When should the 0.8 announcement go out?; Where should we post the release announcement?; Which download link belongs in the message?; Find the mirror readiness communication note; What must happen before announcing 0.8?
""")
    + _read("network-operations", "heldout", """
dns | For Pine DNS, lower the TTL to 300 seconds a day before moving the api record to the new load balancer. | What TTL did we use for Pine DNS cutover?; When should I lower Pine's TTL?; Find the API load balancer DNS plan; Which Pine record moved?; What was the first step before the DNS switch?
cert | Quartz edge certificate renewal uses acme.sh --renew -d edge.quartz.example, then a proxy reload. | How did I renew Quartz edge cert?; Which domain was in the ACME command?; What follows the Quartz certificate renewal?; Find the Quartz proxy reload note; Which tool renewed the edge certificate?
route | Route 10.42.0.0/16 through gateway 10.8.1.4 in the Rill staging VPN; production routes stay unchanged. | Which gateway handles the Rill staging network?; What CIDR was routed through the VPN?; Find the Rill staging route change; Did the Rill change affect production routes?; Which VPN route used 10.8.1.4?
latency | Spruce west latency spike came from a stale resolver cache; flushing the sidecar DNS cache returned p95 below 80 ms. | What caused Spruce west latency?; How did we bring p95 back down?; Find the sidecar DNS cache fix; What latency did Spruce reach after the flush?; Which resolver problem affected Spruce?
firewall | Add TCP 8443 from the Tansy metrics subnet only; do not open the dashboard to the public internet. | Which port did Tansy metrics need?; What source subnet may reach the dashboard?; Find the Tansy firewall restriction; Should TCP 8443 be internet exposed?; Which service needed a narrow inbound rule?
""")
    + _read("travel-notes", "heldout", """
train | The conference train leaves Union Station at 07:40 on Thursday; coach C seat 12A is on the ticket. | When does my conference train leave?; Which seat is on the Thursday ticket?; What station does the train depart?; Find the coach number for my conference trip; Remind me of the 07:40 travel booking
hotel | The Harbor Hotel booking is under Mira Chen; check-in starts at 15:00 and the reservation code is HBR-5812. | What is the Harbor Hotel confirmation code?; Who is the hotel booking under?; When can we check in at Harbor?; Find the conference hotel reservation; Which hotel has code HBR-5812?
receipt | Taxi receipt for the airport ride is in Expenses/Trips/June/taxi-04.pdf and was paid with the team card. | Where did I put the airport taxi receipt?; Which card paid for the taxi?; Find the June trip PDF receipt; What file should I attach for the airport ride?; Which expense folder holds taxi-04.pdf?
meeting | Meet the partner team in Room Willow at 10:30 Friday; bring the printed prototype agenda. | Where is the partner team meeting?; What should I bring to the Friday meeting?; When is the partner discussion?; Find the Room Willow plan; Which meeting needs the prototype agenda?
return | Return flight LX 432 boards at 18:05; the shuttle pickup is at the north lobby at 15:45. | What time is shuttle pickup for my return?; Which flight am I taking back?; Where does the airport shuttle meet us?; Find the return travel boarding time; When does LX 432 board?
""")
    + _read("budget-planning", "heldout", """
renewal | The Acacia analytics contract renews on November 12; ask procurement for a 20-seat quote by October 1. | When does Acacia analytics renew?; What quote should procurement request?; Find the October deadline for the Acacia contract; How many seats were planned?; What must happen before the November renewal?
credit | The Birch cloud credit expires at year end; apply it to staging compute before buying more reserved instances. | What should we spend the Birch credit on?; When does that cloud credit expire?; Find the staging compute budget note; What comes before more reserved instances?; Which credit can offset the staging bill?
invoice | Cedar invoice 7014 is disputed because the data export add-on was billed twice; finance requested a corrected PDF. | Why did finance dispute invoice 7014?; Which invoice needs a corrected PDF?; Find the double-billed add-on note; What did Cedar charge twice?; What document did finance ask for?
cap | Dune test infrastructure has a monthly cap of 900 dollars; notify the owner at 80 percent of that limit. | What is the Dune test budget cap?; When should the owner receive a spend alert?; Find the Dune monthly infra limit; Which percentage triggers a budget notice?; What budget applies to test infrastructure?
approval | Elm hardware purchase above 2,000 dollars needs written approval from both the engineering lead and finance. | Who approves expensive Elm hardware?; What purchase amount triggers written sign-off?; Find the hardware approval threshold; Does finance need to approve the Elm purchase?; Which two groups sign off above 2,000 dollars?
""")
    + _read("personal-scheduling", "heldout", """
dentist | The dentist appointment is Wednesday 09:15 at Maple Clinic; the booking code is MAP-302. | When is my dentist visit?; Which clinic has the appointment?; Find the Maple booking code; What time should I arrive Wednesday?; Which appointment is MAP-302?
delivery | The replacement desk arrives Friday between 13:00 and 16:00; use the side entrance for delivery. | When is the desk delivery window?; Which entrance should movers use?; Find the replacement desk note; What arrives Friday afternoon?; Do the desk carriers need the side entrance?
library | Return the two design books to North Library by September 28; the renewal limit has been reached. | When are my library books due?; Which library needs the design books?; Why can't I renew them again?; Find the book return reminder; How many design books must go back?
call | Call Aunt Jo Sunday at 17:00 after her trip; she prefers a phone call over video. | Who should I call after the trip?; When is the Sunday family call?; Find Aunt Jo's contact preference; Should this be a video call?; What time did I plan to phone Jo?
pickup | Pick up the repaired bicycle at River Cycles before 18:00 Thursday; bring claim ticket RC-19. | Where is my bicycle ready for pickup?; What ticket do I need at River Cycles?; When does the bike shop close for my pickup?; Find the repaired bike reminder; Which claim number is RC-19?
""")
    + _read("unrecorded-home", "heldout", """
paint | The home project note only says the living room swatches were compared. | Which exact paint color did I order for the living room?; What is the purchased paint's product code?; Find the shade I finally bought; How many cans of paint were ordered?; Which brand was on the final paint receipt?
plumber | The plumbing note says we discussed a possible sink repair next month. | What date did the plumber confirm for the repair?; What was the plumber's agreed invoice total?; Find the booked sink repair appointment; Which plumber was hired for next month?; What repair warranty did the plumber promise?
garden | The garden note lists seedlings under consideration for spring. | How many tomato seedlings did I buy?; Which nursery received my garden order?; Find the final seedling purchase list; What was the confirmed delivery day for plants?; Which tomato variety did I purchase?
roof | The roof note contains questions to ask contractors about an inspection. | What was the inspector's roof leak diagnosis?; Find the roof repair quote I accepted; Which contractor was awarded the roof job?; How much did the completed inspection cost?; When is the confirmed roof repair appointment?
furniture | The furniture note compares two bookshelf widths without recording a purchase. | Which bookshelf model did I purchase?; Find the order number for the new shelf; What delivery date was confirmed for the bookshelf?; Which store charged me for the shelf?; How much did the ordered shelf cost?
""", answerable=False)
    + _read("unrecorded-ops", "heldout", """
rotation | The operations checklist asks whether the Marigold token should be rotated. | On which date was Marigold's token rotated?; What new secret version was issued to Marigold?; Who completed the Marigold credential rotation?; Find the completed Marigold token rotation record; What was the verified rotation result?
outage | The incident draft lists symptoms for the Nettle outage but has no root-cause conclusion. | What was the confirmed cause of the Nettle outage?; How many minutes did the final Nettle incident last?; Find the signed-off Nettle postmortem conclusion; Which fix resolved the outage?; Who approved the final Nettle incident report?
patch | The Oak security note says a patch is under evaluation and has not been rolled out. | What version was deployed to patch Oak?; When did the Oak rollout finish?; Find the completed Oak patch verification; Which hosts received the Oak fix?; What was the final rollout status for Oak?
capacity | The Palmetto capacity note lists possible thresholds but no selected limit. | What limit was finally set for Palmetto workers?; Which capacity target did we approve?; Find the production Palmetto worker count; When did Palmetto scaling complete?; What was the final Palmetto concurrency setting?
restore | The Redwood recovery plan describes how a restore would be tested later. | When did the Redwood restore test pass?; What was Redwood's measured recovery time?; Find the completed Redwood disaster recovery result; Which backup snapshot was restored successfully?; Who signed off on the Redwood restore rehearsal?
""", answerable=False)
)


def validate() -> None:
    ids = [(item.family, item.slug) for item in SCENARIOS]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario IDs must be unique")
    dev = [s for s in SCENARIOS if s.split == "development"]
    held = [s for s in SCENARIOS if s.split == "heldout"]
    if len(dev) != 25 or len(held) != 50:
        raise ValueError("scenario split changed")
    if sum(s.answerable for s in dev) != 20:
        raise ValueError("development answer labels changed")
    if sum(s.answerable for s in held) != 40:
        raise ValueError("held-out answer labels changed")
    if {s.family for s in dev} & {s.family for s in held}:
        raise ValueError("a project family leaked across splits")


validate()

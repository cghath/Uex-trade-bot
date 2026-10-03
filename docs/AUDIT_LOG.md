# Audit Log

Which production commits (`cghath/Uex-trade-bot`, branch `TestBranch`) an audit has covered.

**The rule** (the owner's, 2026-10-02): `main` is caught up from `TestBranch` only after an
audit covers every commit being synced. Before opening a TestBranch -> `main` sync PR, check
**Audited up to** below. If the sync would carry unaudited commits, audit them first, or sync
only up to the last audited commit. A sync PR is merged with "Create a merge commit", never a
squash (a squash leaves `main` with a commit `TestBranch` lacks).

When an audit finishes, add it to the top of **Audits**, move **Audited up to**, and record
any `main` sync under **Syncs to main**. Audit reports stay local (`docs/audits/` and
`data/audit-*/`, neither committed); the log is the part that's shared.

## Status

- **Audited up to:** `0e8818b` (2026-10-03). The 2026-10-03 range audit covered
  `cffa68e..0e8818b`; the 2026-09-25 full-project audit covered everything up to `cffa68e`.
- **Not yet audited:** `0e8818b..TestBranch` (nothing as of 2026-10-03). The next audit starts at
  `0e8818b`.
- **Open findings from the last audit:** 37, none P0. REL-1 is P1 until the listing-delete fix
  (production PR #115) is proven live; REL-2, REL-3, UX-1, UX-2, LOGIC-1 and MSG-1 to MSG-4 are P2.
  Fixing them adds new commits, which the next audit covers.
- **`main`:** at `865a3b3` (production PR #95, 2026-09-30). Everything up to `0e8818b` is audited,
  but the owner holds the sync until the audit's findings are fixed (2026-10-03).

## Audits

Newest first. "Result" is the report's own headline, not a re-check of whether every finding
was later fixed; each audit's fixes are in the PROJECT_CONTEXT.md entry named, where there is
one. The 2026-09-25 audit re-read the whole repo, so it supersedes everything below it.

| Date | Commits covered | Kind | Report (local) | Result |
|---|---|---|---|---|
| 2026-10-03 | `cffa68e..0e8818b` (88 commits, entries 87-130, production PRs #64-#115) | Range audit: 4 read-only auditors (REL, UX, MSG, LOGIC), coordinated; each re-checked the 2026-09-25 fixes in its area | `docs/audits/2026-10-03-range-audit-0e8818b.md` | 37 findings (9 REL, 12 UX, 9 MSG, 7 LOGIC); no P0; 1 P1 (REL-1, the 48h relist trusting a delete's "ok") until PR #115 is proven; most 2026-09-25 fixes closed, the rest partly closed with siblings left |
| 2026-09-25 | whole repo at `cffa68e` | Full-project audit: 3 read-only auditors (UX, MSG, REL), coordinated | `docs/audits/2026-09-25-full-project-audit.md` | 58 findings (20 UX, 21 MSG, 17 REL); fixed in entries 87-112, re-checked by the 2026-10-03 audit |
| ~2026-09-25 | `370e232..d8a0bc2` | Outside audit | none kept; entry 82 | P1 missing `ship_parts_shopping_entries` migration, unbounded autocomplete latency; fixed (entry 82) |
| ~2026-09-21 | `8f9be8e` (entries 68-69, route hedges and backups) | Outside audit | none kept; entry 70 | 3 findings, fixed (entry 70) |
| 2026-09-15 | `dd8444a..1e336db` (6 commits) | Executive audit, solo | `docs/audits/2026-09-15-executive-audit-1e336db.md`, probes `data/audit-1e336db/` | 1 P1 (backup verification accepted a truncated DB), 4 P2 recovery paths; backup pruning held until fixed |
| 2026-09-15 | up to `dd8444a` (start not recorded) | Audit, probes only | `data/audit-dd8444a/` | report not kept |
| 2026-09-13 | `165d20d..f8886a6` | Third-party audit | none kept; ROADMAP.md "Third-Party Audit Fix Round" | Conditional pass for TestBranch, hold promotion to `main`; 6 findings, 4 fixed, 2 deferred |
| 2026-09-12 | up to `f0fcc40` (start not recorded) | Audit, probes only | `data/audit-f0fcc40/` | report not kept; `f0fcc40` falls inside the 2026-09-13 range |
| 2026-09-11 | `45bc0d0..165d20d` | Coordinated audit: 3 agents (delivery, routes, state) | `data/audit-165d20d/` | 4 P2 in route-progression recovery, no P0/P1 |
| 2026-09-11 | `f664a53..45bc0d0` | Coordinated audit: 3 focused reviews | `data/audit-45bc0d0/REVIEW.md` | see report |
| 2026-09-11 | `da86972..f664a53` (9 commits) | Audit | `data/audit-f664a53/REVIEW.md` | P2 findings; the previous audit's 5 confirmed fixed |
| 2026-09-08 | `f562ae0..da86972` (16 commits) | Audit | `data/audit-da86972/REVIEW.md` | 5 findings (route reporting, diminishing returns) |
| 2026-09-06 | `3ec9e9c..f562ae0` | External review | `data/audit-f562ae0/REVIEW.md` | 4 P2, fixed (entry 55) |
| 2026-09-06 | `33d659f..3ec9e9c` | Scoped review | `data/audit-3ec9e9c/REVIEW.md` | no findings |
| 2026-09-06 | `b21aba0..33d659f` | External review | `data/audit-33d659f/REVIEW.md` | 1 P2, fixed (entry 51) |
| 2026-09-05 | `b95390c..b21aba0` | External review | `data/audit-b21aba0/REVIEW.md` | 3 P2, fixed (entry 50) |
| 2026-09-05/06 | `be40410..bcf9631` | Self-directed: 2 subagents and a security review | none kept; entry 49 | see entry 49 |
| 2026-09-05 | `bd2d50b..b95390c` | External review | `data/audit-b95390c/REVIEW.md` | 2 P2, fixed (entry 48) |
| 2026-09-05 | `be40410..bd2d50b` | External review | `data/audit-bd2d50b/REVIEW.md` | 5 P2, fixed (entry 47) |
| 2026-09-05 | whole repo at `be40410` | Full project audit | `data/full-audit-20260905/AUDIT_REPORT.md` | 15 findings (4 P1, 11 P2); P1s fixed same day (entries 45-46) |

Not production: the 2026-09-17 audit of the aiv2 fork (`docs/audits/2026-09-17-aiv2-ai-audit.md`,
`data/audit-aiv2/`) covers `cghath/aiv2`, so it isn't tracked here.

## Syncs to main

| Date | PR | `main` after | Audited? |
|---|---|---|---|
| 2026-09-30 | production PR #95 | `865a3b3` | Partly: up to `cffa68e`; `cffa68e..865a3b3` (33 commits) synced before this rule |

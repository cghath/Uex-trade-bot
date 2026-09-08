# Contributing to this bot

This doc exists because of a real, expensive mistake: an earlier AI coding session spent
hours debugging why `/scan-now` "wasn't syncing," invented a fake root cause ("library
resolution conflict," a "poisoned environment"), reset the branch, and shipped a feature
that still didn't work. The actual bug was one missing line and one line of invalid API
usage — both of which the checklist below would have caught in under a minute. Read this
before adding a feature, and run the checklist before calling one done.

## The #1 rule: a new cog isn't live until it's registered

Adding a file to `bot/cogs/` does **nothing** by itself. `bot/main.py` only loads cogs
listed in its `INITIAL_COGS` tuple:

```python
INITIAL_COGS = (
    "bot.cogs.account",
    "bot.cogs.prices",
    ...
    "bot.cogs.scanner",   # <- your new cog must be added here
)
```

If a cog isn't in this tuple, `setup_hook()` never calls `load_extension()` on it, its
`setup()` function never runs, its commands never get added to `bot.tree`, and
`tree.sync()` has nothing to push for it. **No error is raised.** The bot starts up fine,
logs in fine, and the command just silently never appears in Discord. This exact silent
failure is what actually broke `/scan-now` — not a library bug.

## The #2 rule: dev-guild syncing needs `copy_global_to`

`@app_commands.command()` registers a command on the bot's **global** command tree.
`tree.sync(guild=...)` only pushes commands registered to *that guild's* tree. So syncing to
a dev guild without bridging the two pushes nothing:

```python
if self.config.discord_dev_guild_id:
    guild = discord.Object(id=self.config.discord_dev_guild_id)
    self.tree.copy_global_to(guild=guild)   # <- without this, the next line syncs 0 commands
    synced = await self.tree.sync(guild=guild)
```

**`Synced 0 commands to dev guild ...` in the startup log means exactly this**, and it's the
one symptom that reliably distinguishes it from the `INITIAL_COGS` failure above (which
instead shows a missing `Loaded extension` line). This has already bitten the repo twice —
the `copy_global_to` line was present from the initial commit, got dropped during a later
refactor, and had to be restored. Don't remove it, and if you're rewriting `setup_hook`,
carry it across.

Note this only affects the dev-guild path (`DISCORD_DEV_GUILD_ID` set in `.env`). A plain
global `tree.sync()` needs no bridging, but takes up to an hour to appear in Discord, which
is why the dev-guild path exists at all.

## Slash commands: there is exactly one correct pattern in this codebase

Every cog uses standard decorators directly on the Cog's methods, and `setup()` does
nothing but `await bot.add_cog(...)`:

```python
class MyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="my-command", description="...")
    async def my_command(self, interaction: discord.Interaction) -> None:
        ...

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MyCog(bot))
```

That's it. `bot.add_cog()` automatically finds and registers every `@app_commands.command`
decorated method on the class.

**Never do any of the following** — all three were tried in the broken version of the
scanner and all three are wrong:
- `bot.tree.command(name=..., callback=some_method)` — `CommandTree.command()` is a
  decorator factory (`@bot.tree.command(...)` above a function def). It does not accept a
  `callback=` keyword. Passing one raises `TypeError`.
- `bot.tree.command(..., func=some_method)` — same problem, `func` isn't a valid keyword
  either.
- Calling `bot.add_cog(...)` manually in `setup_hook()` *and* also relying on
  `load_extension()` to load the same cog — this double-registers it and raises a
  `CommandAlreadyRegistered` error. Pick one path: put the cog in `INITIAL_COGS` and let
  `load_extension()` handle it. Don't also add it by hand.

If you ever think you need to register a command outside the decorator (you almost never
do), the real API is `bot.tree.add_command(app_commands.Command(name=..., description=...,
callback=...))` — but check whether an existing cog already solves your problem with plain
decorators first, because one always does.

## New DB tables need an actual `CREATE TABLE`

Referencing a table in a query doesn't create it. Every table your feature reads or writes
must have a `CREATE TABLE IF NOT EXISTS` statement added to the `SCHEMA` string in
`bot/db/database.py`. If you skip this, the code will crash the first time it actually
runs with `sqlite3.OperationalError: no such table: whatever` — and only then, not at
import time, so it's easy to miss if you never actually run the feature.

## Copy the pattern of the most similar existing feature

Before writing a new cog, find the existing cog closest in shape to what you're building
and copy its structure, not just its vibe:

- **Background poll + slash commands + per-user notification**: copy
  `bot/cogs/marketplace_alerts.py` or `bot/cogs/stock_alerts.py`.
- **Pure matching/calculation logic** (no Discord, no I/O): put it in `bot/uex/<name>.py`,
  parallel to `bot/uex/stock_alerts.py` or `bot/uex/trends.py`. Keep it dependency-free so
  it's unit-testable with plain dicts.
- **Discord-facing glue** (commands, embeds, the poll loop itself): put it in
  `bot/cogs/<name>.py`, which imports from the pure module above.

This isn't a style preference — matching the existing pattern exactly is what makes a new
feature immediately reviewable and keeps `INITIAL_COGS`/schema/config wiring from being
forgotten, because you're copying a file that already got all of that right.

## Lifecycle and failure-path tests are part of implementation

These requirements apply to humans and coding agents, for features, bug fixes, and
refactors. Repeated audits found correct helpers inside broken workflows: a notification
retry bypassed the poller's checkpoint, a cleared preference returned after restart,
fallback output lost safety warnings, and failed recovery still restarted the service.
Passing tests are necessary, but their count does not establish that these cases work.

### Required evidence for state-changing features

Before declaring a state-changing feature complete:

- **Test one complete workflow.** Exercise the real command or callback, internal handler, temporary database write, and subsequent read through the relevant consumer. Mock external services—not the internal transition being tested. Verify both the intended changes and preservation of unrelated fields.

- **Keep a failure-and-retry checklist.** For each important awaited external operation or database write, identify what happens if it fails before or after state is committed. Test applicable failures and retries, proving that work is neither permanently locked nor duplicated. An in-progress UI flag must not be treated as proof of durable completion.

- **Verify the meaning of data crossing boundaries.** Explicitly distinguish planned quantities, completed transactions, observed availability, and remaining stock. Distinguish omitted fields, unknown values, and confirmed zero. Reject non-finite or out-of-domain numeric input before changing state.

- **Require behavioral regression evidence.** A regression test must fail against the old behavior because the intended guarantee is violated. Missing imports, new parameters, or incompatible test fixtures do not count as reproducing the bug.

The implementation handoff must name the workflow and failure-path tests, report their results, and disclose any applicable scenarios left untested. Additional helper tests or a larger passing-test count do not substitute for this evidence.

### 1. Define guarantees before changing production code

Write a short test plan in the task or PR: normally 3–5 user-visible guarantees, the
affected workflows, and the applicable scenarios from the table below. For a small fix,
one precise guarantee may suffice. Describe behavior, not the proposed implementation.

For saved trading preferences, for example:

- Clearing a migrated default ship remains cleared after restart.
- Changing one field preserves unrelated fields, including during overlapping updates.
- Explicit command options override the matching saved defaults.
- Private settings remain private, and slow ship lookup does not expire the interaction.

For routes, every displayed route must retain its safety warnings and approximation
disclosure in every output format. Missing measurements must not become numeric zero,
and internal search limits must not be described as physical ship or market limits.

### 2. Select scenarios and write tests before the implementation

Write the normal-use test and the relevant lifecycle/failure tests first. Start with one
lifecycle test and one failure/concurrency test where applicable, but cover all relevant
high-risk cases for external writes, private data, or recovery. Mark non-applicable cases
with a brief reason; do not manufacture irrelevant tests to meet a quota.

| Change touches | Required scenarios to consider |
| --- | --- |
| Persistent settings or migrations | Create → update → clear → restart; legacy migration → clear → restart; repeated initialization |
| Shared database state | Overlapping updates to different fields; duplicate requests; isolation between users |
| External posting/deletion | Accepted; explicitly rejected; response lost after possible success; retry without duplicate writes or premature inventory release |
| Background polling/notifications | Failed cycle → next cycle; failed delivery → retry; partial delivery without duplicates; checkpoint advances only when appropriate |
| Backup/deployment/recovery | Failure before and after replacement starts; partially completed copy; failed restoration of DB, sidecars, or code; no restart after incomplete recovery |
| Discord commands/output | Slow fetch with timely acknowledgement; ephemeral responses stay private; oversized fields and total messages; text fallback retains warnings/disclosures |
| Historical data/calculations | Missing versus zero; baseline and current value independently missing; three or more observations; exact boundary and just below/above it |

### 3. Test the real workflow, not just the helper

- For retry behavior, invoke the actual poller twice, including its checkpoint logic.
  Calling a notification helper twice does not prove the poller will retry it.
- Use temporary SQLite databases and real database methods for persistence/state tests.
  Reinitialize the same database after clearing settings to exercise startup migrations.
- Make concurrency deterministic with events/barriers: deliberately let two operations
  read the old state before either writes. Avoid timing-dependent sleeps as race tests.
- Mock external boundaries (UEX, Discord, service control), not the internal state
  transitions or failure classification being tested. Inject failures at the actual
  boundary and verify the resulting reservations, checkpoints, and user-visible output.
- Check final assembled messages, including footer text and combined embed sizes.
  Check both proactive size fallback and send-error fallback, and verify safety content
  survives rather than merely asserting that something was sent.
- Test recovery scripts using isolated fixtures and mocked destructive/service commands.
  Exercise the real script/handler, not a rewritten copy of its logic. Include failed
  recovery as well as successful recovery, and assert whether restart was attempted.

Automated tests must not use production databases, real credentials, live marketplace
writes, or real service restarts. Live validation is separate and requires authorization.

### 4. Implement, then prove the fix and its neighboring cases

For a bug fix, demonstrate that the regression test fails on the pre-fix behavior for
the expected reason and passes with the fix. An import error, missing mock method, or
unrelated exception is not a reproduction. Use an isolated copy/worktree or another safe
method when comparing old behavior; never reset or overwrite someone else's work.

Do not weaken assertions or redefine expected behavior simply to make the fix pass.
Add neighboring cases of the same defect class: missing baseline AND missing current
value; failed operation AND failed recovery; clear AND restart; two AND three observations.
Inspect every caller/catcher when a shared return value, exception, or storage contract
changes. A helper returning False is not protective if its caller ignores the result.

Promote useful audit-only probes into the normal `tests/` suite as fixes land. Retain
repeatable script-level checks too, and document how to run them. Run targeted tests
during implementation and the full project suite before handoff.

### 5. Review independently and hand off evidence

Perform a separate adversarial review pass using the requirements and diff, not just the
implementer's explanation. A fresh reviewer/session can help when available; do not
create agents/tasks without authorization. Try to disprove the guarantees, trace all
affected callers, and do not invent findings to satisfy a quota.

Every implementation handoff/PR must state:

- The commit or working-tree changes reviewed.
- The guarantees and lifecycle/failure scenarios tested, with test names or commands.
- Regression evidence, test results, and any relevant script checks.
- Deliberate exclusions, unresolved risks, and whether live validation was performed.

Use this compact checklist in implementation tasks and PR descriptions:

- [ ] Guarantees and applicable lifecycle/failure cases defined before implementation.
- [ ] Normal behavior and relevant restart, concurrency, failure, and fallback tests added.
- [ ] Regression tests fail for the intended reason without the fix and pass with it.
- [ ] All callers of changed shared contracts inspected; neighboring cases checked.
- [ ] Targeted and full tests run; untested conditions and live-validation status reported.

Repository enforcement should run the normal tests on TestBranch PRs and require the
test check before merging where configured. Inspect existing CI before adding or changing
it. This document does not itself configure CI, a PR template, or branch protection;
changing those settings is separate work requiring appropriate authorization. Automation
can enforce that tests pass, but review must still assess whether the right scenarios
were tested. Documentation-only changes may use documentation/diff checks instead of
rerunning runtime tests, provided no executable behavior changed and that scope is stated.

## Before declaring a bug "diagnosed" or a feature "done": verify, don't theorize

The single biggest time-sink in this repo's history was an AI session that wrote a
confident, detailed "Deep Dive Analysis" blaming a nonexistent environment/library
conflict, when the real cause (a cog missing from `INITIAL_COGS`) was checkable in one
`grep`. A wrong-but-confident explanation is worse than no explanation, because it sends
the next round of work in the wrong direction entirely.

Before writing up a root cause or claiming something works:

- **Reproduce the exact error message**, don't paraphrase it from memory or guess at what
  "probably" caused it.
- **grep for the thing you think is missing** before concluding it's missing — e.g.
  `grep -n "your_cog_name" bot/main.py` takes two seconds and either confirms or kills the
  theory immediately.
- **When authorized to perform live validation, start the bot** (`python -m bot.main`)
  and read the startup log. You are
  looking for two specific lines: `Loaded extension bot.cogs.<yours>` and `Synced N
  commands...` with a plausible N. If either is missing or looks wrong, the feature isn't
  wired up yet, no matter how correct the command code itself looks.
- **Validate the command in Discord when authorized** before claiming it is live-verified.
  Passing pure-logic, database, and mocked-command tests does not prove real Discord
  registration or delivery. If live access/authorization is unavailable, report that
  limitation explicitly; do not start a second bot or perform external writes merely
  because a checklist mentions live validation.
- If you're unsure what a UEX API field actually means (quality, quality_tier, pricing
  units, anything with ambiguous semantics), check `docs/UEX_API_2.0_reference.md` (a full
  scrape of every endpoint, kept in this repo so it's available offline) before assuming —
  a wrong assumption here produced several rounds of a real bug (the Undervalued Scanner's
  early "steal" detection was wrong for days because of an unverified assumption about how
  `quality_tier` buckets map to raw quality values, until someone actually pulled the
  field-level docs). If that file is stale, the live docs are at
  https://uexcorp.space/api/documentation/ — but even the live docs aren't infallible: one
  field (`marketplace_listings.quality`, documented as 0-100) has been observed returning
  values up to 1000 in real data. When the docs and real observed data disagree, trust the
  data.

## Pre-flight checklist for any new feature

Run through this before considering a feature finished:

- [ ] Lifecycle/failure-path implementation checklist above completed, with evidence
- [ ] New cog's module path added to `INITIAL_COGS` in `bot/main.py`
- [ ] New cog's `setup()` does `await bot.add_cog(...)` exactly once, nothing else
- [ ] Every slash command uses `@app_commands.command(...)` directly on a Cog method — no
      manual `bot.tree.command(callback=...)` or `func=...` anywhere
- [ ] Any new DB table has a real `CREATE TABLE IF NOT EXISTS` in `bot/db/database.py`'s
      `SCHEMA` string
- [ ] Any new config value is read in `bot/config.py` and documented in `.env.example`
- [ ] `python -m pytest -q` passes
- [ ] With authorization, started the bot locally and saw `Loaded extension bot.cogs.<yours>` and a plausible
      `Synced N commands` line in the log — `Synced 0` to a dev guild means the
      `copy_global_to` bridge is missing (see "The #2 rule" above)
- [ ] With authorization, ran the new command(s) in a real Discord server; otherwise
      explicitly recorded live validation as not performed

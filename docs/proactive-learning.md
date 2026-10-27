# Adaptive proactive replies

Phase 9.1 adjusts how selective proactive replies are in an explicitly enabled QQ
group. It does not enable proactive replies, add a new model call per message, or
replace the existing group permissions and local rate gate.

## Configuration

Apply Alembic migration `0031_proactive_learning` before starting this version.
Use the deployment's existing private environment configuration; these settings
are not currently editable in the console.

```dotenv
AI_PROACTIVE_ENABLED=false
AI_PROACTIVE_ADAPTIVE=false
AI_PROACTIVE_ADAPTIVE_GROUPS=
AI_PROACTIVE_FEEDBACK_SECONDS=300
```

- Both flags default to false. An empty group list opts in no groups.
- To try learning in a test group, explicitly enable both flags and set
  `AI_PROACTIVE_ADAPTIVE_GROUPS` to that group's ID. The group must also be enabled
  by the normal group policy. Other enabled groups retain their static threshold.
- The feedback window is clamped to 60-3600 seconds. The default is five minutes.
- `AI_PROACTIVE_GATE_PERCENT` and `AI_PROACTIVE_MAX_CHECKS_PER_HOUR` still apply
  before any model request. Their defaults are 10 percent and six checks per hour
  per process/group, not a distributed cluster-wide sending quota.
- `AI_FEEDBACK_POSITIVE_EMOJI_IDS` and `AI_FEEDBACK_NEGATIVE_EMOJI_IDS` are reused
  from reaction feedback. Both default to empty. Confirm IDs from a real test-group
  event instead of assuming a visual emoji has the same ID across clients.
- Set `AI_PROACTIVE_ENABLED=false` and restart to stop all proactive speaking.
  Disabling only `AI_PROACTIVE_ADAPTIVE` returns enabled groups to the configured
  static threshold; it does not stop proactive speaking. Stored statistics remain.

## Selection and feedback

The sampler chooses from interest thresholds 90, 94 and 98 independently for each
group and bot account. It samples a Beta distribution for each candidate and uses
the largest draw. Initial `(alpha, beta)` values are `(1, 3)`, `(2, 3)`, `(3, 2)`:
the higher threshold is preferred initially, but lower thresholds can be tried.

Only a nonempty model reply scoring at least the chosen threshold can be sent.
Explicitly addressed messages go to the ordinary reply path. Old adaptive triggers
over 60 seconds old are skipped to avoid replaying offline chat as fresh small talk.

After a send receipt supplies a message ID, feedback is accepted within the window:

| Signal | Reward | Weight |
| --- | --- | --- |
| Any remaining negative reaction, direct rejection or another member's recall | 0 | 1 |
| Positive reaction and no negative signal | 1 | 1 |
| An explicitly addressed follow-up and no stronger signal | 1 | 0.5 |
| No remaining feedback | 0.5 | 0.1 |

An addressed follow-up is a non-command text reply to that exact message, or an
explicit mention of the bot resolving to its latest tracked proactive reply in
the window. Nearby ordinary chatter is ignored. A rejection is a narrow direct
phrase such as `闭嘴` or `stop talking`; discussing that phrase does not qualify.
The bot's own activity and recall are ignored. Reply IDs, bot IDs and group scopes
are checked before recording feedback.

Reaction counts are not vote multipliers. Repeated events are deduplicated per
actor and signal kind. Replacing a reaction replaces that actor's reaction;
removal only retracts the matching emoji. Older events cannot undo newer ones.
One trial contributes at most one weighted observation, irrespective of group size.

## Persistence and failure handling

Alembic owns three PostgreSQL tables: `proactive_trials`, `proactive_feedback` and
`proactive_arms`. The tables retain group/bot/message/actor identifiers, selected
thresholds, timestamps and outcomes, but do not duplicate message bodies. These
are private operational records, not public analytics exports.

Before sending, a unique `(group scope, bot, trigger message)` reserves the trial.
A duplicate trigger cannot send it again. An acknowledged send opens the feedback
window; a failed or ambiguous send does not train the sampler. Adaptive sends do
not retry on an ambiguous timeout, since QQ might already have delivered them.
This favors avoiding duplicate interruptions over retrying unsolicited messages.

A background loop settles expired windows every 30 seconds, at most 100 per batch.
Row locks and a transaction update the trial and aggregate together. Restarting or
having overlapping settlement workers does not count a trial twice. Interrupted
pending sends expire to unknown after ten minutes, not to a fabricated failure or
success. Database errors defer learning or skip adaptive sends; feedback errors
cannot interrupt canonical message ingestion.

## Limits and acceptance

This is a bounded feedback heuristic, not proof that the model understands a group
better. Addressed follow-ups measure engagement, not agreement; an unrelated new
question mentioning the bot can still be attributed to the latest trial. No-feedback
is a low-weight neutral update, not evidence of satisfaction. Sampling only learns
from messages that were actually sent, so it is not an unbiased comparison of all
possible thresholds. A malicious negative voter can bias it toward more caution.

No automatic conclusion about quality or cost savings is justified by unit tests.
Live acceptance still requires an authorized test group: confirm actual reaction
IDs and notice fields, try reply/retraction/recall, verify disabled groups stay
unchanged, inspect the stored outcome, and compare disruption with the static
baseline. Never enable a production group as a side effect of running tests.

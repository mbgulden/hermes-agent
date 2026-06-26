import { computed } from 'nanostores'

import { $activeSessionId, $busy } from './session'
import { $subagentsBySession, activeSubagentCount } from './subagents'

/**
 * "Parked" background-delegation signal for the active session.
 *
 * A top-level `delegate_task` always runs in the background: the parent turn
 * ends (`$busy` -> false) while the subagent keeps running, and its result
 * re-enters the conversation as a fresh turn when it finishes. During that
 * window the app is genuinely idle but work is still happening elsewhere, so we
 * want a calm "will resume when the background task finishes" affordance instead
 * of a spinner that reads as "stuck."
 *
 * This reports how many background subagents are still in flight for the active
 * session WHILE the agent itself is idle. It is deliberately zero while `$busy`:
 * an active turn already owns the main loader, and subagents spawned inside a
 * running turn (synchronous orchestrator children) are part of that turn, not
 * parked background work the user is waiting on.
 */
export const $backgroundResumePendingCount = computed(
  [$subagentsBySession, $activeSessionId, $busy],
  (bySession, sid, busy) => {
    if (busy || !sid) {
      return 0
    }

    return activeSubagentCount(bySession[sid] ?? [])
  }
)

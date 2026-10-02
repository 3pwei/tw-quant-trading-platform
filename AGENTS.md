# Public Platform instructions

Keep real-order execution disabled. Preserve owner isolation, recovery locks,
Kill Switch, durable order/outbox-before-submit and authoritative FillEvent
position changes. Never add secrets, runtime records or concrete strategy logic.
Use Core contracts for strategy plugins. No fallback evaluator is permitted.

Run applicable Python, Dashboard and security checks. Do not merge or deploy
without explicit authorization. Build and test public source without private
package credentials or private strategy artifacts.

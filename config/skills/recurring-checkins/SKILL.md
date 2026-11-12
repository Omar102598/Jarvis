---
name: recurring-checkins
description: Scheduling repeating check-ins/reminders from chat ("every Sunday check in", "weekdays at 5") AND proactively offering one when the user starts a goal, plan or habit.
---

Recurring check-ins — schedule these YOURSELF, they are NOT code changes:
"check in with me every Sunday", "every weekday at 5 remind me…", "review X monthly" →
schedule_recurring_checkin(title, cron in the user's LOCAL time, prompt). Write `prompt` as
an instruction to your future self naming the tools to call. confirmed=True when the user
asked; list_recurring_checkins / cancel_recurring_checkin manage them. Never send these to Forge.
- BE PROACTIVE about it: when the user sets a goal, plan or habit that only works with
  regular review (a diet, training block, budget, job search), OFFER a fitting check-in in
  your reply and file it with confirmed=False — it lands in the Approval Inbox as a one-tap
  "set up". If they say yes in chat, approve it with manage_approvals.

---
name: recomp-nutrition-training
description: The user's 12-week body recomposition plan: food/meal logging and macro estimates, nutrition totals, weight/waist, weekly progress check-ins, today's workout, skipping a day, changing the lifting split.
---

Body recomposition, nutrition & training — the user is on a 12-week recomp plan:
- get_recomp_plan for targets/week/phase/today's training; get_recomp_checkin for progress
  (present its suggestion and ASK before update_recomp_targets).
- Food: when the user says what they ate or shows food, estimate macros and log_meal (one
  entry per meal); edit_meal / delete_meal for corrections; get_nutrition_today /
  get_nutrition_history for totals. "What should I eat?" → fit the remaining macros, prefer
  their favorite meals. log_body_metrics for weight/waist.
- Training is Apollo's rolling split (get_todays_workout); "I skipped today" →
  skip_workout_day so the rotation continues instead of dropping a workout. "Change my
  split / swap workouts / start with legs" → set_workout_split (a tool, NOT a Forge task).

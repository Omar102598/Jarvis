---
name: groceries
description: Grocery shopping as REMY, the grocery agent: building carts at H-E-B, Target, Amazon Fresh or Whole Foods, the usual order, favourite products, approving or checking a grocery order, meal ideas from groceries.
---

Groceries belong to Remy, the grocery agent. Remy runs in the background with
his own browser, which is signed in to H-E-B, Target, Amazon Fresh and Whole
Foods. Never hand groceries to spawn_task (a task agent "acting as Remy" has
no logins), and don't build carts yourself in mac_chrome (the user's own
Chrome usually isn't signed in to grocery sites).

- trigger_grocery_run(stores, budget, notes): "build me a grocery cart",
  "use Remy", "do the weekly shop". Pass what the user said:
  - stores: "heb", "target", "amazon", "whole_foods" (comma-separated), or
    empty to let Remy compare and pick.
  - budget: their cap in dollars ("don't go over $150" -> 150), else 0.
  - notes: every other instruction, in their words ("use the existing cart
    items as a reference", "no fish").
  Remy plans meals from the nutrition profile and recomp targets, ADDS items
  (never removes what's in the cart) and never checks out. Tell the user he's
  on it and will report back; don't wait for him.
- get_grocery_status: what Remy built last, the total, and whether it's pending.
- approve_grocery_order: only when the user explicitly approves the pending order.
- get_usual_order / learn_fresh_cart: the learned staples; learn_fresh_cart
  rescans a store's current cart into them.
- pin_favorite_product: "always get this brand of X".
- suggest_meals: meal ideas from what they have or plan to buy.

If the request is cut off mid-sentence, ask for the rest before triggering.

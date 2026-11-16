---
name: groceries
description: Grocery shopping as REMY, the grocery agent: building carts at H-E-B, Target, Amazon Fresh or Whole Foods, removing items from carts, learning from past orders, the usual order, favourite products, approving or checking a grocery order, meal ideas from groceries.
---

Groceries belong to Remy, the grocery agent. Remy runs in the background with
his own browser, which is signed in to H-E-B, Target, Amazon Fresh and Whole
Foods. Never hand groceries to spawn_task (a task agent "acting as Remy" has
no logins), and don't build carts yourself in mac_chrome (the user's own
Chrome usually isn't signed in to grocery sites).

- add_grocery_items(items, store, force): when the user NAMES items ("add egg
  whites and spinach", "put 2 bags of basmati in my H-E-B cart"), or says "just
  add what's missing" after editing the cart themselves and you can tell what's
  missing. Adds exactly those, nothing else. Remy skips anything already in the
  cart; force=True only when they explicitly want more of something they have.
  Never use trigger_grocery_run for a few items.
- trigger_grocery_run(stores, budget, notes): the FULL weekly shop only: "build
  me a grocery cart", "do the weekly shop", "plan my groceries for the week".
  Remy reads the cart first and adds only what the week's meals still need, so
  it's also right for "fill in whatever's missing for my meals" when you can't
  name the items. Pass what the user said:
  - stores: "heb", "target", "amazon", "whole_foods" (comma-separated), or
    empty to let Remy compare and pick.
  - budget: their cap in dollars ("don't go over $150" -> 150), else 0.
  - notes: every other instruction, in their words ("use the existing cart
    items as a reference", "no fish").
  Remy plans meals from the nutrition profile and recomp targets, ADDS items
  (never removes what's in the cart, never re-adds what's already there) and
  never checks out. Only one Remy job runs at a time; a second one queues.
  Don't start a learn_fresh_cart scan and a run in the same turn unless the
  user asked for both: the run reads the cart itself. Tell the user he's
  on it and will report back; don't wait for him.
- remove_grocery_items(items, stores): take items out of any cart, whoever
  added them. items in the user's words, comma-separated; "all" only when
  they explicitly ask to empty a cart. stores defaults to all carts.
  To undo Remy's own shopping ("remove what Remy added today", "keep my
  items, take out his"), use added_by_remy="today" / "last" / "YYYY-MM-DD"
  and leave items empty: Remy logs what he adds, so the user's items stay.
  Never describe "what Remy added" in items -- the cart can't tell who
  added a line, only the log can.
- learn_fresh_cart(store, source="orders"): "learn what I usually order",
  "read my past orders" -- reads past orders at all stores (store="all").
- get_grocery_status: what Remy built last, the total, and whether it's pending.
- approve_grocery_order: only when the user explicitly approves the pending order.
  Remy flags quantities above the user's usual (e.g. six tubs of yogurt) in
  his report and in get_grocery_status; approve_grocery_order refuses until the
  user confirms them. Read the flagged lines out, and pass
  confirm_quantities=True only after the user says those amounts are right.
  Never change quantities yourself; offer to have Remy fix them.
- get_usual_order: the learned items, with how many orders each was in.
  learn_fresh_cart(store, source="cart") rescans a store's current cart.
- pin_favorite_product: "always get this brand of X".
- suggest_meals: meal ideas from what they have or plan to buy.

If the request is cut off mid-sentence, ask for the rest before triggering.

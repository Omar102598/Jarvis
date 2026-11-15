---
name: food-delivery
description: Ordering restaurant food on Uber Eats: finding restaurants, reading a menu, picking dishes that fit the user's goals, building or changing their Uber Eats cart, what's in their Uber Eats carts. Not groceries (that's Remy).
---

Uber Eats runs through the user's signed-in session in the Mac browser (the
same browser Remy uses). Jarvis builds the cart; the user always checks out
in the Uber Eats app. There is no tool that places an order -- never say the
order is placed, and don't offer to place it.

- ubereats_search(query): restaurants near the user's saved address, by name
  ("Blue Sushi") or cuisine ("sushi"). Gives store ids.
- ubereats_menu(store, filter): the menu with prices and descriptions, and
  whether the store is open. Read it before recommending dishes; for nutrition
  goals, also load the recomp-nutrition-training skill and fit their macros.
- ubereats_item_options(store, item): option groups for items marked
  [has options] (size, rice, sauce). Ask the user about required choices you
  can't infer.
- ubereats_add_to_cart(store, item, quantity, options, instructions): adds to
  that restaurant's cart and reports the new total. Use exact menu names. Add
  only what the user asked for or agreed to.
- ubereats_view_cart(store): one or all carts with totals (Uber keeps one cart
  per restaurant).
- ubereats_remove_from_cart(store, items): items by name, or "all" to empty it.

If a tool says the browser is busy, Remy is shopping; try again in a few
minutes. If it says Uber Eats isn't signed in, ask the user to sign in at
ubereats.com in Remy's browser on the Mac.

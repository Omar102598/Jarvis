---
name: shopping
description: Shopping for products on Amazon (not groceries) — finding, showing or comparing products to buy (supplements, gear, electronics, household items), and putting chosen ones in the Amazon cart.
---

# Shopping on Amazon

Tools:
- `amazon_search(query, max_results)`: live Amazon listings, shown to the user as product cards (photo, price, rating; tapping opens the listing). Run it every time the user wants to SEE products, including "show me those again"; earlier answers from memory or review sites have no listings to show.
- `amazon_add_to_cart(items)`: a link that fills the user's Amazon retail cart with ASINs. Only ASINs that came from `amazon_search` (or a listing the user gave you); never a guessed one.

How to answer:
- Search with the words a shopper would type ("whey protein isolate unflavored"), one search per distinct product type.
- The card shows the photos and prices, so in your reply pick: which one you'd choose for the user and why (their goals from the profile, e.g. protein per calorie on the recomp plan), and what to avoid. Don't re-list every price.
- Sponsored results are marked. Don't recommend one just because it's first.
- Nothing is bought; the user checks out on Amazon.
- Groceries (H-E-B, Amazon Fresh, Whole Foods) are Remy's: load the groceries skill instead.
- If the browser is busy (a grocery run), say so and offer to search when Remy is done.

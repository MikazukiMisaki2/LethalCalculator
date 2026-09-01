# Faith/Crest semantics

`CardRules v2` treats Faith as a logical resource with zero or more runtime
instances. A Faith instance is identified by `source_card_id` and, when it
comes from a live Tracker snapshot, `unique_id`. Its `value` is mutable;
`initial_value` is only the value used when the instance is created.

An ability stored on a Faith instance is a persistent event listener. A card
can add one with `grant_resource_ability`; it does not replace existing
listeners. `resource_selector` must be explicit when an effect targets one
instance; an omitted selector means the active/logical Faith resource.

Crests are separate runtime objects. They retain their own `card_id`,
`unique_id`, `countdown`, `faith_value`, `variable_x`, and
`supplement_info`. A Crest may coexist with Faith instances and must not be
collapsed into a single integer. Tracker's `crests` and `extra_crests` map to
these objects.

`on_ally_follower_evolve` and `on_ally_amulet_destroy` are global event
triggers. They are distinct from `on_evolve` and `on_destroy`, which describe
the card/entity currently resolving. This distinction is required for cards
such as Sathanid and Lyanthoth.

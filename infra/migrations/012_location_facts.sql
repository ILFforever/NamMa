-- Facts about one saved place, not the whole person: "ร้านส้มตำมีหมา" is the shop's dog, "บ้านแม่มียายติดเตียง" is
-- grandma at mother's home. Same shape as user_memory's columns (household, house, assets, history; needs stay with
-- the person in user_memory). pending holds this place's sensitive facts until the person consents, as pending_facts
-- does for the general ones. Both go with the place when it is deleted.
ALTER TABLE user_locations
    ADD COLUMN facts   JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN pending JSONB NOT NULL DEFAULT '{}';

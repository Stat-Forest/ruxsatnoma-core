-- Schema `rules` - the rule engine store: norms (VMQ 689), tariffs (VMQ 278),
-- season calendars, rotation plans and the history of the base calculation unit.
-- Specification module 10.3, scenario S18.
--
-- Applied after 30-geo.sql: rules.norm and rules.rotation_plan reference geo.contour.
--
-- Three ideas shape the whole file.
--
--   Versioning. A norm or a tariff is never edited in place. A change is a new row
--   with its own effective period and its own rule_version. Permits already issued
--   keep pointing at the version they were calculated with through
--   app.calculation.rule_version, so they are never recalculated.
--   Clause 4.2.15, step 18.3 of scenario S18.
--
--   Approval. Nothing becomes PUBLISHED without the document that approves it and
--   the person who approved it. Step 18.1 of scenario S18. A retroactive change
--   needs a second approver on top of that - maker-checker, step 18.2.
--
--   Coefficients are data. Their numeric values are still unknown: questions N1
--   (conditional head coefficients), N2 (tariff coefficients and the base unit)
--   and N3 (season and rotation calendars) are open with the customer. Every
--   coefficient is therefore a column to be filled with rows, never a literal in
--   a DEFAULT or a CHECK. When the values arrive, nothing in this file changes.
--
-- Six activity types are permitted by the Forest Code and VMQ 278:
--   GRAZING, HAYMAKING, BEEKEEPING, RECREATION, FIREWOOD, RESEARCH.
--
-- created_by and updated_by are plain uuid, as everywhere else in the schema:
-- they are audit trail, not a domain relation. approved_by is a domain relation
-- and does carry a foreign key.


-- ---------------------------------------------------------------------------
-- rules.bhm_history - base calculation unit (BHM), revised annually
-- ---------------------------------------------------------------------------
-- Amount = BHM * coefficient * quantity. The value lives here once and is
-- resolved by date, instead of being copied into every tariff row: the base unit
-- is revised every year while the tariff coefficients are not. Each calculation
-- freezes the value it used in app.calculation.bhm_value, so a later revision
-- never moves the amount of an issued permit.

CREATE TABLE rules.bhm_history (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    value           numeric(18,2) NOT NULL,     -- UZS
    effective_from  date NOT NULL,
    effective_to    date,                       -- open ended while in force
    legal_act       text,                       -- act that set the value
    approval_doc_id uuid,                       -- nullable: legacy values carry no document
    source          text NOT NULL DEFAULT 'MANUAL',
    legacy_id       bigint,
    legacy_table    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_by      uuid,

    CONSTRAINT bhm_history_value_positive
        CHECK (value > 0),
    CONSTRAINT bhm_history_source_known
        CHECK (source IN ('MANUAL', 'LEGACY_IMPORT')),
    CONSTRAINT bhm_history_period_ordered
        CHECK (effective_to IS NULL OR effective_to >= effective_from),

    -- exactly one base unit value is in force at any moment
    CONSTRAINT bhm_history_no_overlap EXCLUDE USING gist (
        daterange(effective_from, effective_to, '[]') WITH &&
    )
);

CREATE INDEX bhm_history_by_period ON rules.bhm_history (effective_from DESC);


-- ---------------------------------------------------------------------------
-- rules.season_calendar - length of the grazing and mowing season, by territory
-- ---------------------------------------------------------------------------
-- Supplies Season_share in Oz = Yield_c_per_ha * Area_ha * Season_share.
-- The season differs between regions, hence territory_code. Concrete dates and
-- shares come from VMQ 689 and are pending question N3.

CREATE TABLE rules.season_calendar (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    territory_code  text NOT NULL,              -- SOATO code of the region
    activity_type   text NOT NULL,
    season_start    date NOT NULL,
    season_end      date NOT NULL,
    season_days     integer GENERATED ALWAYS AS ((season_end - season_start) + 1) STORED,
    season_share    numeric(8,6) NOT NULL,      -- part of the year the season covers
    status          text NOT NULL DEFAULT 'DRAFT',
    effective_from  date NOT NULL,
    effective_to    date,
    approval_doc_id uuid,
    notes           text,
    legacy_id       bigint,
    legacy_table    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_by      uuid,

    CONSTRAINT season_calendar_activity_type_known CHECK (activity_type IN (
        'GRAZING', 'HAYMAKING', 'BEEKEEPING', 'RECREATION', 'FIREWOOD', 'RESEARCH'
    )),
    CONSTRAINT season_calendar_status_known
        CHECK (status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')),
    CONSTRAINT season_calendar_season_ordered
        CHECK (season_end >= season_start),
    CONSTRAINT season_calendar_share_is_a_fraction
        CHECK (season_share > 0 AND season_share <= 1),
    CONSTRAINT season_calendar_period_ordered
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT season_calendar_published_requires_approval
        CHECK (status <> 'PUBLISHED' OR approval_doc_id IS NOT NULL),

    -- one calendar in force per territory and activity, so the lookup by date
    -- can never return two rows
    CONSTRAINT season_calendar_no_overlapping_published EXCLUDE USING gist (
        territory_code WITH =,
        activity_type WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    ) WHERE (status = 'PUBLISHED')
);

CREATE INDEX season_calendar_lookup
    ON rules.season_calendar (territory_code, activity_type, effective_from DESC)
    WHERE status = 'PUBLISHED';


-- ---------------------------------------------------------------------------
-- rules.rotation_plan - years of use followed by years of rest, per contour
-- ---------------------------------------------------------------------------
-- Rotation is spatial: a pasture rests, a province does not. The plan is a cycle
-- that starts on cycle_start and repeats: use_years of use, then rest_years of
-- rest. An application whose period falls into a rest year is refused with
-- ERR-NORM-003 and RJ-07, and the answer names the next allowed window, which is
-- why the cycle is stored rather than just the current phase.

CREATE TABLE rules.rotation_plan (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contour_id      uuid NOT NULL REFERENCES geo.contour(id),
    activity_type   text NOT NULL,
    cycle_start     date NOT NULL,              -- first day of the first year of use
    use_years       integer NOT NULL,
    rest_years      integer NOT NULL,
    cycle_years     integer GENERATED ALWAYS AS (use_years + rest_years) STORED,
    status          text NOT NULL DEFAULT 'DRAFT',
    effective_from  date NOT NULL,
    effective_to    date,
    approval_doc_id uuid,
    notes           text,
    legacy_id       bigint,
    legacy_table    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_by      uuid,
    updated_by      uuid,

    CONSTRAINT rotation_plan_activity_type_known CHECK (activity_type IN (
        'GRAZING', 'HAYMAKING', 'BEEKEEPING', 'RECREATION', 'FIREWOOD', 'RESEARCH'
    )),
    CONSTRAINT rotation_plan_status_known
        CHECK (status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')),
    CONSTRAINT rotation_plan_use_years_positive
        CHECK (use_years >= 1),
    CONSTRAINT rotation_plan_rest_years_not_negative
        CHECK (rest_years >= 0),
    CONSTRAINT rotation_plan_period_ordered
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT rotation_plan_published_requires_approval
        CHECK (status <> 'PUBLISHED' OR approval_doc_id IS NOT NULL),

    CONSTRAINT rotation_plan_no_overlapping_published EXCLUDE USING gist (
        contour_id WITH =,
        activity_type WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    ) WHERE (status = 'PUBLISHED')
);

CREATE INDEX rotation_plan_lookup
    ON rules.rotation_plan (contour_id, activity_type, effective_from DESC)
    WHERE status = 'PUBLISHED';


-- ---------------------------------------------------------------------------
-- rules.norm - versioned norm of use, per contour and activity type
-- ---------------------------------------------------------------------------
-- Holds the whole VMQ 689 chain, inputs included, so that max_sb can be
-- re-derived from the row alone years later:
--
--   Oz     = yield_c_per_ha * area_ha * season_share
--   Oz_eff = Oz * insurance_reserve_ratio
--   max_sb = floor(Oz_eff / feed_unit_per_head)
--
-- insurance_reserve_ratio and feed_unit_per_head are columns, not literals: the
-- formula belongs to the rule engine, the numbers belong to the row that was in
-- force. A later amendment of VMQ 689 becomes a new norm version, not a rewrite
-- of past calculations.
--
-- The chain applies to GRAZING. The other five activity types are charged per
-- hectare, hive or cubic metre and carry no conditional head limit, so those
-- columns stay empty for them.

CREATE TABLE rules.norm (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contour_id              uuid NOT NULL REFERENCES geo.contour(id),
    activity_type           text NOT NULL,
    organization_id         uuid REFERENCES iam.organization(id),  -- forestry responsible for the norm,
                                                                   -- reported to the applicant with ERR-NORM-001

    -- inputs of the calculation
    survey_doc_id           uuid,                  -- geobotanical survey document
    survey_date             date,
    yield_c_per_ha          numeric(12,4),         -- centners of feed per hectare
    area_ha                 numeric(12,4),         -- contour area at the moment of calculation
    season_calendar_id      uuid REFERENCES rules.season_calendar(id),
    season_share            numeric(8,6),          -- copied from the calendar, frozen with the version
    rotation_plan_id        uuid REFERENCES rules.rotation_plan(id),
    insurance_reserve_ratio numeric(6,4),          -- VMQ 689 insurance reserve
    feed_unit_per_head      numeric(10,4),         -- centners of feed units per conditional head

    -- result
    max_sb                  numeric(12,2),         -- maximum conditional head load of the contour

    -- versioning and approval
    rule_version            text NOT NULL,
    -- Nullable on purpose. Scenario S18 step 1 creates the draft before the
    -- geobotanical survey document exists, so a NOT NULL here would make a draft
    -- impossible to insert. The requirement that matters -- no publication
    -- without an approving document -- is carried by
    -- norm_published_requires_approval below, which also demands an approver.
    approval_doc_id         uuid,
    status                  text NOT NULL DEFAULT 'DRAFT',
    effective_from          date NOT NULL,
    effective_to            date,
    approved_by             uuid REFERENCES iam.user_account(id),
    approved_at             timestamptz,
    is_retroactive          boolean NOT NULL DEFAULT false,
    retroactive_approved_by uuid REFERENCES iam.user_account(id),
    supersedes_id           uuid REFERENCES rules.norm(id),
    notes                   text,
    legacy_id               bigint,
    legacy_table            text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    created_by              uuid,
    updated_by              uuid,

    CONSTRAINT norm_activity_type_known CHECK (activity_type IN (
        'GRAZING', 'HAYMAKING', 'BEEKEEPING', 'RECREATION', 'FIREWOOD', 'RESEARCH'
    )),
    CONSTRAINT norm_status_known
        CHECK (status IN ('DRAFT', 'REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED')),
    CONSTRAINT norm_period_ordered
        CHECK (effective_to IS NULL OR effective_to >= effective_from),

    -- step 18.1 of scenario S18: no approval document, no publication
    CONSTRAINT norm_published_requires_approval CHECK (
        status <> 'PUBLISHED'
        OR (approval_doc_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
    ),

    -- step 18.2: a retroactive version needs a second pair of eyes
    CONSTRAINT norm_retroactive_requires_maker_checker
        CHECK (NOT is_retroactive OR retroactive_approved_by IS NOT NULL),

    -- a published grazing norm is useless without the numbers behind max_sb
    CONSTRAINT norm_published_grazing_is_complete CHECK (
        status <> 'PUBLISHED'
        OR activity_type <> 'GRAZING'
        OR (
            yield_c_per_ha IS NOT NULL
            AND area_ha IS NOT NULL
            AND season_share IS NOT NULL
            AND insurance_reserve_ratio IS NOT NULL
            AND feed_unit_per_head IS NOT NULL
            AND max_sb IS NOT NULL
        )
    ),

    CONSTRAINT norm_yield_not_negative
        CHECK (yield_c_per_ha IS NULL OR yield_c_per_ha >= 0),
    CONSTRAINT norm_area_positive
        CHECK (area_ha IS NULL OR area_ha > 0),
    CONSTRAINT norm_season_share_is_a_fraction
        CHECK (season_share IS NULL OR (season_share > 0 AND season_share <= 1)),
    CONSTRAINT norm_insurance_reserve_is_a_fraction
        CHECK (insurance_reserve_ratio IS NULL
               OR (insurance_reserve_ratio > 0 AND insurance_reserve_ratio <= 1)),
    CONSTRAINT norm_feed_unit_positive
        CHECK (feed_unit_per_head IS NULL OR feed_unit_per_head > 0),
    CONSTRAINT norm_max_sb_not_negative
        CHECK (max_sb IS NULL OR max_sb >= 0),
    CONSTRAINT norm_supersedes_another_row
        CHECK (supersedes_id IS NULL OR supersedes_id <> id),

    -- rules.get_published_norm(contour_id, activity, on_date) must return one row
    -- or none, never two
    CONSTRAINT norm_no_overlapping_published EXCLUDE USING gist (
        contour_id WITH =,
        activity_type WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    ) WHERE (status = 'PUBLISHED')
);

CREATE INDEX norm_lookup
    ON rules.norm (contour_id, activity_type, effective_from DESC)
    WHERE status = 'PUBLISHED';

-- reproducing an old calculation starts from its rule_version
CREATE INDEX norm_by_rule_version ON rules.norm (rule_version);


-- ---------------------------------------------------------------------------
-- rules.tariff - versioned payment coefficients of VMQ 278
-- ---------------------------------------------------------------------------
-- Amount = BHM * coefficient * quantity. The base unit is not repeated here, it
-- is resolved from rules.bhm_history by date; a tariff row carries the
-- coefficient only, and therefore survives the annual revision of the base unit.
--
-- Granularity: one row per activity type, livestock group and privilege
-- category. Grazing is charged per animal group, the other activities are not,
-- which is why livestock_group is empty for them. Privilege categories of
-- clauses 9 to 11 of VMQ 278 are pending question N4, so the column is an open
-- classifier code and carries no value list.

CREATE TABLE rules.tariff (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_type           text NOT NULL,
    livestock_group         text,                  -- grazing only
    privilege_category      text,                  -- null means the base rate
    unit                    text NOT NULL,         -- what quantity is measured in
    coefficient             numeric(12,6) NOT NULL,-- multiplier of the base unit

    -- versioning and approval
    rule_version            text NOT NULL,
    approval_doc_id         uuid NOT NULL,
    status                  text NOT NULL DEFAULT 'DRAFT',
    effective_from          date NOT NULL,
    effective_to            date,
    approved_by             uuid REFERENCES iam.user_account(id),
    approved_at             timestamptz,
    is_retroactive          boolean NOT NULL DEFAULT false,
    retroactive_approved_by uuid REFERENCES iam.user_account(id),
    supersedes_id           uuid REFERENCES rules.tariff(id),
    notes                   text,
    legacy_id               bigint,
    legacy_table            text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    created_by              uuid,
    updated_by              uuid,

    CONSTRAINT tariff_activity_type_known CHECK (activity_type IN (
        'GRAZING', 'HAYMAKING', 'BEEKEEPING', 'RECREATION', 'FIREWOOD', 'RESEARCH'
    )),

    -- twelve groups of appendix 5 to VMQ 689: four adult species, the same four
    -- under two years, sheep and goats over six months, lambs and kids under six
    CONSTRAINT tariff_livestock_group_known CHECK (livestock_group IS NULL OR livestock_group IN (
        'CATTLE_ADULT', 'HORSE_ADULT', 'CAMEL_ADULT', 'DONKEY_ADULT',
        'CATTLE_YOUNG', 'HORSE_YOUNG', 'CAMEL_YOUNG', 'DONKEY_YOUNG',
        'SHEEP_OVER_6M', 'GOAT_OVER_6M', 'LAMB_UNDER_6M', 'KID_UNDER_6M'
    )),
    CONSTRAINT tariff_livestock_group_belongs_to_grazing
        CHECK ((activity_type = 'GRAZING') = (livestock_group IS NOT NULL)),

    CONSTRAINT tariff_unit_known CHECK (unit IN (
        'HEAD', 'HECTARE', 'HIVE', 'CUBIC_METRE', 'STERE', 'PERSON_DAY'
    )),
    CONSTRAINT tariff_coefficient_not_negative
        CHECK (coefficient >= 0),
    CONSTRAINT tariff_status_known
        CHECK (status IN ('DRAFT', 'REVIEW', 'APPROVED', 'PUBLISHED', 'ARCHIVED')),
    CONSTRAINT tariff_period_ordered
        CHECK (effective_to IS NULL OR effective_to >= effective_from),

    CONSTRAINT tariff_published_requires_approval CHECK (
        status <> 'PUBLISHED'
        OR (approval_doc_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
    ),
    CONSTRAINT tariff_retroactive_requires_maker_checker
        CHECK (NOT is_retroactive OR retroactive_approved_by IS NOT NULL),
    CONSTRAINT tariff_supersedes_another_row
        CHECK (supersedes_id IS NULL OR supersedes_id <> id),

    -- rules.get_tariff(activity, on_date) must be unambiguous for every
    -- combination of group and privilege
    CONSTRAINT tariff_no_overlapping_published EXCLUDE USING gist (
        activity_type WITH =,
        (COALESCE(livestock_group, '')) WITH =,
        (COALESCE(privilege_category, '')) WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    ) WHERE (status = 'PUBLISHED')
);

CREATE INDEX tariff_lookup
    ON rules.tariff (activity_type, effective_from DESC)
    WHERE status = 'PUBLISHED';

CREATE INDEX tariff_by_rule_version ON rules.tariff (rule_version);

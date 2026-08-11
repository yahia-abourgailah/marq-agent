-- ============================================================
-- MarQ Agent - Local Deals Test Database
-- TEST DATA ONLY
-- ============================================================

DROP TABLE IF EXISTS deals;

CREATE TABLE deals (
    id BIGSERIAL PRIMARY KEY,

    name VARCHAR(255) NOT NULL,

    stage VARCHAR(100) NOT NULL,
    status VARCHAR(100) NOT NULL,

    value NUMERIC(15, 2) NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'EGP',

    owner_id INTEGER NOT NULL,
    owner_name VARCHAR(255) NOT NULL,

    project_id INTEGER NOT NULL,
    project_name VARCHAR(255) NOT NULL,

    customer_name VARCHAR(255),

    unit_code VARCHAR(100),

    created_at DATE NOT NULL,
    expected_close_date DATE,

    days_in_stage INTEGER NOT NULL DEFAULT 0,

    deleted_at TIMESTAMP NULL
);


-- ============================================================
-- Sample Deals
-- ============================================================

INSERT INTO deals (
    name,
    stage,
    status,
    value,
    currency,
    owner_id,
    owner_name,
    project_id,
    project_name,
    customer_name,
    unit_code,
    created_at,
    expected_close_date,
    days_in_stage,
    deleted_at
)
VALUES

(
    'Villa 12 - Ahmed Hassan',
    'Negotiation',
    'active',
    12400000,
    'EGP',
    101,
    'Sara Mostafa',
    1,
    'MarQ Gardens',
    'Ahmed Hassan',
    'GRD-B2-12',
    '2026-05-14',
    '2026-09-01',
    23,
    NULL
),

(
    'Apartment 304 - Omar Ali',
    'Qualified',
    'active',
    8500000,
    'EGP',
    102,
    'Ahmed Mohamed',
    1,
    'MarQ Gardens',
    'Omar Ali',
    'GRD-A3-04',
    '2026-06-10',
    '2026-09-20',
    12,
    NULL
),

(
    'Villa 7 - Mohamed Samir',
    'Reserved',
    'active',
    15000000,
    'EGP',
    101,
    'Sara Mostafa',
    2,
    'MarQ North',
    'Mohamed Samir',
    'NOR-V7',
    '2026-07-18',
    '2026-08-25',
    18,
    NULL
),

(
    'Apartment 112 - Youssef Adel',
    'Negotiation',
    'active',
    6700000,
    'EGP',
    103,
    'Omar Khaled',
    3,
    'MarQ Ville',
    'Youssef Adel',
    'VIL-C1-12',
    '2026-07-01',
    '2026-09-10',
    31,
    NULL
),

(
    'Townhouse 21 - Karim Nabil',
    'Qualified',
    'active',
    9800000,
    'EGP',
    102,
    'Ahmed Mohamed',
    2,
    'MarQ North',
    'Karim Nabil',
    'NOR-T21',
    '2026-06-25',
    '2026-10-05',
    15,
    NULL
),

(
    'Villa 18 - Mahmoud Fathy',
    'Closed Won',
    'won',
    17500000,
    'EGP',
    103,
    'Omar Khaled',
    3,
    'MarQ Ville',
    'Mahmoud Fathy',
    'VIL-V18',
    '2026-04-12',
    '2026-07-15',
    7,
    NULL
),

(
    'Apartment 502 - Hassan Tarek',
    'Lead',
    'active',
    5200000,
    'EGP',
    104,
    'Mariam Adel',
    1,
    'MarQ Gardens',
    'Hassan Tarek',
    'GRD-D5-02',
    '2026-08-01',
    '2026-11-01',
    5,
    NULL
),

(
    'Villa 31 - Deleted Deal',
    'Negotiation',
    'active',
    11000000,
    'EGP',
    101,
    'Sara Mostafa',
    1,
    'MarQ Gardens',
    'Test Customer',
    'GRD-B3-31',
    '2026-05-01',
    '2026-09-15',
    40,
    '2026-08-05 10:00:00'
);
-- Spec 9.4: separate least-privilege roles for migration vs runtime.
-- The owner (mm_owner) runs migrations. The runtime role (mm_app) can INSERT
-- into the ledger but can never UPDATE or DELETE a posted entry - the database
-- itself, not application discipline, makes the ledger append-only.

CREATE ROLE mm_app WITH LOGIN PASSWORD 'mm_app_dev_pw';

GRANT CONNECT ON DATABASE moneymovement TO mm_app;
GRANT USAGE ON SCHEMA public TO mm_app;

-- Default privileges for tables created later by the migration owner.
ALTER DEFAULT PRIVILEGES FOR ROLE mm_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mm_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO mm_app;

-- Read-only reporting role (spec 9.4).
CREATE ROLE mm_readonly WITH LOGIN PASSWORD 'mm_readonly_dev_pw';
GRANT CONNECT ON DATABASE moneymovement TO mm_readonly;
GRANT USAGE ON SCHEMA public TO mm_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE mm_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO mm_readonly;

-- A separate database for the automated tests.
--
-- The suite truncates users and ledger tables between tests. Pointing it at
-- the same database the running stack uses means `pytest` silently destroys
-- whatever is deployed - which is exactly what happened once during this
-- build, wiping the seeded demo accounts moments before a demo.
CREATE DATABASE moneymovement_test OWNER mm_owner;
GRANT ALL PRIVILEGES ON DATABASE moneymovement_test TO mm_owner;
GRANT CONNECT ON DATABASE moneymovement_test TO mm_app;

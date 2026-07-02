-- Scratch-only privileged credential for the D93 scratch-write side-channel
-- (table-intermediate Slice 2). Runs after hr-warehouse.sql (alphabetical init
-- order) with CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 enabled.
--
-- HARD INVARIANT #1 (blast radius): this credential is GRANTed on the `scratch`
-- database ONLY. A total logic bug in /scratch/v1/materialize can therefore never
-- CREATE / INSERT / DROP / SELECT a WAREHOUSE table — the warehouse databases are
-- physically outside this credential's reach. The runtime never holds it
-- (invariant #8); it lives only in the MCP deploy config (SCRATCH_CH_*).

CREATE DATABASE IF NOT EXISTS scratch;

CREATE USER IF NOT EXISTS scratch_writer
    IDENTIFIED WITH plaintext_password BY 'scratch-pass';

-- CREATE DATABASE lets the endpoint bootstrap `scratch` on first use (OQ-G); the
-- table-level grants cover materialize (CREATE/INSERT/SELECT) + best-effort drop.
-- ALL scoped to `scratch.*` — never `dbpcm_warehouse.*`.
GRANT CREATE DATABASE ON scratch.* TO scratch_writer;
GRANT CREATE TABLE, DROP TABLE, INSERT, SELECT ON scratch.* TO scratch_writer;

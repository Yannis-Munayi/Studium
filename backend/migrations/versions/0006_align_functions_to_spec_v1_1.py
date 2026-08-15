"""adopt the spec v1.1 function bodies and names

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15

Two functions are brought into line with the ratified spec text, so that a
reader diffing the database against §6.0 and §6.6 finds no differences.

``uuid_generate_v7``: both the 0001 body and the v1.1 body produce a correct
32-character v7 UUID, but v1.1 assembles it component by component with an
explicit ``lpad`` on each piece. That is the more defensive form -- it cannot
silently shorten if an intermediate value happens to be small -- and it is what
the spec now publishes, so it wins.

``bump_session_cost`` -> ``accumulate_session_cost``: rename only, plus the
spec's subquery form of the update. The trigger is recreated under the spec's
name.

Non-additive by the letter of §13 (a function is replaced and a trigger
renamed), but both operations are atomic inside the migration transaction and
neither changes a row, so no maintenance window is needed.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


# UUIDv7 layout (128 bits): 48 bits unix_ts_ms (12 hex) | version 7 (1 hex)
# | 12 bits rand_a (3 hex) | variant + rand_b_high (2 hex) | 56 bits (14 hex)
# = 32 hex characters exactly.
UUID_V7_SPEC = """
CREATE OR REPLACE FUNCTION uuid_generate_v7() RETURNS uuid AS $$
DECLARE
  v_time_ms  bigint;
  v_rand     bytea;
  v_rand_a   int;
  v_variant_byte int;
  v_hex      text;
BEGIN
  v_time_ms := (extract(epoch from clock_timestamp()) * 1000)::bigint;
  v_rand    := gen_random_bytes(10);

  -- 12 bits of rand_a assembled from the low nibble of byte 0 and all of byte 1
  v_rand_a := ((get_byte(v_rand, 0) & 15) << 8) | get_byte(v_rand, 1);

  -- Byte 2: force top two bits to '10' (RFC 4122 variant), keep low 6 as random
  v_variant_byte := (get_byte(v_rand, 2) & 63) | 128;

  v_hex :=
    lpad(to_hex(v_time_ms), 12, '0')                           -- 12 chars
    || '7'                                                     --  1 char
    || lpad(to_hex(v_rand_a), 3, '0')                          --  3 chars
    || lpad(to_hex(v_variant_byte), 2, '0')                    --  2 chars
    || encode(substring(v_rand FROM 4 FOR 7), 'hex');          -- 14 chars

  RETURN v_hex::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
"""

UUID_V7_0001 = """
CREATE OR REPLACE FUNCTION uuid_generate_v7() RETURNS uuid AS $$
DECLARE
  v_time_ms bigint := (extract(epoch from clock_timestamp()) * 1000)::bigint;
  v_rand    bytea  := gen_random_bytes(10);
  v_hex     text   := encode(v_rand, 'hex');
BEGIN
  RETURN (
    lpad(to_hex(v_time_ms), 12, '0') ||
    '7' || substr(v_hex, 1, 3) ||
    to_hex((get_byte(v_rand, 2) & 63) | 128) ||
    substr(v_hex, 7, 14)
  )::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
"""

ACCUMULATE_SESSION_COST = """
CREATE OR REPLACE FUNCTION accumulate_session_cost() RETURNS TRIGGER AS $$
BEGIN
  UPDATE learning_sessions
  SET total_cost_usd = total_cost_usd + NEW.cost_usd
  WHERE id = (SELECT session_id FROM session_turns WHERE id = NEW.session_turn_id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

BUMP_SESSION_COST = """
CREATE OR REPLACE FUNCTION bump_session_cost() RETURNS TRIGGER AS $$
BEGIN
  UPDATE learning_sessions ls
     SET total_cost_usd = ls.total_cost_usd + NEW.cost_usd
    FROM session_turns st
   WHERE st.id = NEW.session_turn_id
     AND ls.id = st.session_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(UUID_V7_SPEC)

    op.execute("DROP TRIGGER IF EXISTS trg_agent_traces_session_cost ON agent_traces")
    op.execute(ACCUMULATE_SESSION_COST)
    op.execute(
        "CREATE TRIGGER trg_agent_traces_accumulate_cost AFTER INSERT ON agent_traces "
        "FOR EACH ROW EXECUTE FUNCTION accumulate_session_cost()"
    )
    op.execute("DROP FUNCTION IF EXISTS bump_session_cost()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_traces_accumulate_cost ON agent_traces")
    op.execute(BUMP_SESSION_COST)
    op.execute(
        "CREATE TRIGGER trg_agent_traces_session_cost AFTER INSERT ON agent_traces "
        "FOR EACH ROW EXECUTE FUNCTION bump_session_cost()"
    )
    op.execute("DROP FUNCTION IF EXISTS accumulate_session_cost()")
    op.execute(UUID_V7_0001)

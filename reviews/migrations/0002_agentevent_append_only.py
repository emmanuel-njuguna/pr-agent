"""Make reviews_agentevent append-only in the database itself (Postgres), not only in Python."""
from django.db import migrations

FORWARD = """
CREATE FUNCTION reviews_agentevent_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'reviews_agentevent is append-only: % is not allowed', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER reviews_agentevent_no_update_delete
    BEFORE UPDATE OR DELETE ON reviews_agentevent
    FOR EACH ROW EXECUTE FUNCTION reviews_agentevent_append_only();

CREATE TRIGGER reviews_agentevent_no_truncate
    BEFORE TRUNCATE ON reviews_agentevent
    FOR EACH STATEMENT EXECUTE FUNCTION reviews_agentevent_append_only();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS reviews_agentevent_no_truncate ON reviews_agentevent;
DROP TRIGGER IF EXISTS reviews_agentevent_no_update_delete ON reviews_agentevent;
DROP FUNCTION IF EXISTS reviews_agentevent_append_only();
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD, params=None)


def backward(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(BACKWARD, params=None)


class Migration(migrations.Migration):
    dependencies = [('reviews', '0001_initial')]
    operations = [migrations.RunPython(forward, backward)]

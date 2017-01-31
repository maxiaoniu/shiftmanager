"""
Query templates for use by the Redshift class.
"""

copy_from_s3 = """\
COPY {table}
FROM '{manifest_key}'
CREDENTIALS '{creds}'
JSON '{jpaths_key}'
MANIFEST GZIP TIMEFORMAT 'auto'
"""

all_privileges = """\
SELECT
  c.relkind,
  n.oid as "schema_oid",
  n.nspname as "schema",
  c.oid as "rel_oid",
  c.relname,
  c.relowner AS "owner_id",
  u.usename AS "owner_name",
  pg_catalog.array_to_string(c.relacl, '\n') AS "privileges",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' END AS "type"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     JOIN pg_catalog.pg_user u ON u.usesysid = c.relowner
WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
  AND n.nspname !~ '^pg_'
ORDER BY c.relkind, n.oid, n.nspname;
"""

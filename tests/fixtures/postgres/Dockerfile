# Postgres fixture with the extensions DB Analyzer probes for.
# pg_stat_statements and pgstattuple ship with the image (contrib); hypopg comes from PGDG apt.
ARG PG_MAJOR=17
FROM postgres:${PG_MAJOR}
ARG PG_MAJOR
RUN apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-hypopg" \
 && rm -rf /var/lib/apt/lists/*

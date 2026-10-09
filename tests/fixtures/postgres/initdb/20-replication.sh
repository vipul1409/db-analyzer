#!/bin/sh
# Let the standby fixtures (compose profile "standby") stream from this primary.
set -e
echo "host replication postgres all scram-sha-256" >> "$PGDATA/pg_hba.conf"

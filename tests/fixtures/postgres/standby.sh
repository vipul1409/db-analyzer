#!/bin/bash
# Hot standby fixture: clone the primary named in $PRIMARY once, then run as a streaming replica.
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until PGPASSWORD=postgres pg_basebackup -h "$PRIMARY" -U postgres -D "$PGDATA" -R -X stream; do
    echo "waiting for $PRIMARY"; sleep 1
  done
  chown -R postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
fi
exec gosu postgres postgres -c hot_standby=on "$@"
